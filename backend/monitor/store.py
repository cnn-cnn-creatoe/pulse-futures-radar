"""Small local SQLite store. Production market data and demo data stay separate."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable

from .engine import Candle
from .history_data import valid_contract_symbol


DAY_MS = 86_400_000
SHANGHAI_OFFSET_MS = 8 * 60 * 60 * 1000


def shanghai_day_start(timestamp_ms: int) -> int:
    """Return the UTC epoch of 00:00 for the timestamp's UTC+8 day."""
    timestamp_ms = int(timestamp_ms)
    return ((timestamp_ms + SHANGHAI_OFFSET_MS) // DAY_MS) * DAY_MS - SHANGHAI_OFFSET_MS


def alert_direction(alert: dict) -> str:
    """Normalize the candidate side without depending on Telegram code."""
    direction = str(alert.get("direction") or "").lower()
    if direction in {"long", "short"}:
        return direction
    pattern = alert.get("pattern")
    if pattern in {"阳阳", "阴阳"}:
        return "long"
    if pattern in {"阴阴", "阳阴"}:
        return "short"
    return "neutral"


def is_usdt_cached_identity(symbol: str, metadata: dict | None = None) -> bool:
    """Local display filter only; a suffix never establishes official scope."""
    metadata = metadata or {}
    quote_asset = metadata.get("quoteAsset", metadata.get("quote_asset"))
    if quote_asset is not None:
        return quote_asset == "USDT"
    if not valid_contract_symbol(symbol):
        return False
    base, separator, expiry = symbol.rpartition("_")
    if separator and len(expiry) == 6 and expiry.isascii() and expiry.isdigit():
        return base.endswith("USDT")
    return symbol.endswith("USDT")


def contract_metadata_fields(metadata: dict) -> dict:
    """Add display aliases without replacing the persisted official fields."""
    def field(camel: str, snake: str):
        return metadata.get(camel, metadata.get(snake))

    contract_type = field("contractType", "contract_type")
    underlying_type = field("underlyingType", "underlying_type")
    subtypes = field("underlyingSubType", "underlying_sub_type")
    subtypes = list(subtypes) if isinstance(subtypes, (list, tuple)) else []
    kind = str(contract_type or "").upper()
    underlying = str(underlying_type or "").upper()
    is_tradfi = (kind in {"TRADIFI_PERPETUAL", "TRADFI_PERPETUAL"}
                 or any(str(tag).upper() in {"TRADFI", "TRADIFI"} for tag in subtypes)
                 or underlying in {"COMMODITY", "STOCK", "EQUITY"}
                 or underlying.endswith("_EQUITY"))
    # Perpetual exchangeInfo rows also contain a far-future deliveryDate
    # sentinel. Expose an expiry only for known dated delivery contracts.
    delivery_type = kind in {"CURRENT_QUARTER", "NEXT_QUARTER",
                             "CURRENT_QUARTER_DELIVERING", "NEXT_QUARTER_DELIVERING"}
    return {"base_asset": field("baseAsset", "base_asset"),
            "quote_asset": field("quoteAsset", "quote_asset"),
            "margin_asset": field("marginAsset", "margin_asset"),
            "contract_type": contract_type, "underlying_type": underlying_type,
            "underlying_sub_type": subtypes, "is_tradfi": is_tradfi,
            "pair": metadata.get("pair"), "onboard_date": field("onboardDate", "onboard_date"),
            "delivery_date": field("deliveryDate", "delivery_date") if delivery_type else None}


def is_trade_qualified(alert: dict, schema_version: int | None = None) -> bool:
    """Legacy/missing qualifications never become actionable by implication."""
    return (alert.get("qualified") is True
            and isinstance(alert.get("quality_schema_version"), int)
            and not isinstance(alert.get("quality_schema_version"), bool)
            and (schema_version is None or alert["quality_schema_version"] == schema_version)
            and alert.get("source", "live") == "live"
            and alert.get("synthetic") is not True)


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=15000")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS candles (
                    symbol TEXT NOT NULL, timeframe TEXT NOT NULL,
                    open_time INTEGER NOT NULL, close_time INTEGER NOT NULL,
                    open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                    close REAL NOT NULL, quote_volume REAL NOT NULL,
                    taker_buy_quote REAL NOT NULL, trades INTEGER NOT NULL,
                    volume REAL NOT NULL, closed INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(symbol, timeframe, open_time)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS alerts (
                    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL, close_time INTEGER NOT NULL,
                    created_at INTEGER NOT NULL, payload TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS alerts_close ON alerts(close_time DESC);
                CREATE TABLE IF NOT EXISTS alert_daily_counts (
                    symbol TEXT NOT NULL, day_start INTEGER NOT NULL,
                    timeframe TEXT NOT NULL, direction TEXT NOT NULL,
                    signal_count INTEGER NOT NULL,
                    PRIMARY KEY(symbol,day_start,timeframe,direction)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS open_interest (
                    symbol TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    open_interest_quantity REAL NOT NULL, mark_price REAL,
                    estimated_value REAL, source_time INTEGER,
                    PRIMARY KEY(symbol, timestamp)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS universe_snapshot (
                    id INTEGER PRIMARY KEY CHECK (id=1),
                    initialized_at INTEGER NOT NULL, observed_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS universe_symbols (
                    symbol TEXT PRIMARY KEY, onboard_date INTEGER NOT NULL,
                    status TEXT NOT NULL, first_seen INTEGER NOT NULL,
                    last_seen INTEGER NOT NULL, active INTEGER NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS listing_events (
                    event_key TEXT PRIMARY KEY, symbol TEXT NOT NULL,
                    onboard_date INTEGER NOT NULL, event_type TEXT NOT NULL,
                    observed_at INTEGER NOT NULL, payload TEXT NOT NULL,
                    UNIQUE(symbol,onboard_date,event_type)
                );
                CREATE INDEX IF NOT EXISTS listing_events_observed
                    ON listing_events(observed_at DESC);
            """)
            # Reconcile an upgraded database once, and repeat only if a legacy
            # writer changed the alert table without updating the counters.
            # Normal restarts therefore avoid the JSON grouping scan.
            counter_version = self._db.execute(
                "SELECT value FROM store_meta WHERE key='alert_daily_counts_version'"
            ).fetchone()
            alert_total = int(self._db.execute("SELECT COUNT(*) FROM alerts").fetchone()[0])
            counter_total = int(self._db.execute(
                "SELECT COALESCE(SUM(signal_count),0) FROM alert_daily_counts"
            ).fetchone()[0])
            if (counter_version is None or counter_version[0] != "2"
                    or alert_total != counter_total):
                self._db.execute("DELETE FROM alert_daily_counts")
                self._db.execute("""
                INSERT INTO alert_daily_counts
                    (symbol,day_start,timeframe,direction,signal_count)
                SELECT symbol,
                       ((close_time + 28800000) / 86400000) * 86400000 - 28800000,
                       timeframe,
                       CASE
                         WHEN lower(COALESCE(json_extract(payload,'$.direction'),'')) IN ('long','short')
                           THEN lower(json_extract(payload,'$.direction'))
                         WHEN json_extract(payload,'$.pattern') IN ('阳阳','阴阳') THEN 'long'
                         WHEN json_extract(payload,'$.pattern') IN ('阴阴','阳阴') THEN 'short'
                         ELSE 'neutral'
                       END,
                       COUNT(*)
                FROM alerts
                GROUP BY symbol,
                         ((close_time + 28800000) / 86400000) * 86400000 - 28800000,
                         timeframe,
                         CASE
                           WHEN lower(COALESCE(json_extract(payload,'$.direction'),'')) IN ('long','short')
                             THEN lower(json_extract(payload,'$.direction'))
                           WHEN json_extract(payload,'$.pattern') IN ('阳阳','阴阳') THEN 'long'
                           WHEN json_extract(payload,'$.pattern') IN ('阴阴','阳阴') THEN 'short'
                           ELSE 'neutral'
                         END
                """)
                self._db.execute("""
                    INSERT INTO store_meta(key,value)
                    VALUES ('alert_daily_counts_version','2')
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value
                """)

    def save_candles(self, candles: Iterable[Candle]) -> None:
        rows = [
            (c.symbol, c.timeframe, c.open_time, c.close_time, c.open, c.high,
             c.low, c.close, c.quote_volume, c.taker_buy_quote, c.trades,
             c.volume, 1)
            for c in candles if c.closed is True
        ]
        if not rows:
            return
        with self._lock, self._db:
            self._db.executemany("""
                INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(symbol,timeframe,open_time) DO UPDATE SET
                    close_time=excluded.close_time, open=excluded.open,
                    high=excluded.high, low=excluded.low, close=excluded.close,
                    quote_volume=excluded.quote_volume,
                    taker_buy_quote=excluded.taker_buy_quote,
                    trades=excluded.trades,volume=excluded.volume,closed=1
            """, rows)

    def get_candles(self, symbol: str, timeframe: str, limit: int = 500,
                    *, start: int | None = None, end: int | None = None) -> list[Candle]:
        sql = "SELECT * FROM candles WHERE symbol=? AND timeframe=? AND closed=1"
        params: list = [symbol, timeframe]
        if start is not None:
            sql += " AND open_time>=?"
            params.append(start)
        if end is not None:
            sql += " AND open_time<=?"
            params.append(end)
        sql += " ORDER BY open_time DESC LIMIT ?"
        params.append(max(1, min(int(limit), 200_000)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return [Candle(**{**dict(row), "closed": bool(row["closed"])}) for row in reversed(rows)]

    def candle_inventory(self, symbols: Iterable[str] | None = None) -> list[dict]:
        """Exact saved closed-bar inventory, grouped by contract and timeframe.

        Count primary-key rows, never download attempts, so upserts and ignored
        open candles cannot inflate progress. Aggregation reads only summary
        columns and joins each latest row through the existing primary key;
        existing databases work immediately without a migration or backfill.
        This synchronous scan is intended for a cached, off-event-loop API read.
        No cache lives here: saves and pruning are reflected on the next call.
        """
        if symbols is None:
            groups: list[list[str] | None] = [None]
        else:
            requested = list(dict.fromkeys([symbols] if isinstance(symbols, str) else symbols))
            if not requested:
                return []
            # Stay below older SQLite builds' bind-parameter limit too.
            groups = [requested[index : index + 900] for index in range(0, len(requested), 900)]
        result: list[dict] = []

        def read(connection: sqlite3.Connection) -> None:
            for group in groups:
                condition = "closed=1"
                parameters: list = []
                if group is not None:
                    condition += " AND symbol IN (" + ",".join("?" for _ in group) + ")"
                    parameters.extend(group)
                rows = connection.execute(f"""
                    WITH inventory AS (
                        SELECT symbol,timeframe,COUNT(*) AS bars,
                               MIN(open_time) AS first_open_time,
                               MAX(open_time) AS last_open_time
                        FROM candles WHERE {condition}
                        GROUP BY symbol,timeframe
                    )
                    SELECT inventory.symbol,inventory.timeframe,inventory.bars,
                           inventory.first_open_time,inventory.last_open_time,
                           latest.close_time AS last_close_time,
                           latest.close AS last_price
                    FROM inventory
                    JOIN candles AS latest ON latest.symbol=inventory.symbol
                         AND latest.timeframe=inventory.timeframe
                         AND latest.open_time=inventory.last_open_time
                    """, parameters).fetchall()
                result.extend(dict(row) for row in rows)

        if self.path == ":memory:":
            with self._lock:
                read(self._db)
        else:
            # This GROUP BY scans the retained history and can take seconds on
            # a multi-million-row database.  A separate WAL reader keeps that
            # diagnostic scan from holding the writer connection's Python lock
            # and delaying live candle persistence or small status reads.
            database = sqlite3.connect(self.path, timeout=15)
            database.row_factory = sqlite3.Row
            try:
                database.execute("PRAGMA query_only=ON")
                database.execute("BEGIN")
                read(database)
            finally:
                database.close()
        return sorted(result, key=lambda row: (row["symbol"], row["timeframe"]))

    def candle_window_stats(self, symbol: str, timeframe: str,
                            start: int | None = None, end: int | None = None) -> dict:
        """Count unique closed bars in an inclusive open-time window.

        The latest candle is the actual stored row inside this same window.
        Gaps remain gaps: this does not infer a continuous interval from the
        first/last timestamps. Aggregate and latest-row reads share the store
        lock so a concurrent save cannot mix two states in one result.
        """
        condition = "symbol=? AND timeframe=? AND closed=1"
        parameters: list = [symbol, timeframe]
        if start is not None:
            condition += " AND open_time>=?"
            parameters.append(start)
        if end is not None:
            condition += " AND open_time<=?"
            parameters.append(end)
        with self._lock:
            aggregate = self._db.execute(f"""
                SELECT COUNT(*) AS bars,MIN(open_time) AS first_open_time,
                       MAX(open_time) AS last_open_time
                FROM candles WHERE {condition}
                """, parameters).fetchone()
            result = {"symbol": symbol, "timeframe": timeframe, **dict(aggregate),
                      "last_close_time": None, "last_price": None, "latest_candle": None}
            if result["bars"]:
                row = self._db.execute("""
                    SELECT * FROM candles
                    WHERE symbol=? AND timeframe=? AND open_time=? AND closed=1
                    """, (symbol, timeframe, result["last_open_time"])).fetchone()
                latest = {**dict(row), "closed": bool(row["closed"])}
                result.update(last_close_time=latest["close_time"], last_price=latest["close"], latest_candle=latest)
        return result

    def save_alert(self, alert: dict) -> bool:
        """Insert one immutable real signal; a repeated close is never inserted twice."""
        if alert.get("source", "live") != "live" or alert.get("synthetic") is True:
            raise ValueError("Production alert storage does not accept demo or synthetic signals")
        close_time = int(alert.get("second_candle_close_time", alert.get("close_time", 0)))
        if not alert.get("symbol") or not alert.get("timeframe") or not close_time:
            raise ValueError("Alert requires symbol, timeframe and candle close time")
        key = f"{alert['symbol']}:{alert['timeframe']}:{close_time}:TWO_BAR_VOLATILITY"
        payload = dict(alert)
        payload.setdefault("id", key)
        payload.setdefault("alert_id", key)
        payload.setdefault("created_at", int(time.time() * 1000))
        day_start = shanghai_day_start(close_time)
        direction = alert_direction(payload)
        with self._lock, self._db:
            cursor = self._db.execute(
                "INSERT OR IGNORE INTO alerts VALUES (?,?,?,?,?,?)",
                (key, alert["symbol"], alert["timeframe"], close_time,
                 payload["created_at"], json.dumps(payload, ensure_ascii=False, allow_nan=False)),
            )
            if cursor.rowcount != 1:
                return False
            self._db.execute("""
                INSERT INTO alert_daily_counts
                    (symbol,day_start,timeframe,direction,signal_count)
                VALUES (?,?,?,?,1)
                ON CONFLICT(symbol,day_start,timeframe,direction)
                DO UPDATE SET signal_count=signal_count+1
            """, (payload["symbol"], day_start, payload["timeframe"], direction))
            counts = self._daily_counts_locked(payload["symbol"], day_start,
                                                payload["timeframe"], direction)
            payload.update(counts)
            # The immutable evidence records the count visible when the live
            # event was accepted. API reads recompute it from the counter table
            # so older rows on the same day also show the final current total.
            self._db.execute("UPDATE alerts SET payload=? WHERE event_key=?", (
                json.dumps(payload, ensure_ascii=False, allow_nan=False), key))
        return True

    def daily_counts_for_alert(self, alert: dict) -> dict:
        """Read the stable counter fields for one already saved live alert."""
        close_time = int(alert.get("second_candle_close_time", alert.get("close_time", 0)) or 0)
        if not alert.get("symbol") or not alert.get("timeframe") or not close_time:
            return {}
        day_start = shanghai_day_start(close_time)
        with self._lock:
            return self._daily_counts_locked(
                str(alert["symbol"]), day_start, str(alert["timeframe"]),
                alert_direction(alert))

    @staticmethod
    def _daily_count_fields(day_start: int, total: int, timeframe: int,
                            direction: int) -> dict:
        return {
            "daily_signal_date": time.strftime(
                "%Y-%m-%d", time.gmtime((day_start + SHANGHAI_OFFSET_MS) / 1000)
            ),
            "daily_signal_count": int(total),
            "daily_timeframe_signal_count": int(timeframe),
            "daily_direction_signal_count": int(direction),
        }

    def _daily_counts_locked(self, symbol: str, day_start: int,
                             timeframe: str, direction: str) -> dict:
        rows = self._db.execute("""
            SELECT timeframe,direction,signal_count
            FROM alert_daily_counts WHERE symbol=? AND day_start=?
        """, (symbol, day_start)).fetchall()
        total = sum(int(row["signal_count"]) for row in rows)
        same_timeframe = sum(int(row["signal_count"]) for row in rows
                             if row["timeframe"] == timeframe)
        same_direction = sum(int(row["signal_count"]) for row in rows
                             if row["direction"] == direction)
        return self._daily_count_fields(day_start, total, same_timeframe, same_direction)

    def _enrich_daily_counts(self, alerts: list[dict]) -> list[dict]:
        """Attach final UTC+8 daily counts to list/detail rows in bounded batches."""
        if not alerts:
            return alerts
        identities = []
        for alert in alerts:
            close_time = int(alert.get("second_candle_close_time", alert.get("close_time", 0)) or 0)
            if alert.get("symbol") and close_time:
                identities.append((str(alert["symbol"]), shanghai_day_start(close_time)))
        identities = list(dict.fromkeys(identities))
        grouped: dict[tuple[str, int], list[sqlite3.Row]] = {}
        with self._lock:
            for offset in range(0, len(identities), 300):
                batch = identities[offset:offset + 300]
                placeholders = ",".join("(?,?)" for _ in batch)
                params = [value for pair in batch for value in pair]
                rows = self._db.execute(f"""
                    SELECT symbol,day_start,timeframe,direction,signal_count
                    FROM alert_daily_counts
                    WHERE (symbol,day_start) IN ({placeholders})
                """, params).fetchall()
                for row in rows:
                    grouped.setdefault((row["symbol"], row["day_start"]), []).append(row)
        enriched = []
        for alert in alerts:
            close_time = int(alert.get("second_candle_close_time", alert.get("close_time", 0)) or 0)
            if not alert.get("symbol") or not close_time:
                enriched.append(alert)
                continue
            day_start = shanghai_day_start(close_time)
            rows = grouped.get((str(alert["symbol"]), day_start), [])
            timeframe = str(alert.get("timeframe") or "")
            direction = alert_direction(alert)
            total = sum(int(row["signal_count"]) for row in rows)
            same_timeframe = sum(int(row["signal_count"]) for row in rows
                                 if row["timeframe"] == timeframe)
            same_direction = sum(int(row["signal_count"]) for row in rows
                                 if row["direction"] == direction)
            enriched.append({**alert, **self._daily_count_fields(
                day_start, total, same_timeframe, same_direction)})
        return enriched

    def list_alerts(self, limit: int = 500, include_candles: bool = True,
                    qualified: bool | None = None, schema_version: int | None = None,
                    quote_asset: str | None = None) -> list[dict]:
        # A list row is a decision summary. Drop full indicator histories and
        # policy snapshots inside SQLite before transfer/JSON decoding: SMC
        # events and zones can be much larger than the chart alone. The exact
        # candles, contexts and policy remain available through get_alert().
        # Qualification, reasons, confirmations, trade plan, scores and policy
        # hash stay on the compact row; the immutable payload is never updated.
        projection = "payload" if include_candles else "json_remove(payload, '$.candles', '$.indicators', '$.indicator_context', '$.policy_snapshot')"
        conditions = ["COALESCE(json_extract(payload,'$.source'),'live')='live'", "COALESCE(json_extract(payload,'$.synthetic'),0)!=1"]
        params = []
        if qualified is not None:
            # Include legacy/unscored records in the rejected view, never the
            # qualified view. Filter before LIMIT so older matches remain visible.
            test = "(json_type(payload,'$.qualified')='true' AND json_type(payload,'$.quality_schema_version')='integer')"
            conditions.append(test if qualified else f"NOT COALESCE({test},0)")
        if schema_version is not None:
            conditions.append("json_extract(payload,'$.quality_schema_version')=?")
            params.append(int(schema_version))
        if quote_asset is not None:
            if not isinstance(quote_asset, str) or not quote_asset.isalnum():
                raise ValueError("quote_asset must contain only letters or numbers")
            quote_asset = quote_asset.upper()
            # Filter BEFORE LIMIT: newer excluded quotes must not hide older
            # requested signals. Explicit recorded metadata takes precedence;
            # only legacy records without either field use an exact quote or
            # six-ASCII-digit delivery suffix. This is an archive display rule,
            # not a replacement for official exchangeInfo scope verification.
            conditions.append("""CASE
                WHEN json_type(payload,'$.quote_asset') IS NOT NULL
                    THEN json_extract(payload,'$.quote_asset')=?
                WHEN json_type(payload,'$.quoteAsset') IS NOT NULL
                    THEN json_extract(payload,'$.quoteAsset')=?
                ELSE (symbol GLOB ? OR symbol GLOB ?) END""")
            params.extend([quote_asset, quote_asset, f"?*{quote_asset}",
                           f"?*{quote_asset}_[0-9][0-9][0-9][0-9][0-9][0-9]"])
        sql = f"SELECT {projection} FROM alerts"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY close_time DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100_000)))
        with self._lock:
            rows = self._db.execute(sql, params).fetchall()
        return self._enrich_daily_counts([json.loads(row[0]) for row in rows])

    is_trade_qualified = staticmethod(is_trade_qualified)

    def reconcile_universe(self, metadata: list[dict], observed_at: int) -> list[dict]:
        """Persist official snapshots and emit each verified listing transition once.

        Existing TRADING contracts in the first snapshot are baseline, not new
        listings. An unknown older contract discovered later is also not called
        new. Unknown listings must have opened since the last successful
        snapshot, so adding older asset classes to scope is only discovery.
        Pending entries may be announced honestly as upcoming, and their
        subsequent TRADING transition is a separate event, surviving restarts.
        """
        observed_at = int(observed_at)
        entries = {m["symbol"]: dict(m) for m in metadata if m.get("symbol")}
        if not entries:
            return []  # An empty/failed response must not initialize the baseline.
        events = []
        with self._lock, self._db:
            snapshot = self._db.execute("SELECT * FROM universe_snapshot WHERE id=1").fetchone()
            boot = snapshot["initialized_at"] if snapshot else observed_at
            previous_observed = snapshot["observed_at"] if snapshot else observed_at
            old = {r["symbol"]: dict(r) for r in self._db.execute("SELECT * FROM universe_symbols")}
            self._db.execute("UPDATE universe_symbols SET active=0")
            for symbol, meta in entries.items():
                prior = old.get(symbol)
                onboard = max(0, int(meta.get("onboardDate") or meta.get("onboard_date") or 0))
                status = str(meta.get("status", "UNKNOWN"))
                first_seen = prior["first_seen"] if prior else observed_at
                self._db.execute("""INSERT INTO universe_symbols VALUES (?,?,?,?,?,?,?)
                    ON CONFLICT(symbol) DO UPDATE SET onboard_date=excluded.onboard_date,
                    status=excluded.status,last_seen=excluded.last_seen,active=1,payload=excluded.payload
                    """, (symbol, onboard, status, first_seen, observed_at, 1,
                          json.dumps(meta, ensure_ascii=False, allow_nan=False)))
                event_type = None
                if status == "PENDING_TRADING" and onboard > observed_at:
                    event_type = "upcoming"
                elif status == "TRADING" and snapshot:
                    pending_transition = prior and prior["status"] == "PENDING_TRADING"
                    new_since_snapshot = onboard > previous_observed and (not prior or onboard != prior["onboard_date"])
                    if pending_transition or (new_since_snapshot and onboard <= observed_at):
                        event_type = "listed"
                if event_type:
                    key = f"{symbol}:{onboard}:{event_type}"
                    event = {**contract_metadata_fields(meta),
                             "id": key, "symbol": symbol, "event_type": event_type,
                             "onboard_date": onboard or None, "observed_at": observed_at,
                             "first_seen": first_seen, "quote_asset": meta.get("quoteAsset", meta.get("quote_asset")),
                             "base_asset": meta.get("baseAsset", meta.get("base_asset")),
                             "status": status, "source": "live", "telegram_sent": False,
                             "tradable": status == "TRADING",
                             "title": "新合约已开放交易" if event_type == "listed" else "发现待上架合约",
                             "note": "来自币安 exchangeInfo；交易状态与历史预热就绪分别判断"}
                    inserted = self._db.execute("INSERT OR IGNORE INTO listing_events VALUES (?,?,?,?,?,?)",
                        (key, symbol, onboard, event_type, observed_at,
                         json.dumps(event, ensure_ascii=False, allow_nan=False)))
                    if inserted.rowcount:
                        events.append(event)
            self._db.execute("INSERT INTO universe_snapshot VALUES (1,?,?) ON CONFLICT(id) DO UPDATE SET observed_at=excluded.observed_at",
                             (boot, observed_at))
        return events

    def list_universe(self, active_only: bool = True) -> list[dict]:
        sql = "SELECT * FROM universe_symbols" + (" WHERE active=1" if active_only else "") + " ORDER BY symbol"
        with self._lock:
            rows = self._db.execute(sql).fetchall()
        result = []
        for row in rows:
            metadata = json.loads(row["payload"])
            result.append({**metadata, **contract_metadata_fields(metadata), "first_seen": row["first_seen"],
                           "last_seen": row["last_seen"], "snapshot_active": bool(row["active"])})
        return result

    def list_listing_events(self, limit: int = 100) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""SELECT events.payload AS event_payload,meta.payload AS metadata_payload
                FROM listing_events AS events LEFT JOIN universe_symbols AS meta
                  ON meta.symbol=events.symbol AND meta.onboard_date=events.onboard_date
                ORDER BY events.observed_at DESC,events.event_key LIMIT ?""",
                (max(1, min(int(limit), 10_000)),)).fetchall()
        # Old event JSON remains immutable. Matching stored official metadata
        # can supply newly added display aliases without rewriting old rows.
        return [{**contract_metadata_fields(json.loads(row["metadata_payload"]) if row["metadata_payload"] else {}),
                 **json.loads(row["event_payload"])} for row in rows]

    def universe_snapshot_status(self) -> dict:
        with self._lock:
            row = self._db.execute("SELECT * FROM universe_snapshot WHERE id=1").fetchone()
            total = self._db.execute("SELECT COUNT(*) FROM universe_symbols WHERE active=1").fetchone()[0]
            events = self._db.execute("SELECT event_type,COUNT(*) FROM listing_events GROUP BY event_type").fetchall()
        counts = {kind: count for kind, count in events}
        return {"initialized": row is not None, "initialized_at": row["initialized_at"] if row else None,
                "observed_at": row["observed_at"] if row else None, "snapshot_count": total,
                "listed_events": counts.get("listed", 0), "upcoming_events": counts.get("upcoming", 0)}

    def status_snapshot(self, listing_limit: int = 5) -> tuple[dict, list[dict]]:
        """Read the small persisted status fields without waiting for writes.

        The dashboard's status poll must not queue behind a long prune, history
        page commit, or diagnostic inventory scan. File databases use a
        short-lived WAL reader; in-memory tests retain the single connection.
        Both status sections come from one read transaction.
        """
        limit = max(1, min(int(listing_limit), 10_000))

        def read(connection: sqlite3.Connection) -> tuple[dict, list[dict]]:
            row = connection.execute("SELECT * FROM universe_snapshot WHERE id=1").fetchone()
            total = connection.execute("SELECT COUNT(*) FROM universe_symbols WHERE active=1").fetchone()[0]
            event_counts = connection.execute(
                "SELECT event_type,COUNT(*) FROM listing_events GROUP BY event_type").fetchall()
            event_rows = connection.execute("""SELECT events.payload AS event_payload,
                    meta.payload AS metadata_payload
                FROM listing_events AS events LEFT JOIN universe_symbols AS meta
                  ON meta.symbol=events.symbol AND meta.onboard_date=events.onboard_date
                ORDER BY events.observed_at DESC,events.event_key LIMIT ?""", (limit,)).fetchall()
            counts = {kind: count for kind, count in event_counts}
            snapshot = {
                "initialized": row is not None,
                "initialized_at": row["initialized_at"] if row else None,
                "observed_at": row["observed_at"] if row else None,
                "snapshot_count": total,
                "listed_events": counts.get("listed", 0),
                "upcoming_events": counts.get("upcoming", 0),
            }
            events = [{
                **contract_metadata_fields(json.loads(item["metadata_payload"]) if item["metadata_payload"] else {}),
                **json.loads(item["event_payload"]),
            } for item in event_rows]
            return snapshot, events

        if self.path == ":memory:":
            with self._lock:
                return read(self._db)
        database = sqlite3.connect(self.path, timeout=1)
        database.row_factory = sqlite3.Row
        try:
            database.execute("PRAGMA query_only=ON")
            database.execute("BEGIN")
            return read(database)
        finally:
            database.close()

    def get_alert(self, alert_id: str) -> dict | None:
        """Fetch exactly one persisted event; no recalculation or demo fallback."""
        with self._lock:
            row = self._db.execute("SELECT payload FROM alerts WHERE event_key=?", (alert_id,)).fetchone()
        result = json.loads(row[0]) if row else None
        if result and (result.get("source", "live") != "live" or result.get("synthetic") is True):
            return None
        return self._enrich_daily_counts([result])[0] if result else None

    def save_oi(self, sample: dict) -> None:
        quantity = float(sample["open_interest_quantity"])
        mark = sample.get("mark_price")
        with self._lock, self._db:
            self._db.execute("INSERT OR REPLACE INTO open_interest VALUES (?,?,?,?,?,?)", (
                sample["symbol"], int(sample["timestamp"]), quantity, mark,
                quantity * float(mark) if mark is not None else None,
                sample.get("source_time"),
            ))

    def load_oi(self, symbol: str, since: int = 0, limit: int = 130_000,
                until: int | None = None) -> list[dict]:
        with self._lock:
            rows = self._db.execute("""
                SELECT * FROM open_interest WHERE symbol=? AND timestamp>=?
                AND timestamp<=? ORDER BY timestamp DESC LIMIT ?
            """, (symbol, since, until if until is not None else 2**63 - 1, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    get_oi = load_oi

    def counts(self) -> dict:
        tables = (("candles", "candles"), ("alerts", "alerts"),
                  ("oi_snapshots", "open_interest"))
        if self.path == ":memory:":
            with self._lock:
                return {name: self._db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                        for name, table in tables}

        # Counts are diagnostics and can scan millions of rows. A short-lived
        # WAL reader gives all three values one consistent snapshot without
        # holding the writer connection's Python lock and delaying ingestion.
        db = sqlite3.connect(self.path, timeout=15)
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return {name: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for name, table in tables}
        finally:
            db.close()

    def prune(self, now_ms: int) -> None:
        """Keep two weeks of minutes, a year of intraday bars and 90 days of OI."""
        day = 86_400_000
        with self._lock, self._db:
            self._db.execute("DELETE FROM candles WHERE timeframe='1m' AND open_time<?", (now_ms - 14 * day,))
            self._db.execute("DELETE FROM candles WHERE timeframe NOT IN ('1m','1d') AND open_time<?", (now_ms - 366 * day,))
            self._db.execute("DELETE FROM open_interest WHERE timestamp<?", (now_ms - 90 * day,))

    def close(self) -> None:
        with self._lock:
            self._db.close()
