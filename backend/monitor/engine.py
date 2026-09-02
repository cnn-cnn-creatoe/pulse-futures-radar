"""Deterministic two-closed-candle anomaly detection.

All timestamps are UTC Unix milliseconds, and close times are inclusive as in
Binance klines. A calculation needs ``minimum_bars + 3`` candles: historical
bars, their normalization predecessor, and the current pair. Historical V2
windows end strictly before K1, so neither current candle can alter its baseline.
This module performs no I/O and sends no notifications.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from statistics import fmean, median
from typing import Any


TIMEFRAME_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "1d": 86_400_000,
}
# Formal monitoring/download scope selected by the user.  The full interval map
# remains available for parsing retained archives and for the raw 1m stream used
# internally to build closed 5m/15m candles.
MONITOR_TIMEFRAMES = ("5m", "15m")
LOOKBACK = {
    "1m": {"days": 3, "minimum_bars": 1440},
    "5m": {"days": 7, "minimum_bars": 864},
    "15m": {"days": 7, "minimum_bars": 288},
    "30m": {"days": 14, "minimum_bars": 336},
    "1h": {"days": 21, "minimum_bars": 168},
    "2h": {"days": 30, "minimum_bars": 168},
    "1d": {"days": 60, "minimum_bars": 30},
}


@dataclass
class Candle:
    symbol: str
    timeframe: str
    open_time: int
    close_time: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float = 0
    taker_buy_quote: float = 0
    trades: int = 0
    volume: float = 0
    closed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EngineConfig:
    volatility_ratio_min: float = 1.5
    baseline_method: str = "trimmed_mean"
    trim_percent: float = 2.5
    pattern_mode: str = "all"
    allow_doji: bool = False

    def __post_init__(self) -> None:
        if not math.isfinite(self.volatility_ratio_min) or self.volatility_ratio_min < 1.5:
            raise ValueError("volatility_ratio_min must be at least 1.5")
        if self.baseline_method not in {"trimmed_mean", "winsorized_mean", "simple_mean"}:
            raise ValueError("baseline_method must be trimmed_mean or simple_mean")
        if not math.isfinite(self.trim_percent) or not 0 <= self.trim_percent < 50:
            raise ValueError("trim_percent must be in [0, 50)")
        if self.pattern_mode not in {"all", "mixed_only"}:
            raise ValueError("pattern_mode must be all or mixed_only")


def _trimmed_mean(values: list[float], trim_percent: float) -> float:
    """Remove, rather than clamp, equal tails; legacy naming was misleading."""
    ordered = sorted(values)
    trim = math.floor(len(ordered) * trim_percent / 100)
    return fmean(ordered[trim : len(ordered) - trim])


def _mean(values: list[float], config: EngineConfig) -> float:
    if config.baseline_method == "simple_mean":
        return fmean(values)
    return _trimmed_mean(values, config.trim_percent)


def _steps(value: float, thresholds: tuple[tuple[float, int], ...]) -> int:
    result = 0
    for threshold, score in thresholds:
        if value < threshold:
            break
        result = score
    return result


def volume_score(relative_volume: float) -> int:
    return _steps(relative_volume, ((0.8, 4), (1, 8), (1.3, 16), (1.8, 24), (2.5, 31), (4, 35)))


def body_score(body_ratio: float) -> int:
    return _steps(body_ratio, ((1, 3), (1.5, 6), (2, 8), (3, 10)))


def efficiency_score(efficiency: float) -> int:
    return _steps(efficiency, ((0.2, 2), (0.35, 4), (0.55, 5), (0.75, 6)))


def displacement_score(efficiency: float) -> int:
    return _steps(efficiency, ((0.2, 1), (0.4, 2), (0.6, 3), (0.8, 4)))


def flow_score(aligned_ratio: float, neutral: bool = False) -> int:
    score = _steps(aligned_ratio, ((0.5, 3), (0.53, 7), (0.56, 12), (0.6, 16), (0.65, 20)))
    return min(score, 12) if neutral else score


def oi_score(ratio: float) -> int:
    return _steps(ratio, ((1, 3), (1.5, 6), (2, 10), (3, 13), (5, 15)))


def liquidity_score(percentile: float | None, spread_bps: float | None) -> int:
    score = 0 if percentile is None else _steps(
        percentile, ((0.2, 2), (0.4, 4), (0.6, 6), (0.8, 7), (0.95, 8))
    )
    if spread_bps is not None:
        score += 2 if spread_bps <= 5 else 1 if spread_bps <= 15 else 0
    return score


def _valid_prices(candle: Candle) -> bool:
    values = (candle.open, candle.high, candle.low, candle.close)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value > 0 for value in values):
        return False
    if candle.high < max(candle.open, candle.close) or candle.low > min(candle.open, candle.close):
        return False
    return candle.high >= candle.low


def _valid_volume(candle: Candle) -> bool:
    values = (candle.quote_volume, candle.taker_buy_quote, candle.volume, candle.trades)
    if not all(isinstance(value, (int, float)) and math.isfinite(value) and value >= 0 for value in values):
        return False
    tolerance = max(1e-10, candle.quote_volume * 1e-10)
    return candle.taker_buy_quote <= candle.quote_volume + tolerance


def _ntr(candle: Candle, previous: Candle) -> float:
    return max(candle.high - candle.low, abs(candle.high - previous.close), abs(candle.low - previous.close)) / previous.close


def _body(candle: Candle, previous: Candle) -> float:
    return abs(candle.close - candle.open) / previous.close


def _kind(candle: Candle, tick_size: float) -> str:
    ratio = abs(candle.close - candle.open) / max(candle.high - candle.low, tick_size)
    if ratio <= 0.05 or math.isclose(ratio, 0.05, abs_tol=1e-12, rel_tol=0):
        return "十字"
    return "阳" if candle.close > candle.open else "阴"


def _optional_number(enrichment: dict[str, Any], key: str, minimum: float | None = None, maximum: float | None = None) -> float | None:
    value = enrichment.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if minimum is not None and value < minimum:
        return None
    if maximum is not None and value > maximum:
        return None
    return float(value)


def _activity_level(score: float) -> str:
    if score >= 90:
        return "极强活跃"
    if score >= 80:
        return "强活跃"
    if score >= 65:
        return "活跃级"
    if score >= 50:
        return "观察级"
    return "弱活跃异常"


def _volatility_level(ratio: float) -> str:
    if ratio >= 5:
        return "超级异常"
    if ratio >= 3:
        return "极强异常"
    if ratio >= 2:
        return "强异常"
    return "异常"


def analyze(
    candles: list[Candle],
    config: EngineConfig | None = None,
    enrichment: dict[str, Any] | None = None,
    tick_size: float = 1e-8,
) -> dict[str, Any]:
    """Analyze the final pair, including explicit reasons when it is unusable.

    ``history_bars`` excludes both current candles and the normalization
    predecessor. ``baseline_samples`` counts historical two-bar windows.
    Optional OI ratios must be independently measured for this same period.
    Near-neutral means net movement <= 2% of the pair's price range, or one tick.
    """
    config = config or EngineConfig()
    config.__post_init__()  # Revalidate if a caller has changed a config object.
    if not isinstance(tick_size, (int, float)) or not math.isfinite(tick_size) or tick_size <= 0:
        raise ValueError("tick_size must be finite and positive")
    enrichment = enrichment or {}
    result: dict[str, Any] = {"status": "insufficient_history", "signal": None, "history_bars": 0, "baseline_samples": 0}
    if not candles:
        return result
    timeframe = candles[-1].timeframe
    if timeframe not in TIMEFRAME_MS:
        return {**result, "status": "unsupported_timeframe"}
    interval = TIMEFRAME_MS[timeframe]
    minimum = LOOKBACK[timeframe]["minimum_bars"]
    full_history = LOOKBACK[timeframe]["days"] * 86_400_000 // interval
    used = candles[-(full_history + 3) :]
    result.update(required_bars=minimum, history_bars=max(0, len(used) - 3))
    symbol = candles[-1].symbol
    for i, candle in enumerate(used):
        if candle.timeframe != timeframe or candle.symbol != symbol:
            return {**result, "status": "mixed_series"}
        if candle.closed is not True:
            return {**result, "status": "open_candle"}
        if not isinstance(candle.open_time, int) or not isinstance(candle.close_time, int):
            return {**result, "status": "invalid_timestamp"}
        if candle.open_time % interval or candle.close_time != candle.open_time + interval - 1:
            return {**result, "status": "invalid_timestamp"}
        if i and candle.open_time != used[i - 1].open_time + interval:
            return {**result, "status": "gap"}
        if not _valid_prices(candle) or not _valid_volume(candle):
            return {**result, "status": "invalid_data"}
    if len(used) < minimum + 3:
        return result

    # NTR/body arrays are indexed one earlier than candles: normalized[i-1]
    # describes used[i]. The final TWO values belong to the candidate pair.
    ntrs = [_ntr(used[i], used[i - 1]) for i in range(1, len(used))]
    bodies = [_body(used[i], used[i - 1]) for i in range(1, len(used))]
    history_ntrs = ntrs[:-2]
    history_bodies = bodies[:-2]
    history_candles = used[1:-2]
    v2_samples = [history_ntrs[i - 1] + history_ntrs[i] for i in range(1, len(history_ntrs))]
    body_samples = [history_bodies[i - 1] + history_bodies[i] for i in range(1, len(history_bodies))]
    volume_samples = [history_candles[i - 1].quote_volume + history_candles[i].quote_volume for i in range(1, len(history_candles))]
    baseline_v2 = _mean(v2_samples, config)
    baseline_body = _mean(body_samples, config)
    baseline_volume = _mean(volume_samples, config)
    result.update(
        baseline_samples=len(v2_samples),
        baseline_method="simple_mean" if config.baseline_method == "simple_mean" else "trimmed_mean",
        baseline={
            "v2_pct": baseline_v2 * 100,
            "simple_mean_v2_pct": fmean(v2_samples) * 100,
            "trimmed_mean_v2_pct": _trimmed_mean(v2_samples, config.trim_percent) * 100,
            "median_v2_pct": median(v2_samples) * 100,
            "quote_volume": baseline_volume,
            "body_pct": baseline_body * 100,
        },
    )
    if baseline_v2 <= 0:
        return {**result, "status": "zero_baseline"}
    current_v2 = ntrs[-2] + ntrs[-1]
    ratio = current_v2 / baseline_v2
    # Only absorb floating-point roundoff at the threshold, not near misses.
    at_threshold = math.isclose(ratio, config.volatility_ratio_min, rel_tol=1e-12, abs_tol=0)
    if at_threshold:
        ratio = config.volatility_ratio_min
    result.update(status="ready", volatility_ratio=ratio, v2_pct=current_v2 * 100)
    previous, first, second = used[-3:]
    first_kind, second_kind = _kind(first, tick_size), _kind(second, tick_size)
    is_doji = "十字" in (first_kind, second_kind)
    pattern = "十字组合" if is_doji else first_kind + second_kind
    result["pattern"] = pattern
    if (is_doji and not config.allow_doji) or (config.pattern_mode == "mixed_only" and pattern not in {"阴阳", "阳阴"}):
        return {**result, "status": "pattern_filtered"}
    if ratio < config.volatility_ratio_min:
        return result

    pair_range = max(first.high, second.high) - min(first.low, second.low)
    final_move = second.close - first.open
    neutral = abs(final_move) <= max(tick_size, pair_range * 0.02)
    direction = "neutral" if neutral else "up" if final_move > 0 else "down"
    current_body = bodies[-2] + bodies[-1]
    # Zero or unavailable baselines never create fictitious relative metrics.
    relative_volume = (first.quote_volume + second.quote_volume) / baseline_volume if baseline_volume > 0 else None
    body_ratio = current_body / baseline_body if baseline_body > 0 else None
    efficiency = min(1.0, current_body / current_v2) if current_v2 > 0 else 0
    displacement = min(1.0, abs(final_move) / pair_range) if pair_range > 0 else 0
    quote_volume = first.quote_volume + second.quote_volume
    taker_buy_ratio = min(1.0, (first.taker_buy_quote + second.taker_buy_quote) / quote_volume) if quote_volume > 0 else None
    aligned_ratio = None if taker_buy_ratio is None else (
        max(taker_buy_ratio, 1 - taker_buy_ratio) if neutral
        else taker_buy_ratio if direction == "up" else 1 - taker_buy_ratio
    )
    oi_change_pct = _optional_number(enrichment, "oi_change_pct", minimum=-100)
    oi_ratio = _optional_number(enrichment, "oi_ratio", minimum=0)
    oi_available = oi_change_pct is not None and oi_ratio is not None
    percentile = _optional_number(enrichment, "liquidity_percentile", minimum=0, maximum=1)
    spread = _optional_number(enrichment, "spread_bps", minimum=0)
    breakdown = {
        "volume": volume_score(relative_volume) if relative_volume is not None else 0,
        "structure": (body_score(body_ratio) if body_ratio is not None else 0) + efficiency_score(efficiency) + displacement_score(displacement),
        "flow": flow_score(aligned_ratio, neutral) if aligned_ratio is not None else 0,
        "oi": oi_score(oi_ratio) if oi_available else 0,
        "liquidity": liquidity_score(percentile, spread),
    }
    score = sum(breakdown.values())
    score_available_max = (
        75
        + (15 if oi_available else 0)
        + (8 if percentile is not None else 0)
        + (2 if spread is not None else 0)
    )
    warnings: list[str] = []
    if not oi_available:
        warnings.append(
            f"持仓量数据或同周期历史基准缺失：OI 项计 0 分，当前可用满分 {score_available_max} 分"
        )
    if percentile is None or spread is None:
        warnings.append("流动性数据不完整：缺失项计 0 分")
    if baseline_volume <= 0:
        warnings.append("历史成交额基准为零：相对成交额不可用")
    if baseline_body <= 0:
        warnings.append("历史实体基准为零：实体强度倍数不可用")
    if taker_buy_ratio is None:
        warnings.append("当前成交额为零：主动买卖占比不可用")
    if (percentile is not None and percentile < 0.2) or (spread is not None and spread > 15):
        warnings.append("低流动性，滑点风险较高")
    if ratio >= 5 and ((relative_volume is not None and relative_volume < 0.8) or first.trades + second.trades < 10):
        warnings.append("低流动性插针风险，可能存在价格失真")
    if ratio > 20:
        warnings.append("波动超过 20 倍：需要数据复核，不应直接作为决策依据")
    if pattern in {"阳阴", "阴阳"} and displacement < 0.2:
        warnings.append("高波动、高分歧，方向一致性较低")
    if not neutral and aligned_ratio is not None and aligned_ratio < 0.5:
        warnings.append("价格方向与主动成交方向背离")
    if oi_change_pct is None:
        oi_structure = "持仓量数据缺失"
    elif oi_change_pct == 0 or neutral:
        oi_structure = "中性，暂无明确持仓方向"
    elif oi_change_pct > 0:
        oi_structure = "新增多头倾向" if direction == "up" else "新增空头倾向"
    else:
        oi_structure = "空头回补倾向" if direction == "up" else "多头平仓或清算倾向"
    signal = {
        "id": f"{symbol}:{timeframe}:{second.close_time}:TWO_BAR_VOLATILITY",
        "symbol": symbol,
        "timeframe": timeframe,
        "pattern": pattern,
        "direction": direction,
        "open_time": first.open_time,
        "close_time": second.close_time,
        "price": second.close,
        "change_pct": final_move / first.open * 100,
        "volatility_ratio": ratio,
        "v2_pct": current_v2 * 100,
        "baseline_v2_pct": baseline_v2 * 100,
        "range_pct": pair_range / previous.close * 100,
        "relative_volume": relative_volume,
        "quote_volume": quote_volume,
        "taker_buy_ratio": taker_buy_ratio,
        "oi_change_pct": oi_change_pct,
        "oi_ratio": oi_ratio,
        "oi_structure": oi_structure,
        "liquidity_percentile": percentile,
        "spread_bps": spread,
        "activity_score": score,
        "priority_score": min(100, score + min(10, max(0, ratio - 1.5) * 5)),
        "level": _volatility_level(ratio),
        "activity_level": _activity_level(score),
        "score_breakdown": breakdown,
        # Keep the raw score honest when optional market inputs are missing.
        # Price/volume/flow always contribute at most 75 points; OI, the
        # cross-sectional liquidity percentile and spread add only when their
        # source values actually exist.  The UI can therefore show 67/77
        # instead of the misleading 67/100 without inventing any missing score.
        "score_available_max": score_available_max,
        "warnings": warnings,
        "baseline_samples": len(v2_samples),
        "baseline_method": result["baseline_method"],
        "body_ratio": body_ratio,
        "body_efficiency": efficiency,
        "displacement_efficiency": displacement,
        "candles": [candle.to_dict() for candle in used[-50:]],
    }
    result["signal"] = signal
    return result


def detect_signal(
    candles: list[Candle],
    config: EngineConfig | None = None,
    enrichment: dict[str, Any] | None = None,
    tick_size: float = 1e-8,
) -> dict[str, Any] | None:
    return analyze(candles, config, enrichment, tick_size)["signal"]


def aggregate_minutes(candles: list[Candle], timeframe: str) -> list[Candle]:
    """Aggregate complete one-minute buckets on UTC boundaries.

    Supports multiple symbols without mixing them. A missing, duplicate, open,
    malformed, or non-minute member invalidates its entire bucket. Incomplete
    leading and trailing buckets are omitted. No missing values are fabricated.
    """
    if timeframe not in TIMEFRAME_MS:
        raise ValueError(f"unsupported timeframe: {timeframe}")
    interval = TIMEFRAME_MS[timeframe]
    minute = TIMEFRAME_MS["1m"]
    count = interval // minute
    buckets: dict[tuple[str, int], list[Candle]] = {}
    for candle in candles:
        if not isinstance(candle.open_time, int):
            continue
        start = candle.open_time // interval * interval
        buckets.setdefault((candle.symbol, start), []).append(candle)
    output: list[Candle] = []
    for (symbol, start), members in sorted(buckets.items(), key=lambda item: (item[0][1], item[0][0])):
        if len(members) != count:
            continue
        members.sort(key=lambda candle: candle.open_time)
        valid = all(
            candle.timeframe == "1m"
            and candle.closed is True
            and candle.open_time == start + i * minute
            and candle.close_time == candle.open_time + minute - 1
            and _valid_prices(candle)
            and _valid_volume(candle)
            for i, candle in enumerate(members)
        )
        if not valid:
            continue
        output.append(Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=start,
            close_time=start + interval - 1,
            open=members[0].open,
            high=max(candle.high for candle in members),
            low=min(candle.low for candle in members),
            close=members[-1].close,
            quote_volume=sum(candle.quote_volume for candle in members),
            taker_buy_quote=sum(candle.taker_buy_quote for candle in members),
            trades=sum(candle.trades for candle in members),
            volume=sum(candle.volume for candle in members),
            closed=True,
        ))
    return output
