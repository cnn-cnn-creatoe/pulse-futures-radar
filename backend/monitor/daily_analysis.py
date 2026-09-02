"""Read-only, causal one-day cross-sectional signal study.

The command-line entry point replays every locally available Binance USD-M
USDT contract from closed candles.  It never writes to SQLite and never uses a
present-day market snapshot as historical enrichment.  Saved live alerts are
only an exact-score cross-check for timestamps that were actually persisted.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import argparse
import json
import math
from pathlib import Path
import sqlite3
from statistics import fmean
import time
from typing import Any, Iterable

from .backtest import _causal_anomaly_prefilter
from .engine import Candle, EngineConfig, LOOKBACK, TIMEFRAME_MS, analyze
from .indicators import IndicatorConfig, _confirmation, indicator_context
from .store import contract_metadata_fields


DAY = 86_400_000
SHANGHAI = timezone(timedelta(hours=8))
METHODS = (
    ("two_bar", "仅两根K线"),
    ("supertrend", "仅超级趋势"),
    ("smc", "仅SMC"),
    ("supertrend_smc", "超级趋势+SMC全部满足"),
)
HORIZONS = (1, 2, 3, 5)
SENSITIVITY_THRESHOLDS = (65, 70, 75, 80)


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _open_readonly(path: Path) -> sqlite3.Connection:
    database = sqlite3.connect(
        f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=60)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    database.execute("PRAGMA busy_timeout=60000")
    database.execute("BEGIN")
    return database


def _tick_size(metadata: dict[str, Any]) -> float:
    for item in metadata.get("filters", []):
        if item.get("filterType") == "PRICE_FILTER":
            value = _finite_number(float(item.get("tickSize", 0)))
            if value is not None and value > 0:
                return value
    return 1e-8


def _row_candle(row: sqlite3.Row) -> Candle:
    values = dict(row)
    values["closed"] = bool(values["closed"])
    return Candle(**values)


def _pattern_direction(pattern: str) -> tuple[str | None, bool]:
    reversal = pattern in {"阴阳", "阳阴"}
    if pattern in {"阳阳", "阴阳"}:
        return "up", reversal
    if pattern in {"阴阴", "阳阴"}:
        return "down", reversal
    return None, reversal


def _forward_return(candles_by_open: dict[int, Candle], signal: Candle,
                    direction: str, horizon: int, fee_bps: float = 5,
                    slippage_bps: float = 3) -> dict[str, Any]:
    """Same adverse next-open/mark and two-side fee formula as backtest._execute."""
    interval = TIMEFRAME_MS[signal.timeframe]
    side = 1 if direction == "up" else -1
    entry_bar = candles_by_open.get(signal.open_time + interval)
    mark_bar = candles_by_open.get(signal.open_time + horizon * interval)
    if entry_bar is None:
        return {"available": False, "reason": "next_open_missing",
                "entry_time": signal.open_time + interval,
                "mark_close_time": signal.close_time + horizon * interval}
    if mark_bar is None:
        return {"available": False, "reason": "horizon_close_missing",
                "entry_time": entry_bar.open_time,
                "entry_raw_open": entry_bar.open,
                "mark_close_time": signal.close_time + horizon * interval}
    slip = slippage_bps / 10_000
    fee = fee_bps / 10_000
    entry = entry_bar.open * (1 + side * slip)
    mark = mark_bar.close * (1 - side * slip)
    net = (side * (mark - entry) / entry - fee * (entry + mark) / entry) * 100
    return {"available": True, "entry_time": entry_bar.open_time,
            "entry_raw_open": entry_bar.open, "entry_after_slippage": entry,
            "mark_close_time": mark_bar.close_time, "mark_raw_close": mark_bar.close,
            "mark_after_slippage": mark, "net_return_pct": net,
            "win": net > 0}


def reconstruct_oi(rows: list[dict[str, Any]], timeframe: str,
                   close_time: int, future_slack_ms: int = 5_000) -> dict[str, Any]:
    """Collector.oi_enrichment reproduced against already-read rows."""
    interval = TIMEFRAME_MS[timeframe]
    duration = interval * 2
    end = close_time + 1
    start = end - duration
    # ``rows`` is often a range-wide, already-read superset.  Production loads
    # a fresh rolling window for every signal; constrain the superset here so a
    # later range date cannot acquire an ever-growing OI baseline.
    window_start = end - LOOKBACK[timeframe]["days"] * DAY - duration
    missing = {"oi_change_pct": None, "oi_ratio": None, "oi_sample_count": 0}
    by_source: dict[int, dict[str, Any]] = {}
    for row in rows:
        source_time = row.get("source_time")
        timestamp = row.get("timestamp")
        if (isinstance(source_time, int) and not isinstance(source_time, bool)
                and source_time > 0 and isinstance(timestamp, int)
                and window_start <= timestamp <= end
                and -future_slack_ms <= timestamp - source_time < 75_000):
            by_source.setdefault(source_time, row)
    times = sorted(by_source)
    observed = [by_source[stamp] for stamp in times]
    if len(observed) < 33:
        return missing

    def value(boundary: int) -> dict[str, Any] | None:
        index = bisect_right(times, boundary) - 1
        while index >= 0 and observed[index]["timestamp"] > boundary:
            index -= 1
        if index < 0 or boundary - times[index] > 75_000:
            return None
        return observed[index]

    def change(boundary: int) -> float | None:
        first, last = value(boundary - duration), value(boundary)
        if not first or not last or first["open_interest_quantity"] <= 0:
            return None
        if abs((last["source_time"] - first["source_time"]) - duration) > 20_000:
            return None
        return (last["open_interest_quantity"] /
                first["open_interest_quantity"] - 1) * 100

    current = change(end)
    if current is None:
        return missing
    history: list[float] = []
    boundary = start
    earliest = times[0] + duration
    while boundary >= earliest:
        item = change(boundary)
        if item is not None:
            history.append(abs(item))
        boundary -= interval
    if len(history) < 30:
        return {**missing, "oi_sample_count": len(history)}
    baseline = sum(history) / len(history)
    if baseline <= 0:
        return {**missing, "oi_sample_count": len(history)}
    return {"oi_change_pct": current, "oi_ratio": abs(current) / baseline,
            "oi_sample_count": len(history)}


def _liquidity_ranks(database: sqlite3.Connection, symbols: list[str],
                     onboard: dict[str, int], start: int, end: int) -> tuple[dict[int, dict[str, dict]], dict]:
    """Reconstruct aligned rolling-24h quote-volume ranks from native 5m bars."""
    interval = TIMEFRAME_MS["5m"]
    boundaries = list(range(start + interval, end + 1, interval))
    rows_at: dict[int, list[tuple[str, float, int, int]]] = {
        boundary: [] for boundary in boundaries}
    sql = f"""SELECT open_time,quote_volume FROM candles
              WHERE symbol=? AND timeframe='5m' AND closed=1
                AND open_time>=? AND open_time<? ORDER BY open_time"""
    for index, symbol in enumerate(symbols, 1):
        rows = database.execute(sql, (symbol, start - DAY, end)).fetchall()
        times = [int(row["open_time"]) for row in rows]
        cumulative = [0.0]
        for row in rows:
            cumulative.append(cumulative[-1] + float(row["quote_volume"]))
        for boundary in boundaries:
            if onboard.get(symbol, 0) >= boundary:
                continue
            left = bisect_left(times, boundary - DAY)
            right = bisect_left(times, boundary)
            rows_at[boundary].append((symbol, cumulative[right] - cumulative[left],
                                      right - left,
                                      max(1, min(288, math.ceil((boundary - max(onboard.get(symbol, 0), boundary - DAY)) / interval)))))
        if index % 100 == 0:
            print(f"liquidity {index}/{len(symbols)}", flush=True)
    ranks: dict[int, dict[str, dict]] = {}
    incomplete = 0
    peer_counts = []
    for boundary, values in rows_at.items():
        ordered = sorted(values, key=lambda item: (item[1], item[0]))
        denominator = len(ordered) - 1
        mapping = {}
        for position, (symbol, volume, bars, expected) in enumerate(ordered):
            complete = bars >= expected
            incomplete += not complete
            mapping[symbol] = {"percentile": position / denominator if denominator > 0 else None,
                               "rolling_24h_quote_volume": volume,
                               "window_bars": bars, "expected_window_bars": expected,
                               "window_complete": complete, "peer_count": len(ordered),
                               "method": "native_5m rolling 24h cross-section; tie by symbol"}
        ranks[boundary] = mapping
        peer_counts.append(len(ordered))
    return ranks, {"boundary_count": len(boundaries),
                   "peer_count_min": min(peer_counts) if peer_counts else 0,
                   "peer_count_max": max(peer_counts) if peer_counts else 0,
                   "incomplete_symbol_windows": incomplete,
                   "note": "Historical 24h ticker snapshots were not stored; native 5m quote-volume sums are a causal reconstruction."}


def _method_flags(context: dict[str, Any], config: IndicatorConfig,
                  pattern: str, close_time: int, timeframe: str,
                  raw_valid: bool) -> tuple[dict[str, bool], dict[str, dict]]:
    direction, reversal = _pattern_direction(pattern)
    if direction is None or context.get("status") != "ready":
        return {method: method == "two_bar" and raw_valid for method, _ in METHODS}, {}
    interval = TIMEFRAME_MS[timeframe]
    confirmations = {
        name: _confirmation(name, context, direction, reversal, close_time, interval, config)
        for name in ("supertrend", "smc")
    }
    st = raw_valid and confirmations["supertrend"]["passed"]
    smc = raw_valid and confirmations["smc"]["passed"]
    return {"two_bar": raw_valid, "supertrend": st, "smc": smc,
            "supertrend_smc": st and smc}, confirmations


def _summary_rows(events: list[dict[str, Any]], include_boundary: bool) -> list[dict[str, Any]]:
    rows = []
    selected_events = [event for event in events
                       if event["activity_80"]["primary"] or
                       (include_boundary and event["activity_80"]["boundary_unknown_spread"])]
    for timeframe in ("5m", "15m"):
        for method, label in METHODS:
            for direction in ("long", "short"):
                chosen = [event for event in selected_events
                          if event["timeframe"] == timeframe
                          and event["entry_direction"] == direction
                          and event["methods"][method]]
                for horizon in HORIZONS:
                    outcomes = [event["forward"][str(horizon)] for event in chosen
                                if event["forward"][str(horizon)]["available"]]
                    values = [item["net_return_pct"] for item in outcomes]
                    rows.append({"scope": "possible_including_unknown_spread" if include_boundary else "confirmed_activity_ge_80",
                                 "timeframe": timeframe, "method": method,
                                 "method_label": label, "direction": direction,
                                 "horizon_bars": horizon,
                                 "selected_signals": len(chosen),
                                 "available_outcomes": len(values),
                                 "wins": sum(value > 0 for value in values),
                                 "win_rate": (sum(value > 0 for value in values) / len(values)
                                              if values else None),
                                 "average_net_return_pct": fmean(values) if values else None})
    return rows


def run(database_path: Path, output_path: Path, date_text: str = "2026-09-01") -> dict[str, Any]:
    began = time.monotonic()
    date = datetime.strptime(date_text, "%Y-%m-%d").replace(tzinfo=SHANGHAI)
    day_start = int(date.timestamp() * 1000)
    day_end = day_start + DAY
    database = _open_readonly(database_path)
    try:
        snapshot = dict(database.execute("""SELECT COUNT(*) AS candles,
            MIN(open_time) AS min_open_time,MAX(open_time) AS max_open_time FROM candles""").fetchone())
        snapshot["oi_rows"] = database.execute("SELECT COUNT(*) FROM open_interest").fetchone()[0]
        snapshot["opened_at_utc"] = datetime.now(timezone.utc).isoformat()
        metadata: dict[str, dict[str, Any]] = {}
        onboard: dict[str, int] = {}
        for row in database.execute("""SELECT symbol,onboard_date,payload FROM universe_symbols
            WHERE active=1 AND status='TRADING'
              AND json_extract(payload,'$.quoteAsset')='USDT' ORDER BY symbol"""):
            metadata[row["symbol"]] = json.loads(row["payload"])
            onboard[row["symbol"]] = int(row["onboard_date"] or 0)
        symbols = sorted(metadata)
        saved_alerts = {}
        for row in database.execute("""SELECT event_key,payload FROM alerts
            WHERE timeframe IN ('5m','15m') AND close_time>=? AND close_time<?""",
                                    (day_start, day_end)):
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if payload.get("source", "live") == "live" and payload.get("synthetic") is not True:
                saved_alerts[row["event_key"]] = payload
        ranks, rank_audit = _liquidity_ranks(database, symbols, onboard, day_start, day_end)

        indicator_config = IndicatorConfig(enabled_indicators=("supertrend", "smc"), confirmation_mode="all")
        engine_config = EngineConfig(volatility_ratio_min=1.5, pattern_mode="all", allow_doji=False)
        fee_bps, slippage_bps = 5.0, 3.0
        coverage = []
        excluded = []
        events: list[dict[str, Any]] = []
        anomaly_counts = Counter()
        activity_counts = {threshold: Counter() for threshold in SENSITIVITY_THRESHOLDS}
        max_scores: dict[str, dict] = {}
        exact_checks = Counter()
        enrichment_counts = Counter()
        status_counts = Counter()

        for timeframe in ("5m", "15m"):
            interval = TIMEFRAME_MS[timeframe]
            full_history = LOOKBACK[timeframe]["days"] * DAY // interval
            full_window = full_history + 3
            minimum_window = LOOKBACK[timeframe]["minimum_bars"] + 3
            expected_nominal = DAY // interval
            for symbol_index, symbol in enumerate(symbols, 1):
                query_start = day_start - (full_history + 2) * interval
                query_end = day_end + 5 * interval
                candle_rows = database.execute("""SELECT * FROM candles
                    WHERE symbol=? AND timeframe=? AND closed=1
                      AND open_time>=? AND open_time<=? ORDER BY open_time""",
                    (symbol, timeframe, query_start, query_end)).fetchall()
                candles = [_row_candle(row) for row in candle_rows]
                by_open = {candle.open_time: candle for candle in candles}
                day_candles = [candle for candle in candles
                               if day_start <= candle.open_time < day_end]
                first_expected = max(day_start, onboard.get(symbol, 0))
                if first_expected % interval:
                    first_expected = (first_expected // interval + 1) * interval
                expected_tradable = max(0, (day_end - first_expected) // interval)
                missing_day = max(0, expected_tradable - len(day_candles))
                times = [candle.open_time for candle in candles]
                gap_prefix = [0]
                for pos in range(1, len(times)):
                    gap_prefix.append(gap_prefix[-1] + (times[pos] != times[pos - 1] + interval))

                def prefix_status(pos: int) -> str:
                    start_pos = max(0, pos - full_window + 1)
                    length = pos - start_pos + 1
                    if length < minimum_window:
                        return "insufficient_history"
                    gaps = gap_prefix[pos] - gap_prefix[start_pos]
                    return "gap_in_lookback" if gaps else "ready"

                ready_day = 0
                pair_status = Counter()
                # Prefilter each contiguous segment. Marks are authoritative only
                # after prefix_status confirms the production window is gap-free.
                marks_by_time: dict[int, str] = {}
                segment_start = 0
                for pos in range(1, len(candles) + 1):
                    boundary = pos == len(candles) or candles[pos].open_time != candles[pos - 1].open_time + interval
                    if not boundary:
                        continue
                    segment = candles[segment_start:pos]
                    if len(segment) >= minimum_window + 1:
                        marks = _causal_anomaly_prefilter(segment, engine_config, minimum_window - 1)
                        marks_by_time.update({segment[index].open_time: value for index, value in marks.items()})
                    segment_start = pos

                oi_rows = [dict(row) for row in database.execute("""SELECT * FROM open_interest
                    WHERE symbol=? AND timestamp>=? AND timestamp<=? ORDER BY timestamp""",
                    (symbol, day_start - LOOKBACK[timeframe]["days"] * DAY - 2 * interval,
                     day_end + interval)).fetchall()]
                for candle in day_candles:
                    pos = bisect_left(times, candle.open_time)
                    state = prefix_status(pos)
                    pair_status[state] += 1
                    status_counts[(timeframe, state)] += 1
                    if state != "ready":
                        continue
                    ready_day += 1
                    if marks_by_time.get(candle.open_time) != "analyze":
                        continue
                    prefix = candles[max(0, pos - full_window + 1):pos + 1]
                    boundary = candle.close_time + 1
                    rank = ranks.get(boundary, {}).get(symbol, {})
                    oi = reconstruct_oi(oi_rows, timeframe, candle.close_time)
                    enrichment = {"liquidity_percentile": rank.get("percentile"),
                                  "spread_bps": None,
                                  "oi_change_pct": oi["oi_change_pct"],
                                  "oi_ratio": oi["oi_ratio"]}
                    analyzed = analyze(prefix, engine_config, enrichment,
                                       _tick_size(metadata[symbol]))
                    signal = analyzed.get("signal")
                    if signal is None:
                        continue
                    anomaly_counts[timeframe] += 1
                    anomaly_counts["all"] += 1
                    enrichment_counts["anomaly_candidates"] += 1
                    enrichment_counts["oi_available"] += oi["oi_ratio"] is not None
                    enrichment_counts["liquidity_percentile_available"] += rank.get("percentile") is not None
                    enrichment_counts["liquidity_window_complete"] += rank.get("window_complete") is True
                    lower = int(signal["activity_score"])
                    upper = min(100, lower + 2)
                    event_key = signal["id"]
                    saved = saved_alerts.get(event_key)
                    exact_score = (_finite_number(saved.get("activity_score"))
                                   if saved else None)
                    if exact_score is not None:
                        exact_checks["available"] += 1
                        exact_checks["score_equal_to_lower"] += exact_score == lower
                        exact_checks["inside_spread_bound"] += lower <= exact_score <= upper
                        exact_checks["ge_80"] += exact_score >= 80
                        exact_checks["max_score"] = max(exact_checks["max_score"], exact_score)
                    if exact_score is not None:
                        primary_80 = exact_score >= 80
                        boundary_80 = False
                        activity_status = ("stored_exact_ge_80" if primary_80
                                           else "stored_exact_below_80")
                    elif lower >= 80:
                        primary_80 = True
                        boundary_80 = False
                        activity_status = "reconstructed_lower_bound_ge_80"
                    elif upper >= 80:
                        primary_80 = False
                        boundary_80 = True
                        activity_status = "unknown_spread_boundary_78_79"
                    else:
                        primary_80 = boundary_80 = False
                        activity_status = "reconstructed_upper_bound_below_80"
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
                    current_max = max_scores.get(timeframe)
                    comparable = exact_score if exact_score is not None else lower
                    if current_max is None or comparable > current_max["comparison_score"]:
                        max_scores[timeframe] = {"symbol": symbol,
                            "close_time": candle.close_time,
                            "comparison_score": comparable,
                            "reconstructed_lower": lower,
                            "reconstructed_upper": upper,
                            "stored_exact": exact_score}
                    if not (primary_80 or boundary_80):
                        continue
                    direction, reversal = _pattern_direction(signal["pattern"])
                    raw_valid = direction is not None and (reversal or signal.get("direction") == direction)
                    context = indicator_context(prefix, indicator_config)
                    flags, confirmations = _method_flags(
                        context, indicator_config, signal["pattern"],
                        candle.close_time, timeframe, raw_valid)
                    forward = {str(horizon): _forward_return(
                        by_open, candle, direction, horizon, fee_bps, slippage_bps)
                        for horizon in HORIZONS}
                    events.append({
                        "event_key": event_key, "symbol": symbol,
                        "timeframe": timeframe, "pattern": signal["pattern"],
                        "signal_direction": signal.get("direction"),
                        "entry_direction": "long" if direction == "up" else "short",
                        "signal_open_time": signal["open_time"],
                        "signal_close_time": signal["close_time"],
                        "signal_close_price": signal["price"],
                        "volatility_ratio": signal["volatility_ratio"],
                        "relative_volume": signal.get("relative_volume"),
                        "activity_score": {
                            "reconstructed_lower_no_spread": lower,
                            "reconstructed_upper_best_spread": upper,
                            "stored_live_exact": exact_score,
                            "status": activity_status,
                            "score_breakdown_lower": signal["score_breakdown"],
                            "oi": oi, "liquidity": rank,
                        },
                        "activity_80": {"primary": primary_80,
                                        "boundary_unknown_spread": boundary_80},
                        "methods": flags, "confirmations": confirmations,
                        "indicator_state": {"supertrend": context.get("supertrend"),
                                            "smc": {key: context.get("smc", {}).get(key)
                                                    for key in ("ready", "direction", "event", "event_direction", "event_time")}},
                        "execution": {"entry": "next bar open with adverse slippage",
                                      "fee_bps_per_side": fee_bps,
                                      "slippage_bps_per_side": slippage_bps},
                        "forward": forward,
                    })
                row = {"symbol": symbol, "timeframe": timeframe,
                       "onboard_date": onboard.get(symbol),
                       "nominal_day_bars": expected_nominal,
                       "expected_while_listed": expected_tradable,
                       "observed_day_bars": len(day_candles),
                       "missing_while_listed": missing_day,
                       "first_day_open_time": day_candles[0].open_time if day_candles else None,
                       "last_day_open_time": day_candles[-1].open_time if day_candles else None,
                       "history_ready_day_bars": ready_day,
                       "history_exclusions": dict(pair_status),
                       "complete_while_listed": missing_day == 0}
                coverage.append(row)
                if missing_day or ready_day < len(day_candles):
                    excluded.append(row)
                if symbol_index % 50 == 0:
                    print(f"replay {timeframe} {symbol_index}/{len(symbols)} anomalies={anomaly_counts[timeframe]}", flush=True)

        sensitivity = []
        for threshold in SENSITIVITY_THRESHOLDS:
            for timeframe in ("5m", "15m", "all"):
                counts = activity_counts[threshold]
                sensitivity.append({"threshold": threshold,
                    "diagnostic_only": threshold != 80,
                    "timeframe": timeframe,
                    "confirmed": counts[(timeframe, "confirmed")],
                    "unknown_spread_boundary": counts[(timeframe, "boundary")],
                    "below": counts[(timeframe, "below")]})
        report = {
            "schema_version": 1,
            "analysis": {
                "date": date_text, "timezone": "Asia/Shanghai (UTC+8)",
                "start_ms": day_start, "end_exclusive_ms": day_end,
                "scope": "active TRADING Binance USD-M contracts with quoteAsset=USDT; every contract type and asset class",
                "timeframes": ["5m", "15m"], "activity_primary_threshold": 80,
                "activity_sensitivity_thresholds_diagnostic_only": [65, 70, 75],
                "anomaly_threshold": 1.5,
                "patterns": ["阳阳", "阴阴", "阴阳", "阳阴"],
                "method_order": [{"id": method, "label": label} for method, label in METHODS],
                "indicator_config": asdict(indicator_config),
                "execution": {"entry": "next closed period open",
                    "outcomes": "entry bar and subsequent 2/3/5-bar closes; independent of exits",
                    "fee_bps_per_side": fee_bps,
                    "slippage_bps_per_side": slippage_bps,
                    "win": "direction-adjusted net return after costs > 0"},
                "activity_bound": "spread history is absent: lower uses spread score 0; upper adds the maximum 2 points. Saved live score is exact where present and takes precedence.",
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
                "summary": [{"timeframe": timeframe,
                    "symbols": sum(row["timeframe"] == timeframe for row in coverage),
                    "expected_while_listed": sum(row["expected_while_listed"] for row in coverage if row["timeframe"] == timeframe),
                    "observed_day_bars": sum(row["observed_day_bars"] for row in coverage if row["timeframe"] == timeframe),
                    "complete_while_listed_symbols": sum(row["complete_while_listed"] for row in coverage if row["timeframe"] == timeframe),
                    "history_ready_day_bars": sum(row["history_ready_day_bars"] for row in coverage if row["timeframe"] == timeframe)}
                    for timeframe in ("5m", "15m")],
                "status_counts": [{"timeframe": key[0], "status": key[1], "bars": value}
                                  for key, value in sorted(status_counts.items())],
                "liquidity_reconstruction": rank_audit,
            },
            "candidate_funnel": {
                "anomaly_events": {key: anomaly_counts[key] for key in ("5m", "15m", "all")},
                "activity_sensitivity": sensitivity,
                "maximum_scores": max_scores,
                "saved_live_score_checks": dict(exact_checks),
                "enrichment_coverage": {
                    **dict(enrichment_counts),
                    "oi_missing": enrichment_counts["anomaly_candidates"] - enrichment_counts["oi_available"],
                    "saved_live_score_missing": anomaly_counts["all"] - exact_checks["available"],
                },
                "events_at_primary_or_boundary_80": len(events),
                "primary_ge_80": sum(event["activity_80"]["primary"] for event in events),
                "boundary_unknown_spread": sum(event["activity_80"]["boundary_unknown_spread"] for event in events),
            },
            "events": events,
            "summary": {
                "confirmed_activity_ge_80": _summary_rows(events, False),
                "possible_including_unknown_spread": _summary_rows(events, True),
            },
            "limitations": [
                "Historical bid/ask spread snapshots do not exist; 78/79 lower scores are not promoted into the >=80 primary result.",
                "Historical 24h ticker snapshots do not exist; liquidity percentile is reconstructed causally from native 5m rolling quote volume.",
                "OI is reconstructed with Collector.oi_enrichment boundary/freshness/sample rules; unavailable OI scores zero.",
                "Current metadata is used for the active USDT universe; onboard_date removes pre-listing bars. Delisted contracts absent from the current active snapshot are outside scope.",
                "No funding, leverage, liquidation, account sizing, or order-book fill model is included.",
                "This is a one-day diagnostic, not evidence of a stable win rate or a trading recommendation.",
            ],
            "self_checks": {
                "database_open_mode": "SQLite mode=ro + query_only transaction",
                "candles_are_primary_source": True,
                "saved_alerts_used_only_for_exact_score": True,
                "future_data_in_signal_or_indicators": False,
                "entry_is_after_signal_close": all(
                    not item["available"] or item["entry_time"] > event["signal_close_time"]
                    for event in events for item in event["forward"].values()),
                "json_finite": True,
            },
            "reproduction": {
                "script": str(Path(__file__).resolve()),
                "working_directory": str(Path.cwd().resolve()),
                "command": (f"python -m monitor.daily_analysis --database {database_path} "
                            f"--date {date_text} --output {output_path}"),
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        output_path.write_text(content, encoding="utf-8")
        return report
    finally:
        database.close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only one-day USDT futures replay")
    parser.add_argument("--database", default="data/pulse.sqlite3")
    parser.add_argument("--date", default="2026-09-01")
    parser.add_argument("--output", default="data/daily-analysis/2026-09-01.analysis.json")
    args = parser.parse_args(list(argv) if argv is not None else None)
    report = run(Path(args.database), Path(args.output), args.date)
    print(json.dumps({"output": str(Path(args.output).resolve()),
                      "symbols": report["universe"]["symbols"],
                      "anomalies": report["candidate_funnel"]["anomaly_events"],
                      "primary_ge_80": report["candidate_funnel"]["primary_ge_80"],
                      "boundary_unknown_spread": report["candidate_funnel"]["boundary_unknown_spread"]},
                     ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
