"""Chronological, cost-aware research replay; never places or promises trades.

Signals see closed prefixes only. Entry is the next candle's open, stops win an
ambiguous OHLC bar, and training outcomes are purged before the holdout split.
The public kline ablation intentionally cannot validate historical OI or spread.
"""
from __future__ import annotations

from bisect import bisect_left, insort
from collections import Counter, deque
from dataclasses import asdict, replace
import math
from statistics import fmean
from typing import Any, Callable

from .engine import Candle, EngineConfig, LOOKBACK, TIMEFRAME_MS, _kind, _ntr, analyze
from .history_data import validate_series
from .indicators import (IndicatorConfig, _evaluate_with_context, evaluate_candidates,
                         validate_quality_config)

THRESHOLD_GRID = (1.5, 2.0, 2.5, 3.0, 4.0)
TIMEFRAME_PRIORS = {"1m": 3.0, "5m": 2.5, "15m": 2.0, "30m": 2.0,
                    "1h": 2.0, "2h": 1.8, "1d": 1.5}
ENRICHMENT_FIELDS = ("oi_change_pct", "oi_ratio", "liquidity_percentile", "spread_bps")
INDICATOR_STUDY_COMBINATIONS = (
    ("wt_extreme_cross", ("wt_extreme_cross",), "WaveTrend · 超买超卖交叉"),
)
INDICATOR_STUDY_WARNINGS = [
    "Primary rows are a Kline-only indicator ablation, not the configured live full policy; strict results remain under each row.policies.strict_full_policy.",
    "The 70/30 holdout and three anchored walk-forward folds are chronological diagnostics; a future unseen period is still required before deployment.",
    "Deflated Sharpe Ratio (DSR) and Probability of Backtest Overfitting (PBO) have not been calculated.",
    "WaveTrend results cannot be used to select a trading strategy without further unseen validation.",
    "All rows retain the same support/resistance and net risk/reward screen; only indicator confirmations differ.",
]


def _options(options: dict | None, quality: dict) -> dict:
    values = {"max_holding_bars": 5, "compare_thresholds": True,
              "fee_bps_per_side": quality.get("fee_bps_per_side", 5),
              "slippage_bps_per_side": quality.get("slippage_bps_per_side", 3),
              "min_validation_trades": 30, "threshold_profile": "manual"}
    options = options or {}
    if set(options) - set(values):
        raise ValueError("unknown backtest option: " + ", ".join(sorted(set(options) - set(values))))
    values.update(options)
    for key, lower, upper in (("max_holding_bars", 1, 100), ("min_validation_trades", 30, 1000)):
        value = values[key]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{key} must be an integer between {lower} and {upper}")
    for key in ("fee_bps_per_side", "slippage_bps_per_side"):
        value = values[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 100:
            raise ValueError(f"{key} must be finite in [0, 100]")
    if type(values["compare_thresholds"]) is not bool:
        raise ValueError("compare_thresholds must be boolean")
    if values["threshold_profile"] not in {"manual", "timeframe"}:
        raise ValueError("threshold_profile must be manual or timeframe")
    return values


def _historical_enrichment(raw: dict | None, close_time: int) -> dict:
    """Only an explicitly timestamped same-close snapshot may affect the past."""
    if not isinstance(raw, dict):
        return {}
    stamp = raw.get("observed_at", raw.get("timestamp"))
    if type(stamp) is not int or stamp > close_time or stamp < 0:
        return {}
    # An optional period marker must exactly match this signal's period.
    if raw.get("close_time", close_time) != close_time:
        return {}
    clean = {}
    bounds = {"oi_change_pct": (-100, None), "oi_ratio": (0, None),
              "liquidity_percentile": (0, 1), "spread_bps": (0, None)}
    for key, (minimum, maximum) in bounds.items():
        value = raw.get(key)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value) or value < minimum or (maximum is not None and value > maximum)):
            continue
        clean[key] = value
    return clean


def _causal_anomaly_prefilter(candles: list[Candle], config: EngineConfig,
                              warmup: int) -> dict[int, str]:
    """Classify bars that provably cannot pass the engine's anomaly gate.

    It maintains the exact causal V2 sample window with a sorted multiset.  A
    value marked ``analyze`` still goes through the authoritative ``analyze``
    function; the fast path only skips values strictly below the same gate.
    This removes repeated construction/sorting of 7-day windows in year-long
    research without changing any accepted signal.
    """
    if len(candles) < 3:
        return {}
    timeframe = candles[0].timeframe
    full_history = LOOKBACK[timeframe]["days"] * 86_400_000 // TIMEFRAME_MS[timeframe]
    ntrs = [0.0] * len(candles)
    for index in range(1, len(candles)):
        ntrs[index] = _ntr(candles[index], candles[index - 1])
    v2 = [0.0] * len(candles)
    for index in range(2, len(candles)):
        v2[index] = ntrs[index - 1] + ntrs[index]
    ordered, window = [], deque()
    total = 0.0
    result = {}
    for index in range(warmup, len(candles) - 1):
        wanted_first, wanted_last = max(2, index - full_history), index - 2
        while not window or window[-1][0] < wanted_last:
            next_index = wanted_first if not window else window[-1][0] + 1
            value = v2[next_index]
            window.append((next_index, value))
            insort(ordered, value)
            total += value
        while window and window[0][0] < wanted_first:
            _, value = window.popleft()
            position = bisect_left(ordered, value)
            ordered.pop(position)
            total -= value
        if not ordered:
            result[index] = "insufficient_history"
            continue
        if config.baseline_method == "simple_mean":
            baseline = total / len(ordered)
        else:
            trim = math.floor(len(ordered) * config.trim_percent / 100)
            trimmed_total = total
            if trim:
                trimmed_total -= sum(ordered[:trim]) + sum(ordered[-trim:])
            baseline = trimmed_total / (len(ordered) - 2 * trim)
        if baseline <= 0:
            result[index] = "zero_baseline"
            continue
        first_kind = _kind(candles[index - 1], 1e-8)
        second_kind = _kind(candles[index], 1e-8)
        is_doji = "十字" in (first_kind, second_kind)
        pattern = "十字组合" if is_doji else first_kind + second_kind
        if ((is_doji and not config.allow_doji)
                or (config.pattern_mode == "mixed_only" and pattern not in {"阴阳", "阳阴"})):
            result[index] = "pattern_filtered"
            continue
        current = v2[index]
        # Conservative tolerance can create a harmless full-analysis false
        # positive, never a skipped near-threshold signal.
        result[index] = ("analyze" if current >= baseline * config.volatility_ratio_min * (1 - 1e-12)
                         else "ready")
    return result


def _metrics(trades: list[dict]) -> dict:
    returns = [trade["net_return_pct"] for trade in trades]
    positive = sum(value for value in returns if value > 0)
    negative = -sum(value for value in returns if value < 0)
    equity = peak = 1.0
    drawdown = 0.0
    for value in returns:
        # Hypothetical sequential fixed 1x notional, one series only; no leverage.
        equity *= max(0, 1 + value / 100)
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100)
    return {"trades": len(trades), "wins": sum(value > 0 for value in returns),
            "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
            "expectancy_net_pct": fmean(returns) if returns else None,
            "profit_factor": positive / negative if negative > 0 else None,
            "profit_factor_note": "no losing closed trades; ratio undefined" if returns and negative == 0 else None,
            "max_drawdown_pct": drawdown if returns else None,
            "hypothetical_fixed_1x_return_pct": (equity - 1) * 100 if returns else None,
            "equity_basis": "sequential single-series 1x notional, full reinvestment; not account performance",
            "average_holding_bars": fmean(t["holding_bars"] for t in trades) if trades else None,
            "average_mfe_pct": fmean(t["mfe_pct"] for t in trades) if trades else None,
            "average_mae_pct": fmean(t["mae_pct"] for t in trades) if trades else None,
            "fees_pct_sum": sum(t["fee_pct"] for t in trades),
            "slippage_bps_per_side": trades[0]["slippage_bps_per_side"] if trades else None}


def _execute(candles: list[Candle], candidate: dict, options: dict) -> dict | None:
    """Simulate one already-known candidate; do not use any later signal state."""
    signal_index = candidate["index"]
    entry_index = signal_index + 1
    if entry_index >= len(candles):
        return None
    plan = candidate["trade_plan"]
    direction = plan.get("direction")
    if direction not in {"long", "short"}:
        return None
    side = 1 if direction == "long" else -1
    stop, target = plan.get("stop_loss"), plan.get("target")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in (stop, target)):
        return None
    slip = options["slippage_bps_per_side"] / 10000
    fee = options["fee_bps_per_side"] / 10000
    raw_entry = candles[entry_index].open
    entry = raw_entry * (1 + side * slip)
    # If the next open already invalidated either bound, do not invent a fill.
    if (side == 1 and not stop < entry < target) or (side == -1 and not target < entry < stop):
        return None
    last_index = min(len(candles) - 1, entry_index + options["max_holding_bars"] - 1)
    exit_index = last_index
    raw_exit = candles[last_index].close
    reason = "max_holding" if last_index == entry_index + options["max_holding_bars"] - 1 else "end_of_data"
    for i in range(entry_index, last_index + 1):
        candle = candles[i]
        if side == 1:
            stop_hit, target_hit = candle.low <= stop, candle.high >= target
            gap_stop = candle.open <= stop
        else:
            stop_hit, target_hit = candle.high >= stop, candle.low <= target
            gap_stop = candle.open >= stop
        if stop_hit:
            raw_exit = candle.open if gap_stop else stop
            reason = "stop_first_ambiguous" if target_hit and not gap_stop else "stop_loss"
        elif target_hit:
            # Even a favorable target gap gets no optimistic improvement.
            raw_exit = target
            reason = "target"
        else:
            continue
        exit_index = i
        break
    exit_price = raw_exit * (1 - side * slip)
    gross = side * (exit_price - entry) / entry * 100
    fees_pct = fee * (entry + exit_price) / entry * 100
    held = candles[entry_index:exit_index + 1]
    favorable = max(c.high for c in held) if side == 1 else min(c.low for c in held)
    adverse = min(c.low for c in held) if side == 1 else max(c.high for c in held)
    horizons = {}
    for horizon in (1, 2, 3, 5):
        index = entry_index + horizon - 1
        if index < len(candles):
            mark = candles[index].close * (1 - side * slip)
            horizons[str(horizon)] = (side * (mark - entry) / entry - fee * (entry + mark) / entry) * 100
        else:
            horizons[str(horizon)] = None
    return {"signal_close_time": candles[signal_index].close_time,
            "entry_time": candles[entry_index].open_time, "exit_time": candles[exit_index].close_time,
            "entry_index": entry_index, "exit_index": exit_index,
            "pattern": candidate["pattern"], "setup": candidate.get("setup"), "direction": direction,
            "volatility_ratio": candidate["volatility_ratio"], "activity_score": candidate["activity_score"],
            "entry_price": entry, "exit_price": exit_price, "stop_loss": stop, "target": target,
            "exit_reason": reason, "holding_bars": exit_index - entry_index + 1,
            "gross_return_after_slippage_pct": gross, "fee_pct": fees_pct,
            "net_return_pct": gross - fees_pct, "slippage_bps_per_side": options["slippage_bps_per_side"],
            "mfe_pct": max(0, side * (favorable - entry) / entry * 100),
            "mae_pct": min(0, side * (adverse - entry) / entry * 100),
            "excursion_basis": "whole holding OHLC bars; exit-bar path unavailable, descriptive bound only",
            "forward_returns_net_pct": horizons,
            "forward_returns_basis": "independent 1/2/3/5-bar close outcomes, ignoring strategy exit"}


def _simulate(candles: list[Candle], candidates: list[dict], threshold: float,
              start: int, stop: int, options: dict, *, include_trades: bool = True,
              outcome_end: int | None = None) -> dict:
    selected = [c for c in candidates if start <= c["index"] < stop and c["volatility_ratio"] + 1e-12 >= threshold]
    counts = Counter({"candidate_signals": len(selected), "qualified_candidates": 0,
                      "excluded_quality": 0, "excluded_missing_data": 0,
                      "excluded_overlap": 0, "excluded_entry_gap": 0, "closed_trades": 0})
    reasons = Counter()
    trades = []
    busy_until = -1
    for candidate in selected:
        if not candidate["qualified"]:
            counts["excluded_quality"] += 1
            counts["excluded_missing_data"] += bool(candidate["missing_required"])
            reasons.update(candidate["quality_reasons"])
            continue
        counts["qualified_candidates"] += 1
        if candidate["index"] < busy_until:
            counts["excluded_overlap"] += 1
            continue
        trade = _execute(candles, candidate, options)
        if trade is None:
            counts["excluded_entry_gap"] += 1
            continue
        # A training/holdout subset must never contain a trade exiting outside it.
        boundary = stop if outcome_end is None else outcome_end
        if trade["exit_index"] >= boundary:
            counts["excluded_boundary"] += 1
            continue
        # Diagnostic horizons also cannot borrow holdout prices in training.
        for horizon in (1, 2, 3, 5):
            if trade["entry_index"] + horizon - 1 >= boundary:
                trade["forward_returns_net_pct"][str(horizon)] = None
        trades.append(trade)
        busy_until = trade["exit_index"]
    counts["closed_trades"] = len(trades)
    by_pattern = {pattern: _metrics([trade for trade in trades if trade["pattern"] == pattern])
                  for pattern in ("阳阳", "阴阴", "阴阳", "阳阴", "十字组合")}
    result = {"threshold": threshold, "metrics": _metrics(trades), "sample_counts": dict(counts),
              "exclusion_reasons": dict(reasons), "pattern_breakdown": by_pattern}
    if include_trades:
        result["trades"] = trades
    return result


def _recommend(comparisons: list[dict], minimum: int, timeframe: str) -> dict:
    eligible = [row for row in comparisons if row["train"]["metrics"]["trades"] >= minimum]
    if not eligible:
        return {"status": "insufficient_sample", "selected_threshold": None, "validated": False,
                "prior_threshold": TIMEFRAME_PRIORS[timeframe], "prior_is_empirical": False,
                "reason": f"no threshold has {minimum} closed training trades; timeframe prior is unvalidated"}
    selected = max(eligible, key=lambda row: (row["train"]["metrics"]["expectancy_net_pct"], row["threshold"]))
    holdout = selected["holdout"]["metrics"]
    enough = holdout["trades"] >= minimum
    positive = enough and holdout["expectancy_net_pct"] > 0
    return {"status": "heldout_positive_not_guaranteed" if positive else "holdout_not_profitable" if enough else "insufficient_holdout",
            "selected_threshold": selected["threshold"], "selection_basis": "highest training mean net return; tie selects higher threshold",
            "holdout_used_for_selection": False, "validated": bool(positive),
            "train": selected["train"]["metrics"], "holdout": holdout,
            "minimum_trades_each_partition": minimum, "prior_threshold": TIMEFRAME_PRIORS[timeframe],
            "prior_is_empirical": False,
            "reason": "single chronological split; further unseen periods and symbols required" if positive
            else f"selected on training only; holdout requires at least {minimum} trades and positive net expectancy"}


def _study_partition(result: dict, minimum: int) -> dict:
    """Attach an explicit sample verdict without changing replay metrics."""
    trades = result["metrics"]["trades"]
    return {**result, "sample_sufficient": trades >= minimum,
            "sample_status": "sufficient" if trades >= minimum else "insufficient_sample",
            "minimum_closed_trades": minimum}


def _study_audit(options: dict) -> dict:
    return {"trial_count": len(INDICATOR_STUDY_COMBINATIONS),
            "predeclared_combinations": True, "selection_performed": False,
            "validation_status": "chronological_holdout_and_anchored_walk_forward",
            "minimum_closed_trades_per_segment": options["min_validation_trades"],
            "best_combination": None, "warnings": list(INDICATOR_STUDY_WARNINGS)}


def _walk_forward(candles: list[Candle], candidates: list[dict], threshold: float,
                  warmup: int, options: dict, *, folds: int = 3,
                  include_trades: bool = True) -> dict:
    """Run non-overlapping, anchored out-of-sample folds on causal candidates.

    The first half of the usable history is the initial training window.  Each
    validation fold is the next sixth of the data; training expands up to the
    fold boundary and is purged by the maximum holding period.  No fold is used
    to change the predeclared indicator combination or threshold.
    """
    minimum = options["min_validation_trades"]
    usable = max(0, len(candles) - warmup)
    if not candles or usable <= options["max_holding_bars"] + folds:
        empty = _study_partition(
            _simulate([], [], threshold, 0, 0, options, include_trades=include_trades), minimum)
        return {"status": "insufficient_data", "method": "anchored_expanding_window",
                "fold_count": folds, "folds": [], "metrics": empty["metrics"],
                "trades": [] if include_trades else None,
                "sample_sufficient": False, "sample_sufficient_all_folds": False,
                "minimum_closed_trades_per_fold": minimum,
                "minimum_total_closed_trades": minimum * folds}
    first_validation = warmup + usable // 2
    remaining = len(candles) - first_validation
    boundaries = [first_validation + math.floor(remaining * i / folds) for i in range(folds + 1)]
    rows, all_trades = [], []
    for fold_index in range(folds):
        validation_start, validation_stop = boundaries[fold_index], boundaries[fold_index + 1]
        train_stop = max(warmup, validation_start - options["max_holding_bars"])
        train = _study_partition(
            _simulate(candles, candidates, threshold, warmup, train_stop, options,
                      include_trades=False, outcome_end=validation_start), minimum)
        validation = _study_partition(
            _simulate(candles, candidates, threshold, validation_start, validation_stop,
                      options, include_trades=include_trades), minimum)
        fold_trades = validation.get("trades", [])
        all_trades.extend(fold_trades)
        rows.append({"fold": fold_index + 1,
                     "train_start_index": warmup, "train_end_exclusive": train_stop,
                     "validation_start_index": validation_start,
                     "validation_end_exclusive": validation_stop,
                     "validation_start_time": candles[validation_start].open_time,
                     "validation_end_time": candles[validation_stop - 1].close_time,
                     "train": train, "validation": validation})
    enough_all = all(row["validation"]["sample_sufficient"] for row in rows)
    return {"status": "completed", "method": "anchored_expanding_window",
            "fold_count": folds, "folds": rows, "metrics": _metrics(all_trades),
            "trades": all_trades if include_trades else None,
            "sample_sufficient": enough_all,
            "sample_sufficient_all_folds": enough_all,
            "minimum_closed_trades_per_fold": minimum,
            "minimum_total_closed_trades": minimum * folds,
            "selection_note": "fixed predeclared combination and threshold; folds are not ranked or tuned"}


def _empty_indicator_study(options: dict, threshold: float | None = None) -> dict:
    partition = _study_partition(_simulate([], [], threshold or 0, 0, 0, options, include_trades=False),
                                 options["min_validation_trades"])
    rows = []
    for study_id, enabled, label in INDICATOR_STUDY_COMBINATIONS:
        policies = {name: {"train": partition, "holdout": partition,
                           "walk_forward": _walk_forward([], [], threshold or 0, 0, options,
                                                         include_trades=False),
                           "sample_sufficient_both": False}
                    for name in ("strict_full_policy", "price_only_ablation")}
        rows.append({"id": study_id, "label": label, "enabled_indicators": list(enabled),
                     "combination_size": len(enabled), "confirmation_mode": "all",
                     "evaluation_policy": "price_only_ablation", "train": partition,
                     "holdout": partition, "sample_sufficient_both": False, "policies": policies})
    return {"status": "insufficient_data", "methodology_version": 2, "threshold": threshold,
            "primary_policy": "price_only_ablation", "rows": rows,
            **_study_audit(options)}


def _build_indicator_study(candles: list[Candle], candidates: dict[str, dict[str, list[dict]]],
                           threshold: float, warmup: int, train_end: int, boundary: int,
                           options: dict, strict_quality: dict, ablation_quality: dict) -> dict:
    """Compare predeclared confirmation sets on one identical chronological split."""
    minimum = options["min_validation_trades"]
    rows = []
    for study_id, enabled, label in INDICATOR_STUDY_COMBINATIONS:
        policies = {}
        for policy_name in ("strict_full_policy", "price_only_ablation"):
            source = candidates[study_id][policy_name]
            keep_trades = True
            train = _study_partition(_simulate(candles, source, threshold, warmup, train_end, options,
                                                include_trades=keep_trades, outcome_end=boundary), minimum)
            holdout = _study_partition(_simulate(candles, source, threshold, boundary, len(candles), options,
                                                  include_trades=keep_trades), minimum)
            walk_forward = _walk_forward(candles, source, threshold, warmup, options,
                                         include_trades=keep_trades)
            policies[policy_name] = {"train": train, "holdout": holdout,
                                     "walk_forward": walk_forward,
                                     "sample_sufficient_both": train["sample_sufficient"] and holdout["sample_sufficient"]}
        primary = policies["price_only_ablation"]
        rows.append({"id": study_id, "label": label, "enabled_indicators": list(enabled),
                     "combination_size": len(enabled), "confirmation_mode": "all",
                     "evaluation_policy": "price_only_ablation", "train": primary["train"],
                     "holdout": primary["holdout"], "walk_forward": primary["walk_forward"],
                     "sample_sufficient_both": primary["sample_sufficient_both"], "policies": policies})
    return {"status": "completed", "methodology_version": 2, "threshold": threshold,
            "primary_policy": "price_only_ablation", "rows": rows,
            **_study_audit(options),
            "quality": {"strict_full_policy": strict_quality, "price_only_ablation": ablation_quality},
            "execution_assumptions": {
                "signal_data": "closed prefix through signal close only",
                "entry": "next bar open with adverse per-side slippage",
                "exit": "confirmed support/resistance plan; stop first when stop and target share an OHLC bar",
                "trade_plan_basis": "same confirmed SMC support/resistance and cost-adjusted risk/reward for every indicator row",
                "confirmation_rule": "all selected indicators must pass; only the confirmation set changes",
                "fee_bps_per_side": options["fee_bps_per_side"],
                "slippage_bps_per_side": options["slippage_bps_per_side"],
                "max_holding_bars": options["max_holding_bars"],
                "split": "chronological 70/30 with max-holding purge plus three anchored expanding-window validation folds; validation never selects a combination"}}


def run_backtest(candles: list[Candle], engine_config: EngineConfig | None = None,
                 indicator_config: IndicatorConfig | None = None, quality: dict | None = None,
                 options: dict | None = None, enrichment_by_close: dict | None = None,
                 progress: Callable[[dict], None] | None = None) -> dict[str, Any]:
    """Replay one continuous symbol/timeframe and compare a predeclared grid.

    Historical enrichment is keyed by exact signal close time and must carry
    observed_at <= that close. Current snapshots must never be passed as history.
    Root/API should run this bounded CPU task outside the event loop.
    """
    config = engine_config or EngineConfig()
    indicator_config = indicator_config or IndicatorConfig()
    config.__post_init__()
    indicator_config.__post_init__()
    effective_quality = validate_quality_config(quality)
    opts = _options(options, effective_quality)
    effective_quality.update({key: opts[key] for key in ("fee_bps_per_side", "slippage_bps_per_side")})
    dataset = validate_series(candles)
    limitations = [
        "Research simulation only; no order execution or profit guarantee.",
        "One symbol/timeframe; no historical full-universe reconstruction. Delisted contracts, survivorship and new-listing alert delivery are not validated.",
        "Klines alone cannot validate historical OI, market-wide liquidity rank, spread, funding fees or order-book fills. No current snapshot is backfilled.",
        "Fixed per-side fee/slippage assumptions; no leverage/liquidation/account sizing; funding fees excluded and may change results.",
        "OHLC intrabar ordering is unknown: stop wins if both bounds touch. MFE/MAE include the entire exit bar and are descriptive bounds.",
        "Timeframe defaults are unvalidated priors, not backtest-optimized thresholds.",
    ]
    if not candles:
        return {"status": "insufficient_data", "dataset": dataset, "options": opts,
                "strategy": {}, "limitations": limitations, "recommendation": None,
                "indicator_study": _empty_indicator_study(opts)}
    if dataset["gaps"]:
        raise ValueError("backtest requires continuous history; gaps must be repaired from verified historical data")
    timeframe = candles[0].timeframe
    required = LOOKBACK[timeframe]["minimum_bars"] + 3
    # The first possible signal index has the engine's full minimum history.
    warmup = required - 1
    boundary = max(warmup, math.floor(len(candles) * .7))
    train_end = max(warmup, boundary - opts["max_holding_bars"])
    dataset.update({"warmup_bars": warmup, "minimum_bars_for_signal": required,
                    "train_start_index": warmup, "train_end_exclusive": train_end,
                    "holdout_start_index": boundary, "holdout_start_time": candles[boundary].open_time if boundary < len(candles) else None,
                    "purged_signal_bars": max(0, boundary - train_end), "split": "chronological 70/30"})
    current_threshold = max(config.volatility_ratio_min, TIMEFRAME_PRIORS[timeframe]) if opts["threshold_profile"] == "timeframe" else config.volatility_ratio_min
    result = {"status": "completed" if len(candles) >= required + opts["max_holding_bars"] + 1 else "insufficient_data",
              "methodology_version": 2,
              "dataset": dataset, "options": opts, "engine_config": asdict(config),
              "indicator_config": asdict(indicator_config), "quality": effective_quality,
              "active_threshold": current_threshold, "strategy": {}, "limitations": limitations}
    if result["status"] != "completed":
        result["recommendation"] = {"status": "insufficient_sample", "validated": False,
                                    "prior_threshold": TIMEFRAME_PRIORS[timeframe], "prior_is_empirical": False}
        result["indicator_study"] = _empty_indicator_study(opts, current_threshold)
        return result
    ablation_quality = {**effective_quality, "require_oi": False,
                        "allow_missing_liquidity": True, "min_activity_score": 0}
    # Lowering score is explicit ablation, never a 100-point reconstructed score.
    candidates: dict[str, list[dict]] = {"strict_full_policy": [], "price_only_ablation": []}
    study_candidates = {study_id: {"strict_full_policy": [], "price_only_ablation": []}
                        for study_id, _, _ in INDICATOR_STUDY_COMBINATIONS}
    analyzer = replace(config, volatility_ratio_min=min(1.5, current_threshold))
    statuses = Counter()
    complete_enrichment = 0
    full_window = LOOKBACK[timeframe]["days"] * 86_400_000 // TIMEFRAME_MS[timeframe] + 3
    # Tests and external callers may replace ``analyze`` with another callable;
    # in that case every prefix remains observable and no engine-specific fast
    # path is applied.
    prefilter = (_causal_anomaly_prefilter(candles, analyzer, warmup)
                 if getattr(analyze, "__module__", "") == "monitor.engine" else None)
    replay_total = max(0, len(candles) - 1 - warmup)
    progress_step = max(1, replay_total // 100)
    for index in range(warmup, len(candles) - 1):
        processed = index - warmup
        if progress is not None and (processed == 0 or processed % progress_step == 0):
            progress({"stage": "causal_replay", "processed_bars": processed,
                      "total_bars": replay_total,
                      "progress": processed / replay_total if replay_total else 1.0})
        close_time = candles[index].close_time
        raw_enrichment = (enrichment_by_close or {}).get(close_time, (enrichment_by_close or {}).get(str(close_time)))
        historical = _historical_enrichment(raw_enrichment, close_time)
        complete_enrichment += all(key in historical for key in ENRICHMENT_FIELDS)
        if prefilter is not None and prefilter.get(index) != "analyze":
            statuses[prefilter.get(index, "ready")] += 1
            continue
        # Same bounded closed window as live engine; never any future candle.
        prefix = candles[max(0, index - full_window + 1):index + 1]
        analyzed = analyze(prefix, analyzer, historical)
        statuses[analyzed["status"]] += 1
        signal = analyzed.get("signal")
        if not signal:
            continue
        policies = (("strict_full_policy", effective_quality), ("price_only_ablation", ablation_quality))
        evaluations = evaluate_candidates(signal, prefix, indicator_config, [policy for _, policy in policies])
        for (name, policy), evaluated in zip(policies, evaluations):
            missing = ((not policy.get("allow_missing_liquidity", False)
                        and (signal.get("liquidity_percentile") is None or signal.get("spread_bps") is None))
                       or (policy.get("require_oi", False) and (signal.get("oi_ratio") is None or signal.get("oi_change_pct") is None)))
            candidates[name].append({"index": index, "pattern": signal["pattern"],
                "setup": evaluated.get("setup"), "volatility_ratio": signal["volatility_ratio"],
                "activity_score": signal["activity_score"], "qualified": evaluated["qualified"] and not missing,
                "trade_plan": evaluated.get("trade_plan"), "missing_required": missing,
                "quality_reasons": evaluated.get("quality_reasons", [])})
        # Each study row sees this exact same closed prefix and base anomaly.
        # Only enabled confirmations change; support/resistance, risk/reward and
        # quality/cost rules remain common across the retained indicator.
        # indicator_context always calculates all three registered states. Reuse
        # this exact same causal context so indicator rows do not repeat expensive
        # SMC scans and cannot observe subtly different data.
        shared_context = evaluations[0].get("indicator_context") if evaluations else None
        for study_id, enabled, _ in INDICATOR_STUDY_COMBINATIONS:
            study_config = replace(indicator_config, enabled_indicators=enabled, confirmation_mode="all")
            study_evaluations = ([_evaluate_with_context(signal, prefix, study_config, policy, shared_context)
                                  for policy in (effective_quality, ablation_quality)]
                                 if isinstance(shared_context, dict)
                                 else evaluate_candidates(signal, prefix, study_config,
                                                          [effective_quality, ablation_quality]))
            for (name, policy), evaluated in zip(policies, study_evaluations):
                missing = ((not policy.get("allow_missing_liquidity", False)
                            and (signal.get("liquidity_percentile") is None or signal.get("spread_bps") is None))
                           or (policy.get("require_oi", False)
                               and (signal.get("oi_ratio") is None or signal.get("oi_change_pct") is None)))
                study_candidates[study_id][name].append({"index": index, "pattern": signal["pattern"],
                    "setup": evaluated.get("setup"), "volatility_ratio": signal["volatility_ratio"],
                    "activity_score": signal["activity_score"],
                    "qualified": evaluated["qualified"] and not missing,
                    "trade_plan": evaluated.get("trade_plan"), "missing_required": missing,
                    "quality_reasons": evaluated.get("quality_reasons", [])})
    result["analysis_status_counts"] = dict(statuses)
    result["historical_enrichment"] = {"evaluated_prefixes": sum(statuses.values()), "complete_snapshots": complete_enrichment,
        "full_100_point_inputs_available": complete_enrichment == sum(statuses.values()) and complete_enrichment > 0,
        "full_100_point_policy_validated": False,
        "mode": "full_timestamped_history" if complete_enrichment == sum(statuses.values()) and complete_enrichment else "limited_data"}
    grid = list(THRESHOLD_GRID) if opts["compare_thresholds"] else [current_threshold]
    for name, rows in candidates.items():
        strategy = _simulate(candles, rows, current_threshold, warmup, len(candles), opts)
        comparisons = []
        for threshold in grid:
            train = _simulate(candles, rows, threshold, warmup, train_end, opts, include_trades=False, outcome_end=boundary)
            holdout = _simulate(candles, rows, threshold, boundary, len(candles), opts, include_trades=False)
            comparisons.append({"threshold": threshold, "train": train, "holdout": holdout})
        strategy["comparisons"] = comparisons
        strategy["recommendation"] = _recommend(comparisons, opts["min_validation_trades"], timeframe)
        strategy["description"] = ("Configured live qualification rules with timestamped historical enrichment only"
            if name == "strict_full_policy" else "Kline indicator/volume/movement/RR ablation; OI, missing liquidity and activity-score gate removed; NOT the live strategy")
        strategy["quality"] = effective_quality if name == "strict_full_policy" else ablation_quality
        result["strategy"][name] = strategy
    result["recommendation"] = result["strategy"]["strict_full_policy"]["recommendation"]
    result["indicator_study"] = _build_indicator_study(
        candles, study_candidates, current_threshold, warmup, train_end, boundary, opts,
        effective_quality, ablation_quality)
    result["warnings"] = (["Historical enrichment is incomplete; price-only results cannot validate the live full-score filtering policy."]
                           if result["historical_enrichment"]["mode"] == "limited_data" else [])
    if progress is not None:
        progress({"stage": "completed", "processed_bars": replay_total,
                  "total_bars": replay_total, "progress": 1.0})
    return result
