"""Read-only causal range replay for 5m/15m USD-M USDT contracts.

This module keeps the daily study's rules, while avoiding one database pass and
one cross-sectional liquidity rebuild per calendar day.  Candle rows are read
once in symbol/timeframe order.  Exact rolling trimmed means are maintained by
Fenwick trees, and liquidity ranks are reconstructed once for the whole range.
Only candidates that can reach the 78-point unknown-spread boundary pay the
cost of the production indicator calculation.
"""
from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import argparse
import heapq
import json
import math
from pathlib import Path
import sqlite3
from statistics import fmean
import time
from typing import Any, Iterable

from .daily_analysis import (DAY, HORIZONS, METHODS, SHANGHAI,
                             SENSITIVITY_THRESHOLDS, _finite_number,
                             _forward_return, _method_flags, _open_readonly,
                             _pattern_direction, _row_candle, _summary_rows,
                             _tick_size, reconstruct_oi)
from .engine import (Candle, EngineConfig, LOOKBACK, TIMEFRAME_MS, _body, _kind,
                     _ntr, _valid_prices, _valid_volume, body_score,
                     displacement_score, efficiency_score, flow_score,
                     liquidity_score, oi_score, volume_score)
from .indicators import IndicatorConfig, indicator_context
from .store import contract_metadata_fields


class _Fenwick:
    def __init__(self, size: int) -> None:
        self.values = [0.0] * (size + 1)

    def add(self, position: int, value: float) -> None:
        index = position + 1
        while index < len(self.values):
            self.values[index] += value
            index += index & -index

    def prefix(self, count: int) -> float:
        total = 0.0
        index = count
        while index:
            total += self.values[index]
            index -= index & -index
        return total

    def lower_bound(self, target: float) -> int:
        """Return zero-based index containing the target cumulative count."""
        index = 0
        bit = 1 << (len(self.values).bit_length() - 1)
        while bit:
            candidate = index + bit
            if candidate < len(self.values) and self.values[candidate] < target:
                index = candidate
                target -= self.values[candidate]
            bit >>= 1
        return index


class _RollingTrimmedMean:
    """Exact rolling tail-trimmed mean with duplicate float support."""

    def __init__(self, possible: Iterable[float], trim_percent: float) -> None:
        self.domain = sorted(set(float(value) for value in possible))
        self.positions = {value: index for index, value in enumerate(self.domain)}
        self.counts = _Fenwick(len(self.domain))
        self.sums = _Fenwick(len(self.domain))
        self.count = 0
        self.total = 0.0
        self.trim_percent = trim_percent

    def add(self, value: float) -> None:
        position = self.positions[float(value)]
        self.counts.add(position, 1)
        self.sums.add(position, value)
        self.count += 1
        self.total += value

    def remove(self, value: float) -> None:
        position = self.positions[float(value)]
        self.counts.add(position, -1)
        self.sums.add(position, -value)
        self.count -= 1
        self.total -= value

    def _smallest_sum(self, count: int) -> float:
        if count <= 0:
            return 0.0
        position = self.counts.lower_bound(count)
        before_count = self.counts.prefix(position)
        before_sum = self.sums.prefix(position)
        return before_sum + (count - before_count) * self.domain[position]

    def mean(self, method: str) -> float | None:
        if self.count <= 0:
            return None
        if method == "simple_mean":
            return self.total / self.count
        trim = math.floor(self.count * self.trim_percent / 100)
        kept = self.count - 2 * trim
        if kept <= 0:
            return None
        low = self._smallest_sum(trim)
        high = self.total - self._smallest_sum(self.count - trim)
        return (self.total - low - high) / kept


class _OIReconstructor:
    """Batch production-equivalent OI reconstruction for one symbol.

    The official archive used by the range study is sampled on exact 5-minute
    exchange timestamps (``timestamp == source_time``). Changes and rolling
    baselines are calculated once per boundary and then answered with prefix
    sums. The same index also honors validated legacy receipt timestamps; a
    pathological non-monotone receipt sequence falls back to the authoritative
    helper rather than silently changing rules.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        # Rows arrive in receipt-time order. Match ``reconstruct_oi``'s
        # setdefault rule: the first valid receipt for a source timestamp wins.
        # An exact official backfill row therefore supersedes a later legacy
        # receipt carrying the same source time.
        chosen: dict[int, tuple[int, float]] = {}
        for row in rows:
            source = row.get("source_time")
            timestamp = row.get("timestamp")
            if (not isinstance(source, int) or isinstance(source, bool) or source <= 0
                    or not isinstance(timestamp, int)
                    or not -5_000 <= timestamp - source < 75_000):
                continue
            chosen.setdefault(source, (timestamp, float(row["open_interest_quantity"])))
        canonical = all(timestamp == source and source % TIMEFRAME_MS["5m"] == 0
                        for source, (timestamp, _quantity) in chosen.items())
        eligible = [(source, item[0], item[1]) for source, item in chosen.items()]
        eligible.sort()
        self.canonical = canonical
        self.times = [item[0] for item in eligible]
        self.receipts = {source: timestamp for source, timestamp, _quantity in eligible}
        self.quantities = {source: quantity for source, _timestamp, quantity in eligible}
        self.receipt_order = sorted((timestamp, source)
                                    for source, timestamp, _quantity in eligible)
        self.receipt_times = [item[0] for item in self.receipt_order]

    def many(self, timeframe: str, close_times: Iterable[int]) -> dict[int, dict[str, Any]]:
        requested = sorted(set(int(value) for value in close_times))
        if not requested:
            return {}
        interval = TIMEFRAME_MS[timeframe]
        duration = 2 * interval
        ends = [close_time + 1 for close_time in requested]
        first_boundary = min(ends) - LOOKBACK[timeframe]["days"] * DAY
        first_boundary -= first_boundary % interval
        last_boundary = max(ends)
        boundaries = list(range(first_boundary, last_boundary + interval, interval))
        prefix_sum = [0.0]
        changes: dict[int, float] = {}
        valid_boundary_indexes: list[int] = []
        valid_min_receipts: list[int] = []

        def value(boundary: int) -> tuple[int, int, float] | None:
            index = bisect_right(self.times, boundary) - 1
            while index >= 0 and self.receipts[self.times[index]] > boundary:
                index -= 1
            if index < 0 or boundary - self.times[index] > 75_000:
                return None
            source = self.times[index]
            return source, self.receipts[source], self.quantities[source]

        for boundary in boundaries:
            first, last = value(boundary - duration), value(boundary)
            change = None
            minimum_receipt = None
            if (first is not None and last is not None and first[2] > 0
                    and abs((last[0] - first[0]) - duration) <= 20_000):
                change = (last[2] / first[2] - 1) * 100
                minimum_receipt = min(first[1], last[1])
            if change is not None:
                changes[boundary] = change
                valid_boundary_indexes.append((boundary - boundaries[0]) // interval)
                valid_min_receipts.append(int(minimum_receipt))
                prefix_sum.append(prefix_sum[-1] + abs(change))
        if any(right < left for left, right in zip(valid_min_receipts, valid_min_receipts[1:])):
            return {close_time: reconstruct_oi(self.rows, timeframe, close_time)
                    for close_time in requested}

        origin = boundaries[0]
        output: dict[int, dict[str, Any]] = {}
        missing = {"oi_change_pct": None, "oi_ratio": None, "oi_sample_count": 0}
        active_receipts: list[tuple[int, int]] = []
        receipt_cursor = 0
        for close_time, end in zip(requested, ends):
            window_start = end - LOOKBACK[timeframe]["days"] * DAY - duration
            while (receipt_cursor < len(self.receipt_order)
                   and self.receipt_order[receipt_cursor][0] <= end):
                receipt, source = self.receipt_order[receipt_cursor]
                heapq.heappush(active_receipts, (source, receipt))
                receipt_cursor += 1
            while active_receipts and active_receipts[0][1] < window_start:
                heapq.heappop(active_receipts)
            left_observation = bisect_left(self.receipt_times, window_start)
            right_observation = bisect_right(self.receipt_times, end)
            if right_observation - left_observation < 33:
                output[close_time] = dict(missing)
                continue
            current = changes.get(end)
            if current is None or not active_receipts:
                output[close_time] = dict(missing)
                continue
            earliest = active_receipts[0][0] + duration
            lower = max(earliest, end - LOOKBACK[timeframe]["days"] * DAY)
            # History walks backwards from end-duration on the candidate's
            # timeframe grid. Ceil the lower bound to that same grid.
            remainder = (lower - end) % interval
            if remainder:
                lower += interval - remainder
            upper = end - duration
            if lower > upper:
                output[close_time] = dict(missing)
                continue
            left_index = max(0, (lower - origin) // interval)
            right_index = min(len(boundaries) - 1, (upper - origin) // interval)
            first_valid = max(bisect_left(valid_boundary_indexes, left_index),
                              bisect_left(valid_min_receipts, window_start))
            after_valid = bisect_right(valid_boundary_indexes, right_index)
            count = max(0, after_valid - first_valid)
            if count < 30:
                output[close_time] = {**missing, "oi_sample_count": count}
                continue
            total = prefix_sum[after_valid] - prefix_sum[first_valid]
            baseline = total / count
            if baseline <= 0:
                output[close_time] = {**missing, "oi_sample_count": count}
                continue
            output[close_time] = {
                "oi_change_pct": current,
                "oi_ratio": abs(current) / baseline,
                "oi_sample_count": count,
            }
        return output


@dataclass(slots=True)
class _LightCandidate:
    """Compact score-only row that can affect diagnostics but never reach 78."""

    symbol: str
    timeframe: str
    close_time: int
    score_before_liquidity: int
    stored_exact: float | None
    oi_available: bool
    liquidity: dict[str, Any] | None = None


def _progress(path: Path, began: float, **values: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_seconds": round(time.monotonic() - began, 3),
        **values,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _series_candidates(candles: list[Candle], start: int, end: int,
                       tick_size: float, config: EngineConfig) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Produce exact engine-equivalent raw anomaly features for one series."""
    if not candles:
        return [], {"observed": 0, "ready": 0, "statuses": {}}
    interval = TIMEFRAME_MS[candles[0].timeframe]
    full_history = LOOKBACK[candles[0].timeframe]["days"] * DAY // interval
    minimum = LOOKBACK[candles[0].timeframe]["minimum_bars"]
    output: list[dict[str, Any]] = []
    statuses = Counter()
    observed = ready = 0
    symbol = candles[0].symbol
    timeframe = candles[0].timeframe

    def invalid_reason(candle: Candle) -> str | None:
        if candle.symbol != symbol or candle.timeframe != timeframe:
            return "mixed_series"
        if candle.closed is not True:
            return "open_candle"
        if (not isinstance(candle.open_time, int)
                or not isinstance(candle.close_time, int)
                or candle.open_time % interval
                or candle.close_time != candle.open_time + interval - 1):
            return "invalid_timestamp"
        if not _valid_prices(candle) or not _valid_volume(candle):
            return "invalid_data"
        return None

    # Treat an invalid row as a hard break.  Authoritative ``analyze`` rejects
    # every prefix containing that row until it has rolled out of the full
    # lookback, just as it does for a time gap.
    segments: list[tuple[int, int, str | None]] = []
    segment_start = 0
    preceding_break: str | None = None
    for index, candle in enumerate(candles):
        reason = invalid_reason(candle)
        if reason is not None:
            if segment_start < index:
                segments.append((segment_start, index, preceding_break))
            if isinstance(candle.open_time, int) and start <= candle.open_time < end:
                observed += 1
                statuses[reason] += 1
            segment_start = index + 1
            preceding_break = f"{reason}_in_lookback"
            continue
        if index > segment_start and candle.open_time != candles[index - 1].open_time + interval:
            segments.append((segment_start, index, preceding_break))
            segment_start = index
            preceding_break = "gap_in_lookback"
    if segment_start < len(candles):
        segments.append((segment_start, len(candles), preceding_break))

    for segment_start, boundary, break_status in segments:
        began_after_break = break_status is not None
        segment = candles[segment_start:boundary]
        if not segment:
            continue
        ntrs = [0.0] * len(segment)
        bodies = [0.0] * len(segment)
        for index in range(1, len(segment)):
            ntrs[index] = _ntr(segment[index], segment[index - 1])
            bodies[index] = _body(segment[index], segment[index - 1])
        v2 = [0.0] * len(segment)
        body2 = [0.0] * len(segment)
        volume2 = [0.0] * len(segment)
        for index in range(min(2, len(segment))):
            if start <= segment[index].open_time < end:
                observed += 1
                statuses[break_status or "insufficient_history"] += 1
        required_window = full_history + 3 if began_after_break else minimum + 3
        for index in range(2, len(segment)):
            v2[index] = ntrs[index - 1] + ntrs[index]
            body2[index] = bodies[index - 1] + bodies[index]
            volume2[index] = segment[index - 1].quote_volume + segment[index].quote_volume
        possible = range(2, len(segment))
        vtree = _RollingTrimmedMean((v2[index] for index in possible), config.trim_percent)
        btree = _RollingTrimmedMean((body2[index] for index in possible), config.trim_percent)
        qtree = _RollingTrimmedMean((volume2[index] for index in possible), config.trim_percent)
        first_window = 2
        last_window = 1
        for index in range(2, len(segment)):
            wanted_first = max(2, index - full_history)
            wanted_last = index - 2
            while last_window < wanted_last:
                last_window += 1
                vtree.add(v2[last_window])
                btree.add(body2[last_window])
                qtree.add(volume2[last_window])
            while first_window < wanted_first:
                vtree.remove(v2[first_window])
                btree.remove(body2[first_window])
                qtree.remove(volume2[first_window])
                first_window += 1
            candle = segment[index]
            if not start <= candle.open_time < end:
                continue
            observed += 1
            if index + 1 < required_window:
                statuses[break_status or "insufficient_history"] += 1
                continue
            baseline_v2 = vtree.mean(config.baseline_method)
            baseline_body = btree.mean(config.baseline_method)
            baseline_volume = qtree.mean(config.baseline_method)
            if baseline_v2 is None or baseline_v2 <= 0:
                statuses["zero_baseline"] += 1
                continue
            ready += 1
            statuses["ready"] += 1
            first, second = segment[index - 1], candle
            first_kind, second_kind = _kind(first, tick_size), _kind(second, tick_size)
            is_doji = "十字" in (first_kind, second_kind)
            pattern = "十字组合" if is_doji else first_kind + second_kind
            if ((is_doji and not config.allow_doji)
                    or (config.pattern_mode == "mixed_only" and pattern not in {"阴阳", "阳阴"})):
                continue
            ratio = v2[index] / baseline_v2
            if math.isclose(ratio, config.volatility_ratio_min, rel_tol=1e-12, abs_tol=0):
                ratio = config.volatility_ratio_min
            if ratio < config.volatility_ratio_min:
                continue
            previous = segment[index - 2]
            pair_range = max(first.high, second.high) - min(first.low, second.low)
            final_move = second.close - first.open
            neutral = abs(final_move) <= max(tick_size, pair_range * 0.02)
            direction = "neutral" if neutral else "up" if final_move > 0 else "down"
            current_body = body2[index]
            relative_volume = (volume2[index] / baseline_volume
                               if baseline_volume is not None and baseline_volume > 0 else None)
            body_ratio = (current_body / baseline_body
                          if baseline_body is not None and baseline_body > 0 else None)
            efficiency = min(1.0, current_body / v2[index]) if v2[index] > 0 else 0
            displacement = min(1.0, abs(final_move) / pair_range) if pair_range > 0 else 0
            quote_volume = volume2[index]
            taker_buy_ratio = min(1.0, (first.taker_buy_quote + second.taker_buy_quote) / quote_volume) if quote_volume > 0 else None
            aligned_ratio = None if taker_buy_ratio is None else (
                max(taker_buy_ratio, 1 - taker_buy_ratio) if neutral
                else taker_buy_ratio if direction == "up" else 1 - taker_buy_ratio)
            breakdown = {
                "volume": volume_score(relative_volume) if relative_volume is not None else 0,
                "structure": ((body_score(body_ratio) if body_ratio is not None else 0)
                              + efficiency_score(efficiency) + displacement_score(displacement)),
                "flow": flow_score(aligned_ratio, neutral) if aligned_ratio is not None else 0,
                "oi": 0, "liquidity": 0,
            }
            output.append({
                "symbol": second.symbol, "timeframe": second.timeframe,
                "position": segment_start + index,
                "segment_position": index,
                "signal": {
                    "id": f"{second.symbol}:{second.timeframe}:{second.close_time}:TWO_BAR_VOLATILITY",
                    "pattern": pattern, "direction": direction,
                    "open_time": first.open_time, "close_time": second.close_time,
                    "price": second.close, "volatility_ratio": ratio,
                    "relative_volume": relative_volume,
                    "score_breakdown": breakdown,
                },
                "score_before_optional": sum(breakdown.values()),
                "signal_candle": second,
                "prefix_start": max(segment_start,
                                    segment_start + index - full_history - 2),
            })
    return output, {"observed": observed, "ready": ready, "statuses": dict(statuses)}


def _liquidity_for_candidates(series: dict[str, tuple[array, array]],
                              symbols: list[str], onboard: dict[str, int],
                              delivery: dict[str, int],
                              targets: dict[int, dict[str, list[int]]],
                              candidates: list[Any], began: float,
                              progress_path: Path) -> dict[str, Any]:
    interval = TIMEFRAME_MS["5m"]
    peer_counts: list[int] = []
    incomplete = 0
    boundaries = sorted(targets)
    for boundary_index, boundary in enumerate(boundaries, 1):
        values: list[tuple[float, str, int, int]] = []
        for symbol in symbols:
            # A delivery contract's final candle closes at delivery-1.  At the
            # close boundary itself it still belongs to that candle's causal
            # peer set; exclude it only from later boundaries.
            if onboard.get(symbol, 0) >= boundary or delivery.get(symbol, 0) < boundary:
                continue
            times, cumulative = series.get(symbol, (array("q"), array("d", [0.0])))
            left = bisect_left(times, boundary - DAY)
            right = bisect_left(times, boundary)
            first_open = max(onboard.get(symbol, 0), boundary - DAY)
            if first_open % interval:
                first_open = (first_open // interval + 1) * interval
            expected = max(1, min(288, max(0, (boundary - first_open) // interval)))
            values.append((cumulative[right] - cumulative[left], symbol,
                           right - left, expected))
        values.sort(key=lambda item: (item[0], item[1]))
        denominator = len(values) - 1
        positions = {symbol: (position, volume, bars, expected)
                     for position, (volume, symbol, bars, expected) in enumerate(values)
                     if symbol in targets[boundary]}
        for symbol, indexes in targets[boundary].items():
            position, volume, bars, expected = positions[symbol]
            complete = bars >= expected
            incomplete += not complete
            rank = {
                "percentile": position / denominator if denominator > 0 else None,
                "rolling_24h_quote_volume": volume, "window_bars": bars,
                "expected_window_bars": expected, "window_complete": complete,
                "peer_count": len(values),
                "method": "native_5m rolling 24h cross-section; tie by symbol",
            }
            for candidate_index in indexes:
                target = candidates[candidate_index]
                if isinstance(target, _LightCandidate):
                    target.liquidity = rank
                else:
                    target["liquidity"] = rank
        peer_counts.append(len(values))
        if boundary_index % 250 == 0 or boundary_index == len(boundaries):
            _progress(progress_path, began, phase="liquidity_reconstruction",
                      boundaries_processed=boundary_index,
                      boundaries_total=len(boundaries), candidates=len(candidates))
    return {"boundary_count": len(boundaries),
            "peer_count_min": min(peer_counts) if peer_counts else 0,
            "peer_count_max": max(peer_counts) if peer_counts else 0,
            "incomplete_target_windows": incomplete,
            "peer_eligibility": "onboardDate < candle close boundary <= deliveryDate",
            "note": "Historical 24h ticker snapshots were not stored; native 5m quote-volume sums are causal. Exact historical status transitions or temporary halts are unavailable and are disclosed as a scope limitation."}


def _date_label(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, SHANGHAI).strftime("%Y-%m-%d")


def _listed_window(start: int, end: int, onboard: int, delivery: int,
                   interval: int) -> tuple[int, int, int]:
    """Return first open, exclusive last-open bound, and expected bar count."""
    first_open = max(start, onboard)
    last_open_bound = min(end, delivery)
    if first_open % interval:
        first_open = (first_open // interval + 1) * interval
    if first_open >= last_open_bound:
        return first_open, last_open_bound, 0
    # Count aligned opens ``first_open + n*interval < last_open_bound``.
    expected = (last_open_bound - first_open + interval - 1) // interval
    return first_open, last_open_bound, expected


def _parse_range(start_text: str, end_text: str) -> tuple[int, int]:
    start = datetime.strptime(start_text, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    end = datetime.strptime(end_text, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    if end <= start:
        raise ValueError("end date must be after start date")
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def _metadata_payloads(database: sqlite3.Connection, start: int, end: int,
                       scope_path: Path | None) -> tuple[dict[str, dict[str, Any]], str]:
    """Load range-intersecting USDT metadata from SQLite or an explicit file.

    Accepted explicit forms are a JSON list, ``{"symbols": [...]}``, or
    ``{"symbols": {"BTCUSDT": {...}}}``.  Items may be raw Binance exchange
    info payloads or wrappers with a ``payload`` object.
    """
    raw_items: list[Any] = []
    source = "database universe_symbols range intersection"
    if scope_path is not None:
        document = json.loads(scope_path.read_text(encoding="utf-8-sig"))
        raw = document.get("symbols", document) if isinstance(document, dict) else document
        if isinstance(raw, dict):
            raw_items = [{"symbol": symbol, **(item if isinstance(item, dict) else {})}
                         for symbol, item in raw.items()]
        elif isinstance(raw, list):
            raw_items = raw
        else:
            raise ValueError("scope metadata symbols must be a list or object")
        source = f"explicit scope metadata: {scope_path}"
    else:
        for row in database.execute("SELECT symbol,onboard_date,payload FROM universe_symbols ORDER BY symbol"):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            payload.setdefault("symbol", row["symbol"])
            payload.setdefault("onboardDate", row["onboard_date"])
            raw_items.append(payload)
    metadata: dict[str, dict[str, Any]] = {}
    for raw in raw_items:
        if isinstance(raw, str):
            payload = {"symbol": raw}
        elif isinstance(raw, dict):
            wrapped = raw.get("payload")
            payload = dict(wrapped) if isinstance(wrapped, dict) else dict(raw)
            if raw.get("symbol") and not payload.get("symbol"):
                payload["symbol"] = raw["symbol"]
        else:
            continue
        symbol = payload.get("symbol")
        if not isinstance(symbol, str) or not symbol:
            continue
        payload.setdefault("quoteAsset", payload.get("quote_asset"))
        payload.setdefault("onboardDate", payload.get("onboard_date"))
        payload.setdefault("deliveryDate", payload.get("delivery_date"))
        if payload.get("quoteAsset") != "USDT":
            continue
        onboard_date = int(payload.get("onboardDate") or 0)
        delivery_date = int(payload.get("deliveryDate") or 253402300799000)
        if onboard_date < end and delivery_date > start:
            metadata[symbol] = payload
    if not metadata:
        raise ValueError("scope metadata contains no range-intersecting USDT contracts")
    return metadata, source


def run(database_path: Path, output_path: Path, start_text: str = "2026-08-01",
        end_text: str = "2026-09-02", progress_path: Path | None = None,
        scope_path: Path | None = None) -> dict[str, Any]:
    began = time.monotonic()
    start, end = _parse_range(start_text, end_text)
    progress_path = progress_path or output_path.with_suffix(".progress.json")
    _progress(progress_path, began, phase="opening_readonly_snapshot",
              start=start_text, end_exclusive=end_text)
    database = _open_readonly(database_path)
    try:
        snapshot = dict(database.execute("""SELECT COUNT(*) AS candles,
            MIN(open_time) AS min_open_time,MAX(open_time) AS max_open_time FROM candles""").fetchone())
        snapshot["oi_rows"] = database.execute("SELECT COUNT(*) FROM open_interest").fetchone()[0]
        snapshot["opened_at_utc"] = datetime.now(timezone.utc).isoformat()
        metadata, scope_source = _metadata_payloads(database, start, end, scope_path)
        onboard = {symbol: int(payload.get("onboardDate") or 0)
                   for symbol, payload in metadata.items()}
        delivery = {symbol: int(payload.get("deliveryDate") or 253402300799000)
                    for symbol, payload in metadata.items()}
        symbols = sorted(metadata)
        saved_alerts: dict[str, dict[str, Any]] = {}
        for row in database.execute("""SELECT event_key,payload FROM alerts
            WHERE timeframe IN ('5m','15m') AND close_time>=? AND close_time<?""", (start, end)):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if payload.get("source", "live") == "live" and payload.get("synthetic") is not True:
                saved_alerts[row["event_key"]] = payload
        oi_start = min(start - LOOKBACK[timeframe]["days"] * DAY
                       - 2 * TIMEFRAME_MS[timeframe]
                       for timeframe in ("5m", "15m"))

        query_start = start - 7 * DAY - 2 * TIMEFRAME_MS["15m"]
        query_end = end + 5 * TIMEFRAME_MS["15m"]
        engine_config = EngineConfig(volatility_ratio_min=1.5, pattern_mode="all", allow_doji=False)
        indicator_config = IndicatorConfig(enabled_indicators=("supertrend", "smc"), confirmation_mode="all")
        candidates: list[Any] = []
        liquidity_series: dict[str, tuple[array, array]] = {}
        coverage_by_pair: dict[tuple[str, str], dict[str, Any]] = {}
        anomaly_counts = Counter()
        activity_counts = {threshold: Counter() for threshold in SENSITIVITY_THRESHOLDS}
        enrichment_counts = Counter()
        pruned_below_floor = Counter()
        pruned_max_lower_upper = Counter()
        processed_pairs = 0

        rows = database.execute("""SELECT * FROM candles
            WHERE timeframe IN ('5m','15m') AND closed=1
              AND open_time>=? AND open_time<?
            ORDER BY symbol,timeframe,open_time""", (query_start, query_end))
        current_key: tuple[str, str] | None = None
        group: list[Candle] = []
        # OI can be far larger than one candle series. Keep only one symbol's
        # rows resident; adjacent timeframe
        # groups reuse it because the candle query is ordered by symbol.
        oi_cache_symbol: str | None = None
        oi_cache_rows: list[dict[str, Any]] = []
        oi_cache_reconstructor = _OIReconstructor([])

        def consume(key: tuple[str, str] | None, candles: list[Candle]) -> None:
            nonlocal processed_pairs, oi_cache_symbol, oi_cache_rows, oi_cache_reconstructor
            if key is None or key[0] not in metadata:
                return
            symbol, timeframe = key
            interval = TIMEFRAME_MS[timeframe]
            listed_first = onboard.get(symbol, 0)
            if listed_first % interval:
                listed_first = (listed_first // interval + 1) * interval
            listed_last = delivery.get(symbol, 253402300799000)
            listed_candles = [candle for candle in candles
                              if listed_first <= candle.open_time < listed_last]
            if timeframe == "5m":
                liquidity_rows = [candle for candle in listed_candles
                                  if start - DAY <= candle.open_time < end]
                times = array("q", (candle.open_time for candle in liquidity_rows))
                cumulative = array("d", [0.0])
                running = 0.0
                for candle in liquidity_rows:
                    running += candle.quote_volume
                    cumulative.append(running)
                liquidity_series[symbol] = (times, cumulative)
            first_expected, last_expected, expected = _listed_window(
                start, end, onboard.get(symbol, 0), delivery.get(symbol, end), interval)
            found, stats = _series_candidates(
                listed_candles, first_expected, last_expected,
                _tick_size(metadata[symbol]), engine_config)
            by_open = {candle.open_time: candle for candle in listed_candles}
            times = [candle.open_time for candle in listed_candles]
            retained_for_oi: list[dict[str, Any]] = []
            for item in found:
                signal = item["signal"]
                anomaly_counts[timeframe] += 1
                anomaly_counts["all"] += 1
                saved = saved_alerts.get(signal["id"])
                exact_score = _finite_number(saved.get("activity_score")) if saved else None
                item["stored_exact"] = exact_score
                # Before touching OI, price/volume/flow plus the maximum OI,
                # percentile and spread contribution already prove that this
                # candidate cannot reach the lowest diagnostic threshold.
                if exact_score is None and item["score_before_optional"] + 25 < min(SENSITIVITY_THRESHOLDS):
                    pruned_below_floor[timeframe] += 1
                    pruned_below_floor["all"] += 1
                    pruned_max_lower_upper[timeframe] = max(
                        pruned_max_lower_upper[timeframe],
                        item["score_before_optional"] + 15 + 8)
                    enrichment_counts["anomaly_candidates"] += 1
                    enrichment_counts["pruned_before_oi_below_sensitivity_floor"] += 1
                    continue
                retained_for_oi.append(item)
            if retained_for_oi and oi_cache_symbol != symbol:
                oi_cache_symbol = symbol
                oi_cache_rows = [dict(row) for row in database.execute(
                    """SELECT * FROM open_interest WHERE symbol=?
                       AND timestamp>=? AND timestamp<=? ORDER BY timestamp""",
                    (symbol, oi_start, end))]
                oi_cache_reconstructor = _OIReconstructor(oi_cache_rows)
            oi_results = oi_cache_reconstructor.many(
                timeframe, (item["signal"]["close_time"] for item in retained_for_oi))
            for item in retained_for_oi:
                signal = item["signal"]
                exact_score = item["stored_exact"]
                oi = oi_results[signal["close_time"]]
                item["oi"] = oi
                item["score_before_liquidity"] = item["score_before_optional"] + (
                    oi_score(oi["oi_ratio"]) if oi["oi_ratio"] is not None and oi["oi_change_pct"] is not None else 0)
                item["signal"]["score_breakdown"]["oi"] = item["score_before_liquidity"] - item["score_before_optional"]
                direction, reversal = _pattern_direction(signal["pattern"])
                raw_valid = direction is not None and (reversal or signal.get("direction") == direction)
                item["raw_valid"] = raw_valid
                item["entry_direction"] = "long" if direction == "up" else "short"
                candle = item["signal_candle"]
                possible_score = exact_score if exact_score is not None else item["score_before_liquidity"] + 10
                # The lowest reported diagnostic threshold is 65.  Without a
                # stored exact score, a base+OI score whose best possible
                # percentile and spread still stays below 65 can never affect
                # an event, method result, or sensitivity bucket except
                # ``below``. Aggregate it now instead of retaining a large
                # nested Python object for the entire month.
                if exact_score is None and possible_score < min(SENSITIVITY_THRESHOLDS):
                    pruned_below_floor[timeframe] += 1
                    pruned_below_floor["all"] += 1
                    pruned_max_lower_upper[timeframe] = max(
                        pruned_max_lower_upper[timeframe],
                        item["score_before_liquidity"] + 8)
                    enrichment_counts["anomaly_candidates"] += 1
                    enrichment_counts["oi_evaluated_candidates"] += 1
                    enrichment_counts["oi_available"] += oi["oi_ratio"] is not None
                    enrichment_counts["pruned_before_liquidity_below_sensitivity_floor"] += 1
                    continue
                if possible_score < 78:
                    candidates.append(_LightCandidate(
                        symbol=symbol, timeframe=timeframe,
                        close_time=signal["close_time"],
                        score_before_liquidity=int(item["score_before_liquidity"]),
                        stored_exact=exact_score,
                        oi_available=oi["oi_ratio"] is not None,
                    ))
                    continue
                if possible_score >= 78:
                    item["forward"] = {str(horizon): _forward_return(
                        by_open, candle, direction, horizon, 5.0, 3.0) for horizon in HORIZONS}
                    position = bisect_left(times, candle.open_time)
                    full_window = LOOKBACK[timeframe]["days"] * DAY // interval + 3
                    prefix = listed_candles[max(0, position - full_window + 1):position + 1]
                    context = indicator_context(prefix, indicator_config)
                    flags, confirmations = _method_flags(
                        context, indicator_config, signal["pattern"], signal["close_time"], timeframe, raw_valid)
                    item["methods"] = flags
                    item["confirmations"] = confirmations
                    item["indicator_state"] = {
                        "supertrend": context.get("supertrend"),
                        "smc": {field: context.get("smc", {}).get(field)
                                for field in ("ready", "direction", "event", "event_direction", "event_time")},
                    }
                for transient in ("signal_candle", "position", "segment_position",
                                  "prefix_start", "raw_valid"):
                    item.pop(transient, None)
                candidates.append(item)
            range_candles = [candle for candle in listed_candles
                             if first_expected <= candle.open_time < last_expected]
            coverage_by_pair[key] = {
                "symbol": symbol, "timeframe": timeframe,
                "onboard_date": onboard.get(symbol), "expected_while_listed": expected,
                "observed_range_bars": len(range_candles),
                "missing_while_listed": max(0, expected - len(range_candles)),
                "first_range_open_time": range_candles[0].open_time if range_candles else None,
                "last_range_open_time": range_candles[-1].open_time if range_candles else None,
                "history_ready_range_bars": stats["ready"],
                "history_exclusions": stats["statuses"],
                "complete_while_listed": len(range_candles) >= expected,
            }
            processed_pairs += 1
            if processed_pairs % 50 == 0:
                _progress(progress_path, began, phase="single_pass_candle_scan",
                          pairs_processed=processed_pairs, pairs_total=len(symbols) * 2,
                          raw_anomalies=anomaly_counts["all"], candidates=len(candidates))

        for row in rows:
            key = (row["symbol"], row["timeframe"])
            if key != current_key:
                consume(current_key, group)
                current_key, group = key, []
            group.append(_row_candle(row))
        consume(current_key, group)

        # Emit explicit zero-observation coverage rows for active pairs absent
        # from the one-pass query instead of silently shrinking the universe.
        for timeframe in ("5m", "15m"):
            interval = TIMEFRAME_MS[timeframe]
            for symbol in symbols:
                key = (symbol, timeframe)
                if key in coverage_by_pair:
                    continue
                first_expected, last_expected, expected = _listed_window(
                    start, end, onboard.get(symbol, 0), delivery.get(symbol, end), interval)
                coverage_by_pair[key] = {
                    "symbol": symbol, "timeframe": timeframe,
                    "onboard_date": onboard.get(symbol), "expected_while_listed": expected,
                    "observed_range_bars": 0, "missing_while_listed": expected,
                    "first_range_open_time": None, "last_range_open_time": None,
                    "history_ready_range_bars": 0,
                    "history_exclusions": {"missing_series": expected},
                    "complete_while_listed": expected == 0,
                }

        targets: dict[int, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
        for index, item in enumerate(candidates):
            if isinstance(item, _LightCandidate):
                close_time, symbol = item.close_time, item.symbol
            else:
                close_time, symbol = item["signal"]["close_time"], item["symbol"]
            targets[close_time + 1][symbol].append(index)
        rank_audit = _liquidity_for_candidates(
            liquidity_series, symbols, onboard, delivery, targets, candidates, began, progress_path)
        compact_candidate_count = sum(isinstance(item, _LightCandidate) for item in candidates)
        full_candidate_count = len(candidates) - compact_candidate_count
        del targets
        del liquidity_series

        events: list[dict[str, Any]] = []
        max_scores: dict[str, dict[str, Any]] = {}
        exact_checks = Counter()
        daily_counts = Counter()
        for item in candidates:
            light = isinstance(item, _LightCandidate)
            if light:
                rank = item.liquidity or {}
                timeframe = item.timeframe
                symbol = item.symbol
                close_time = item.close_time
                score_before_liquidity = item.score_before_liquidity
                exact_score = item.stored_exact
                oi_available = item.oi_available
            else:
                rank = item.get("liquidity", {})
                timeframe = item["timeframe"]
                symbol = item["symbol"]
                close_time = item["signal"]["close_time"]
                score_before_liquidity = item["score_before_liquidity"]
                exact_score = item["stored_exact"]
                oi_available = item["oi"]["oi_ratio"] is not None
            percentile = rank.get("percentile")
            liquidity = liquidity_score(percentile, None)
            if not light:
                item["signal"]["score_breakdown"]["liquidity"] = liquidity
            lower = int(score_before_liquidity + liquidity)
            upper = min(100, lower + 2)
            if exact_score is not None:
                exact_checks["available"] += 1
                exact_checks["score_equal_to_lower"] += exact_score == lower
                exact_checks["inside_spread_bound"] += lower <= exact_score <= upper
                exact_checks["ge_80"] += exact_score >= 80
                exact_checks["max_score"] = max(exact_checks["max_score"], exact_score)
                primary = exact_score >= 80
                boundary = False
                status = "stored_exact_ge_80" if primary else "stored_exact_below_80"
            elif lower >= 80:
                primary, boundary, status = True, False, "reconstructed_lower_bound_ge_80"
            elif upper >= 80:
                primary, boundary, status = False, True, "unknown_spread_boundary_78_79"
            else:
                primary, boundary, status = False, False, "reconstructed_upper_bound_below_80"
            for threshold in SENSITIVITY_THRESHOLDS:
                if exact_score is not None:
                    bucket = "confirmed" if exact_score >= threshold else "below"
                elif lower >= threshold:
                    bucket = "confirmed"
                elif upper >= threshold:
                    bucket = "boundary"
                else:
                    bucket = "below"
                activity_counts[threshold][(timeframe, bucket)] += 1
                activity_counts[threshold][("all", bucket)] += 1
            comparable = exact_score if exact_score is not None else lower
            if timeframe not in max_scores or comparable > max_scores[timeframe]["comparison_score"]:
                max_scores[timeframe] = {
                    "symbol": symbol, "close_time": close_time,
                    "comparison_score": comparable, "reconstructed_lower": lower,
                    "reconstructed_upper": upper, "stored_exact": exact_score,
                }
            enrichment_counts["anomaly_candidates"] += 1
            enrichment_counts["oi_evaluated_candidates"] += 1
            enrichment_counts["oi_available"] += oi_available
            enrichment_counts["liquidity_percentile_available"] += percentile is not None
            enrichment_counts["liquidity_window_complete"] += rank.get("window_complete") is True
            if not (primary or boundary):
                continue
            if light:
                raise RuntimeError("score-only candidate unexpectedly reached activity 78")
            # Any 78/79 candidate necessarily paid for the indicator context in
            # consume(); assert this invariant rather than silently fabricating.
            if "methods" not in item or "forward" not in item:
                raise RuntimeError("indicator context missing for retained event")
            date = _date_label(item["signal"]["close_time"])
            daily_counts[(date, item["timeframe"], "primary" if primary else "boundary")] += 1
            signal = item["signal"]
            events.append({
                "event_key": signal["id"], "symbol": item["symbol"],
                "timeframe": item["timeframe"], "pattern": signal["pattern"],
                "signal_direction": signal["direction"],
                "entry_direction": item["entry_direction"],
                "signal_open_time": signal["open_time"],
                "signal_close_time": signal["close_time"],
                "signal_close_price": signal["price"],
                "volatility_ratio": signal["volatility_ratio"],
                "relative_volume": signal["relative_volume"],
                "activity_score": {
                    "reconstructed_lower_no_spread": lower,
                    "reconstructed_upper_best_spread": upper,
                    "stored_live_exact": exact_score, "status": status,
                    "score_breakdown_lower": signal["score_breakdown"],
                    "oi": item["oi"], "liquidity": rank,
                },
                "activity_80": {"primary": primary,
                                "boundary_unknown_spread": boundary},
                "methods": item["methods"], "confirmations": item["confirmations"],
                "indicator_state": item["indicator_state"],
                "execution": {"entry": "next bar open with adverse slippage",
                              "fee_bps_per_side": 5.0, "slippage_bps_per_side": 3.0},
                "forward": item["forward"],
            })

        for threshold in SENSITIVITY_THRESHOLDS:
            for timeframe in ("5m", "15m", "all"):
                activity_counts[threshold][(timeframe, "below")] += pruned_below_floor[timeframe]
        for timeframe in ("5m", "15m"):
            pruned_upper = pruned_max_lower_upper[timeframe]
            if timeframe in max_scores:
                max_scores[timeframe]["pruned_below_floor_max_possible_lower"] = pruned_upper
                max_scores[timeframe]["proven_global"] = (
                    max_scores[timeframe]["comparison_score"] >= pruned_upper)
            elif pruned_below_floor[timeframe]:
                max_scores[timeframe] = {
                    "comparison_score": None,
                    "pruned_below_floor_max_possible_lower": pruned_upper,
                    "proven_global": False,
                    "status": "exact maximum intentionally unresolved below sensitivity floor",
                }

        sensitivity = []
        for threshold in SENSITIVITY_THRESHOLDS:
            for timeframe in ("5m", "15m", "all"):
                counts = activity_counts[threshold]
                sensitivity.append({
                    "threshold": threshold, "diagnostic_only": threshold != 80,
                    "timeframe": timeframe,
                    "confirmed": counts[(timeframe, "confirmed")],
                    "unknown_spread_boundary": counts[(timeframe, "boundary")],
                    "below": counts[(timeframe, "below")],
                })
        coverage = [coverage_by_pair[key] for key in sorted(coverage_by_pair)]
        excluded = [row for row in coverage
                    if row["missing_while_listed"] or row["history_ready_range_bars"] < row["observed_range_bars"]]
        report = {
            "schema_version": 1,
            "analysis": {
                "range_start": start_text, "range_end_exclusive": end_text,
                "timezone": "Asia/Shanghai (UTC+8)", "start_ms": start,
                "end_exclusive_ms": end,
                "scope": "Binance USD-M quoteAsset=USDT contracts whose onboard/delivery interval intersects the requested range; every contract type and asset class",
                "scope_source": scope_source,
                "timeframes": ["5m", "15m"], "activity_primary_threshold": 80,
                "activity_sensitivity_thresholds_diagnostic_only": [65, 70, 75],
                "anomaly_threshold": 1.5,
                "patterns": ["阳阳", "阴阴", "阴阳", "阳阴"],
                "method_order": [{"id": method, "label": label} for method, label in METHODS],
                "indicator_config": asdict(indicator_config),
                "execution": {"entry": "next closed period open",
                              "outcomes": "entry bar and subsequent 2/3/5-bar closes; independent of exits",
                              "fee_bps_per_side": 5.0, "slippage_bps_per_side": 3.0,
                              "win": "direction-adjusted net return after costs > 0"},
                "activity_bound": "spread history is absent: lower uses spread score 0; upper adds maximum 2 points; saved live score takes precedence.",
                "optimization": "one ordered candle query, exact Fenwick rolling trimmed means, batched 5m-aligned OI, one-symbol OI residency, compact score-only candidates, one range-wide liquidity reconstruction",
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.monotonic() - began, 3),
            },
            "database_snapshot": snapshot,
            "universe": {
                "symbols": len(symbols), "symbol_list": symbols,
                "contract_type_counts": dict(sorted(Counter(
                    str(item.get("contractType")) for item in metadata.values()).items())),
                "underlying_type_counts": dict(sorted(Counter(
                    str(item.get("underlyingType")) for item in metadata.values()).items())),
                "tradfi_symbols": sum(contract_metadata_fields(item)["is_tradfi"]
                                       for item in metadata.values()),
            },
            "coverage": {
                "pairs": coverage, "pairs_with_any_exclusion": excluded,
                "summary": [{
                    "timeframe": timeframe,
                    "symbols": sum(row["timeframe"] == timeframe for row in coverage),
                    "expected_while_listed": sum(row["expected_while_listed"] for row in coverage if row["timeframe"] == timeframe),
                    "observed_range_bars": sum(row["observed_range_bars"] for row in coverage if row["timeframe"] == timeframe),
                    "missing_while_listed": sum(row["missing_while_listed"] for row in coverage if row["timeframe"] == timeframe),
                    "complete_pairs": sum(row["timeframe"] == timeframe and row["complete_while_listed"] for row in coverage),
                } for timeframe in ("5m", "15m")],
                "liquidity_reconstruction": rank_audit,
            },
            "candidate_funnel": {
                "anomaly_events": {key: anomaly_counts[key] for key in ("5m", "15m", "all")},
                "activity_sensitivity": sensitivity,
                "max_scores": max_scores,
                "retained_primary_events": sum(event["activity_80"]["primary"] for event in events),
                "retained_boundary_events": sum(event["activity_80"]["boundary_unknown_spread"] for event in events),
                "daily_counts": [{"date": key[0], "timeframe": key[1], "scope": key[2], "events": count}
                                 for key, count in sorted(daily_counts.items())],
            },
            "events": events,
            "summary": {
                "confirmed_activity_ge_80": _summary_rows(events, False),
                "possible_including_unknown_spread": _summary_rows(events, True),
            },
            "enrichment_audit": {"counts": dict(enrichment_counts),
                                 "saved_exact_checks": dict(exact_checks),
                                 "memory_policy": {
                                     "sensitivity_floor": min(SENSITIVITY_THRESHOLDS),
                                     "pruned_below_floor": dict(pruned_below_floor),
                                     "retained_score_candidates": len(candidates),
                                     "compact_score_only_candidates": compact_candidate_count,
                                     "full_candidates_with_event_payload": full_candidate_count,
                                     "rule": "candidates whose best possible remaining OI/percentile/spread score stays below the sensitivity floor are aggregated, not retained",
                                 }},
            "limitations": [
                "Only locally persisted closed candles are used; absent August history is reported as missing and never fabricated.",
                "Historical spread snapshots are absent. Reconstructed score is a lower bound; 78/79 rows remain a separate unknown-spread boundary.",
                "Historical OI is used only when source-time-aligned samples satisfy the production baseline; otherwise OI scores zero.",
                "The latest locally persisted universe table defines known contracts. Previously observed inactive rows are included when onboard/delivery intersects the range; contracts delisted before this installation ever observed them are absent.",
                "Historical status transitions are not stored. Onboard and delivery timestamps bound participation; temporary exchange halts cannot be reconstructed from metadata alone.",
                "Win rates are descriptive, cost-aware forward outcomes and are not a promise or an execution recommendation.",
            ],
            "self_checks": {
                "unique_event_keys": len({event["event_key"] for event in events}) == len(events),
                "all_entries_after_signal": all(
                    not outcome["available"] or outcome["entry_time"] > event["signal_close_time"]
                    for event in events for outcome in event["forward"].values()),
                "all_events_are_primary_or_boundary": all(
                    event["activity_80"]["primary"] or event["activity_80"]["boundary_unknown_spread"]
                    for event in events),
                "all_anomalies_accounted_after_memory_pruning": (
                    enrichment_counts["anomaly_candidates"] == anomaly_counts["all"]),
                "all_sensitivity_buckets_match_anomalies": all(
                    sum(activity_counts[threshold][(timeframe, bucket)]
                        for bucket in ("confirmed", "boundary", "below"))
                    == anomaly_counts[timeframe]
                    for threshold in SENSITIVITY_THRESHOLDS
                    for timeframe in ("5m", "15m", "all")),
                "summary_rows_per_scope": len(_summary_rows(events, False)),
            },
            "reproduction": {
                "command": (f"python -m monitor.range_analysis --database {database_path} "
                            f"--start {start_text} --end {end_text} --output {output_path}"
                            + (f" --scope-metadata {scope_path}" if scope_path else "")),
                "read_only": True,
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_suffix(output_path.suffix + ".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2,
                                        allow_nan=False), encoding="utf-8")
        temporary.replace(output_path)
        _progress(progress_path, began, phase="complete", output=str(output_path),
                  raw_anomalies=anomaly_counts["all"], retained_events=len(events),
                  primary_events=sum(event["activity_80"]["primary"] for event in events),
                  boundary_events=sum(event["activity_80"]["boundary_unknown_spread"] for event in events))
        return report
    finally:
        database.rollback()
        database.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="data/pulse.sqlite3")
    parser.add_argument("--start", default="2026-08-01")
    parser.add_argument("--end", default="2026-09-02")
    parser.add_argument("--output", default="data/range-analysis/2026-08-01_to_2026-09-02.analysis.json")
    parser.add_argument("--progress", default=None)
    parser.add_argument("--scope-metadata", default=None)
    arguments = parser.parse_args()
    report = run(Path(arguments.database), Path(arguments.output), arguments.start,
                 arguments.end, Path(arguments.progress) if arguments.progress else None,
                 Path(arguments.scope_metadata) if arguments.scope_metadata else None)
    print(json.dumps({
        "output": arguments.output,
        "elapsed_seconds": report["analysis"]["elapsed_seconds"],
        "anomalies": report["candidate_funnel"]["anomaly_events"],
        "primary_events": report["candidate_funnel"]["retained_primary_events"],
        "boundary_events": report["candidate_funnel"]["retained_boundary_events"],
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
