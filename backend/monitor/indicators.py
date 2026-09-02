"""Causal indicator confirmation and conservative screening of anomaly candidates.

SMC portions adapt Smart Money Concepts [LuxAlgo], © LuxAlgo, licensed under
CC BY-NC-SA 4.0. See ../INDICATORS_LICENSE.md for attribution and changes.
Only complete, contiguous candles are accepted. A pivot is usable at its
confirmation close, never at the earlier candle on which its extreme occurred.
The reference trade plan is a cost-adjusted screening aid, not an order.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from statistics import fmean
from typing import Any

from .engine import Candle, TIMEFRAME_MS, _valid_prices, _valid_volume


_CATALOG = (
    {
        "id": "supertrend", "name": "超级趋势",
        "description": "HL2 ± ATR 追踪线；趋势同向，反转要求近期翻转。",
        "parameters": ["atr_period", "atr_multiplier", "atr_method"],
        "formula": "用户提供的 Pine v4 Supertrend 公式",
        "defaults": {"source": "HL2（最高价与最低价的平均值）", "atr_period": 10, "atr_multiplier": 3.0, "atr_method": "Wilder RMA"},
        "visual_layers": [
            {"id": "supertrend", "name": "超级趋势线", "default_visible": True},
            {"id": "flips", "name": "趋势翻转标记", "default_visible": True},
            {"id": "supertrend_highlight", "name": "趋势背景高亮", "default_visible": False},
        ],
    },
    {
        "id": "smc", "name": "SMC 市场结构",
        "description": "确认后的内部/摆动结构、BOS/CHoCH、支撑阻力、订单块、等高低与 FVG。",
        "parameters": ["smc_internal_length", "smc_swing_length", "structure_max_age"],
        "visual_layers": [
            {"id": "events", "name": "结构突破与趋势变化", "default_visible": True},
            {"id": "order_blocks", "name": "订单块", "default_visible": True},
            {"id": "equal_levels", "name": "等高点与等低点", "default_visible": True},
            {"id": "strong_weak", "name": "强弱高低点", "default_visible": True},
            {"id": "fvgs", "name": "公允价值缺口", "default_visible": False},
            {"id": "zone_bounds", "name": "溢价、均衡与折价区", "default_visible": False},
        ],
        "glossary": {
            "BOS": "结构突破", "CHoCH": "趋势变化", "EQH": "等高点", "EQL": "等低点",
            "OB": "订单块", "FVG": "公允价值缺口", "Premium": "溢价区",
            "Equilibrium": "均衡区", "Discount": "折价区",
        },
        "attribution": "Adapted from Smart Money Concepts [LuxAlgo] © LuxAlgo",
        "license": "CC BY-NC-SA 4.0", "license_url": "https://creativecommons.org/licenses/by-nc-sa/4.0/",
        "license_scope": "仅限非商业使用；改编与再发布须署名并采用相同许可",
    },
    {"id": "ema", "name": "EMA 双均线", "description": "EMA20/50 方向确认；反转要求近期交叉。", "parameters": ["ema_fast_period", "ema_slow_period"]},
)


def indicator_catalog() -> list[dict[str, Any]]:
    """Built-in, allowlisted indicator plugins; no uploaded code is executed."""
    return deepcopy(list(_CATALOG))


def _integer(value: Any, name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum}]")


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be finite and in [{minimum}, {maximum}]")
    return float(value)


@dataclass
class IndicatorConfig:
    enabled_indicators: tuple[str, ...] = ("supertrend", "smc")
    confirmation_mode: str = "all"
    atr_period: int = 10
    atr_multiplier: float = 3.0
    atr_method: str = "wilder"
    smc_internal_length: int = 5
    smc_swing_length: int = 50
    structure_max_age: int = 3
    ema_fast_period: int = 20
    ema_slow_period: int = 50

    def __post_init__(self) -> None:
        if not isinstance(self.enabled_indicators, (tuple, list)) or any(not isinstance(item, str) for item in self.enabled_indicators):
            raise ValueError("enabled_indicators must be a list/tuple of registered ids")
        if len(set(self.enabled_indicators)) != len(self.enabled_indicators) or set(self.enabled_indicators) - {item["id"] for item in _CATALOG}:
            raise ValueError("enabled_indicators contains duplicate or unregistered ids")
        self.enabled_indicators = tuple(self.enabled_indicators)
        if self.confirmation_mode not in {"all", "any"}:
            raise ValueError("confirmation_mode must be all or any")
        if self.atr_method not in {"wilder", "sma"}:
            raise ValueError("atr_method must be wilder or sma")
        _integer(self.atr_period, "atr_period", 1, 500)
        _number(self.atr_multiplier, "atr_multiplier", 0.1, 20)
        _integer(self.smc_internal_length, "smc_internal_length", 1, 100)
        _integer(self.smc_swing_length, "smc_swing_length", 2, 500)
        if self.smc_swing_length <= self.smc_internal_length:
            raise ValueError("smc_swing_length must exceed smc_internal_length")
        _integer(self.structure_max_age, "structure_max_age", 0, 100)
        _integer(self.ema_fast_period, "ema_fast_period", 1, 499)
        _integer(self.ema_slow_period, "ema_slow_period", 2, 500)
        if self.ema_fast_period >= self.ema_slow_period:
            raise ValueError("ema_fast_period must be smaller than ema_slow_period")


DEFAULT_QUALITY: dict[str, Any] = {
    "min_activity_score": 80,
    "min_relative_volume": 1.8,
    "min_move_pct": 0.4,
    "min_net_move_pct": 0.25,
    "min_liquidity_percentile": 0.4,
    "max_spread_bps": 8,
    "min_risk_reward": 2,
    "fee_bps_per_side": 5,
    "slippage_bps_per_side": 3,
    "require_oi": False,
    "allow_missing_liquidity": False,
}


def validate_quality_config(quality: dict[str, Any] | None = None) -> dict[str, Any]:
    if quality is not None and not isinstance(quality, dict):
        raise ValueError("quality must be an object")
    unknown = set(quality or {}) - set(DEFAULT_QUALITY)
    if unknown:
        raise ValueError(f"unknown quality setting: {', '.join(sorted(unknown))}")
    result = {**DEFAULT_QUALITY, **(quality or {})}
    limits = {
        "min_activity_score": (0, 100), "min_relative_volume": (0, 1000),
        "min_move_pct": (0, 1000), "min_net_move_pct": (0, 1000),
        "min_liquidity_percentile": (0, 1), "max_spread_bps": (0, 10000),
        "min_risk_reward": (0, 100), "fee_bps_per_side": (0, 1000),
        "slippage_bps_per_side": (0, 1000),
    }
    for key, (minimum, maximum) in limits.items():
        result[key] = _number(result[key], key, minimum, maximum)
    for key in ("require_oi", "allow_missing_liquidity"):
        if not isinstance(result[key], bool):
            raise ValueError(f"{key} must be boolean")
    return result


def _series_status(candles: list[Candle]) -> str:
    if not candles:
        return "insufficient_history"
    timeframe, symbol = candles[-1].timeframe, candles[-1].symbol
    interval = TIMEFRAME_MS.get(timeframe)
    if interval is None:
        return "unsupported_timeframe"
    for i, candle in enumerate(candles):
        if candle.timeframe != timeframe or candle.symbol != symbol:
            return "mixed_series"
        if candle.closed is not True:
            return "open_candle"
        if isinstance(candle.open_time, bool) or isinstance(candle.close_time, bool) or not isinstance(candle.open_time, int) or not isinstance(candle.close_time, int):
            return "invalid_timestamp"
        if candle.open_time % interval or candle.close_time != candle.open_time + interval - 1:
            return "invalid_timestamp"
        if i and candle.open_time != candles[i - 1].open_time + interval:
            return "gap"
        if not _valid_prices(candle) or not _valid_volume(candle):
            return "invalid_data"
    return "ready"


def _atr(candles: list[Candle], period: int, method: str = "wilder") -> list[float | None]:
    ranges = [max(c.high - c.low, abs(c.high - candles[i - 1].close), abs(c.low - candles[i - 1].close)) if i else c.high - c.low for i, c in enumerate(candles)]
    result: list[float | None] = [None] * len(candles)
    # Pine atr(N) uses tr(true), whereas the supplied sma(tr,N) skips the first
    # undefined TR (no previous close). They intentionally differ at warm-up.
    seed_start = 0 if method == "wilder" else 1
    seed_end = seed_start + period - 1
    if len(candles) <= seed_end:
        return result
    result[seed_end] = fmean(ranges[seed_start : seed_end + 1])
    for i in range(seed_end + 1, len(candles)):
        if method == "wilder":
            result[i] = (result[i - 1] * (period - 1) + ranges[i]) / period
        else:
            result[i] = fmean(ranges[i - period + 1 : i + 1])
    return result


def _supertrend(candles: list[Candle], config: IndicatorConfig, atrs: list[float | None]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    previous_up = previous_down = None
    direction = "up"
    flip_time = None
    points: list[dict[str, Any]] = []
    current: dict[str, Any] = {"ready": False, "direction": None, "value": None, "flipped": False, "flip_time": None, "atr": None}
    for i, (candle, atr) in enumerate(zip(candles, atrs)):
        if atr is None:
            continue
        raw_up = (candle.high + candle.low) / 2 - config.atr_multiplier * atr
        raw_down = (candle.high + candle.low) / 2 + config.atr_multiplier * atr
        up1 = raw_up if previous_up is None else previous_up
        down1 = raw_down if previous_down is None else previous_down
        previous_close = candles[i - 1].close if i else candle.close
        up = max(raw_up, up1) if previous_close > up1 else raw_up
        down = min(raw_down, down1) if previous_close < down1 else raw_down
        old_direction = direction
        if direction == "down" and candle.close > down1:
            direction = "up"
        elif direction == "up" and candle.close < up1:
            direction = "down"
        flipped = direction != old_direction
        if flipped:
            flip_time = candle.close_time
        value = up if direction == "up" else down
        signal = "BUY" if flipped and direction == "up" else "SELL" if flipped else None
        points.append({
            "time": candle.open_time, "value": value, "price": value, "direction": direction,
            "source": "supertrend", "code": "UP_TREND" if direction == "up" else "DOWN_TREND",
            "label": "超级趋势上行" if direction == "up" else "超级趋势下行",
            "signal": signal,
            "signal_label": "转为上涨" if signal == "BUY" else "转为下跌" if signal == "SELL" else None,
            "confirmed_at": candle.close_time,
        })
        current = {
            "ready": True, "direction": direction, "value": value, "flipped": flipped,
            "flip_time": flip_time, "atr": atr, "source": "HL2", "atr_period": config.atr_period,
            "atr_multiplier": config.atr_multiplier, "atr_method": config.atr_method,
            "direction_label": "上涨趋势" if direction == "up" else "下跌趋势",
            "atr_method_label": "Wilder RMA" if config.atr_method == "wilder" else "真实波幅简单移动平均",
            "formula": "Pine v4 Supertrend（用户提供公式）",
        }
        previous_up, previous_down = up, down
    return current, points[-300:]


def _ema(candles: list[Candle], config: IndicatorConfig) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    fast = slow = None
    previous_direction = "neutral"
    cross_time = None
    fast_points: list[dict[str, Any]] = []
    slow_points: list[dict[str, Any]] = []
    for i, candle in enumerate(candles):
        fast = candle.close if fast is None else fast + 2 / (config.ema_fast_period + 1) * (candle.close - fast)
        slow = candle.close if slow is None else slow + 2 / (config.ema_slow_period + 1) * (candle.close - slow)
        direction = "up" if fast > slow else "down" if fast < slow else "neutral"
        if direction != previous_direction and previous_direction != "neutral" and direction != "neutral":
            cross_time = candle.close_time
        previous_direction = direction
        if i + 1 >= config.ema_fast_period:
            fast_points.append({"time": candle.open_time, "value": fast})
        if i + 1 >= config.ema_slow_period:
            slow_points.append({"time": candle.open_time, "value": slow})
    ready = len(candles) >= config.ema_slow_period
    return {"ready": ready, "direction": previous_direction if ready else "neutral", "fast": fast if ready else None, "slow": slow if ready else None, "cross_time": cross_time if ready else None}, fast_points[-300:], slow_points[-300:]


def _smc(candles: list[Candle], config: IndicatorConfig) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Confirmed-bar adaptation of LuxAlgo leg/structure/OB/EQ/FVG concepts.

    Internal and swing leg states are independent. Source HTF lookahead and
    retroactive chart labels are deliberately not reproduced. ATR200 filters
    order blocks and equal levels; unavailable ATR200 does not invent values.
    """
    states = {name: {"length": length, "leg": 0, "high": None, "low": None, "direction": "neutral", "event": None} for name, length in (("swing", config.smc_swing_length), ("internal", config.smc_internal_length), ("equal", 3))}
    atr200 = _atr(candles, 200)
    parsed_highs: list[float] = []
    parsed_lows: list[float] = []
    levels: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    order_blocks: list[dict[str, Any]] = []
    equal_levels: list[dict[str, Any]] = []
    fvgs: list[dict[str, Any]] = []
    swing_points: list[dict[str, Any]] = []
    cumulative_delta = 0.0
    for i, candle in enumerate(candles):
        atr = atr200[i]
        high_volatility = atr is not None and candle.high - candle.low >= 2 * atr
        parsed_highs.append(candle.low if high_volatility else candle.high)
        parsed_lows.append(candle.high if high_volatility else candle.low)
        # High/low mitigation uses this completed candle. A deleted zone is
        # never later resurrected merely because a price returns to it.
        order_blocks = [block for block in order_blocks if not ((block["direction"] == "up" and candle.low < block["low"]) or (block["direction"] == "down" and candle.high > block["high"]))]
        fvgs = [gap for gap in fvgs if not ((gap["direction"] == "up" and candle.low < gap["low"]) or (gap["direction"] == "down" and candle.high > gap["high"]))]
        for name, state in states.items():
            size = state["length"]
            if i < size:
                continue
            pivot_index = i - size
            pivot_candle = candles[pivot_index]
            recent = candles[pivot_index + 1 : i + 1]
            leg = state["leg"]
            if pivot_candle.high > max(bar.high for bar in recent):
                leg = 0
            elif pivot_candle.low < min(bar.low for bar in recent):
                leg = 1
            if leg != state["leg"]:
                side = "low" if leg == 1 else "high"
                price = pivot_candle.low if side == "low" else pivot_candle.high
                previous = state[side]
                if name == "equal" and previous is not None and atr is not None and abs(price - previous["price"]) < 0.1 * atr:
                    equal_levels.append({
                        "price": price, "kind": "support" if side == "low" else "resistance",
                        "label": "EQL" if side == "low" else "EQH",
                        "label_cn": "等低点" if side == "low" else "等高点",
                        "code": "EQL" if side == "low" else "EQH",
                        "direction": "up" if side == "low" else "down",
                        "confirmed_at": candle.close_time, "confirmation_time": candle.open_time,
                        "pivot_time": pivot_candle.open_time, "previous_pivot_time": previous["pivot_time"],
                        "source": "equal",
                    })
                    equal_levels = equal_levels[-100:]
                state[side] = {"price": price, "index": pivot_index, "pivot_time": pivot_candle.open_time, "confirmed_at": candle.close_time, "crossed": False}
                if name != "equal":
                    source_label = "摆动" if name == "swing" else "内部"
                    levels.append({
                        "price": price, "kind": "support" if side == "low" else "resistance",
                        "label": source_label + ("低点" if side == "low" else "高点"),
                        "label_cn": source_label + ("低点" if side == "low" else "高点"),
                        "code": ("SWING_" if name == "swing" else "INTERNAL_") + side.upper(),
                        "confirmed_at": candle.close_time, "confirmation_time": candle.open_time,
                        "pivot_time": pivot_candle.open_time, "source": name,
                    })
                    levels = levels[-200:]
                    if name == "swing":
                        if side == "low":
                            code = "LL" if previous is not None and price < previous["price"] else "HL"
                            label = "更低低点" if code == "LL" else "更高低点"
                            direction = "up"
                        else:
                            code = "HH" if previous is not None and price > previous["price"] else "LH"
                            label = "更高高点" if code == "HH" else "更低高点"
                            direction = "down"
                        swing_points.append({
                            "time": candle.open_time, "price": price, "direction": direction,
                            "source": "swing", "code": code, "label": label,
                            "pivot_time": pivot_candle.open_time, "confirmed_at": candle.close_time,
                        })
                        swing_points = swing_points[-100:]
            state["leg"] = leg
        for name in ("internal", "swing"):
            state = states[name]
            for side, direction in (("high", "up"), ("low", "down")):
                pivot = state[side]
                if pivot is None or pivot["crossed"] or i == 0:
                    continue
                same_swing = states["swing"][side]
                if name == "internal" and same_swing is not None and pivot["price"] == same_swing["price"]:
                    continue
                previous_close = candles[i - 1].close
                crossed = (candle.close > pivot["price"] >= previous_close) if direction == "up" else (candle.close < pivot["price"] <= previous_close)
                if not crossed:
                    continue
                event = "CHoCH" if state["direction"] not in {direction, "neutral"} else "BOS"
                event_label = "结构突破" if event == "BOS" else "趋势变化"
                source_label = "内部" if name == "internal" else "波段"
                record = {
                    "event": event, "event_label": event_label, "label": source_label + event_label,
                    "direction": direction, "direction_label": "向上" if direction == "up" else "向下",
                    "time": candle.close_time, "bar_time": candle.open_time, "price": pivot["price"],
                    "source": name, "source_label": source_label, "pivot_time": pivot["pivot_time"],
                    "confirmed_at": pivot["confirmed_at"], "event_confirmed_at": candle.close_time,
                }
                state["direction"], state["event"], pivot["crossed"] = direction, record, True
                events.append(record)
                events = events[-300:]
                indices = range(pivot["index"], i)
                if indices:
                    block_index = min(indices, key=lambda j: parsed_lows[j]) if direction == "up" else max(indices, key=lambda j: parsed_highs[j])
                    block_low, block_high = parsed_lows[block_index], parsed_highs[block_index]
                    # The source swaps parsed bounds for exceptional bars;
                    # such a bar cannot become a usable inverted price zone.
                    if block_low <= block_high:
                        block = {
                            "low": block_low, "high": block_high, "direction": direction, "source": name,
                            "code": "INTERNAL_OB" if name == "internal" else "SWING_OB",
                            "label": ("内部" if name == "internal" else "波段") + ("看涨订单块" if direction == "up" else "看跌订单块"),
                            "direction_label": "看涨" if direction == "up" else "看跌",
                            "origin_time": candles[block_index].open_time, "confirmation_time": candle.open_time,
                            "confirmed_at": candle.close_time, "mitigation": "high_low", "status": "active",
                        }
                        if not ((direction == "up" and candle.low < block_low) or (direction == "down" and candle.high > block_high)):
                            order_blocks.append(block)
                            order_blocks = order_blocks[-100:]
        if i:
            previous = candles[i - 1]
            delta = (previous.close - previous.open) / previous.open
            cumulative_delta += abs(delta)
            threshold = cumulative_delta / i * 2
            if i >= 2:
                before = candles[i - 2]
                direction = None
                if candle.low > before.high and previous.close > before.high and delta > threshold:
                    low, high, direction = before.high, candle.low, "up"
                elif candle.high < before.low and previous.close < before.low and -delta > threshold:
                    low, high, direction = candle.high, before.low, "down"
                if direction:
                    fvgs.append({
                        "low": low, "high": high, "direction": direction,
                        "code": "BULLISH_FVG" if direction == "up" else "BEARISH_FVG",
                        "label": "看涨公允价值缺口" if direction == "up" else "看跌公允价值缺口",
                        "direction_label": "看涨" if direction == "up" else "看跌",
                        "confirmed_at": candle.close_time, "confirmation_time": candle.open_time,
                        "origin_time": previous.open_time, "source": "same_timeframe", "status": "active",
                    })
                    fvgs = fvgs[-100:]
    current_price = candles[-1].close
    # Historical confirmed levels remain evidence, with roles relative to the
    # present close. Their original kind is retained for a transparent chart.
    mapped_levels = []
    for level in levels + equal_levels:
        kind = "support" if level["price"] < current_price else "resistance" if level["price"] > current_price else "at_price"
        # Keep the original abbreviation in ``code`` while all visible labels
        # are Chinese.  This preserves stable machine identifiers without
        # forcing the chart to expose EQH/EQL or other English text.
        mapped_levels.append({**level, "label": level.get("label_cn", level["label"]),
                              "original_kind": level["kind"], "kind": kind})
    supports = [level["price"] for level in mapped_levels if level["kind"] == "support"]
    resistances = [level["price"] for level in mapped_levels if level["kind"] == "resistance"]
    latest_event = events[-1] if events else None
    internal, swing = states["internal"], states["swing"]
    # A confirmed latest break determines short-term direction; swing direction
    # is reported separately rather than silently overriding a recent CHoCH.
    direction = latest_event["direction"] if latest_event else "neutral"
    ready = len(candles) >= config.smc_swing_length + 1 and internal["high"] is not None and internal["low"] is not None
    swing_high, swing_low = swing["high"], swing["low"]
    zone = None
    zone_bounds = None
    zone_layers: list[dict[str, Any]] = []
    strong_weak: list[dict[str, Any]] = []
    if swing_high is not None and swing_low is not None and swing_high["price"] > swing_low["price"]:
        bottom, top = swing_low["price"], swing_high["price"]
        mid = (bottom + top) / 2
        zone = "premium" if current_price > mid + (top - bottom) * 0.025 else "discount" if current_price < mid - (top - bottom) * 0.025 else "equilibrium"
        zone_bounds = {"low": bottom, "high": top, "equilibrium": mid, "confirmed_at": max(swing_low["confirmed_at"], swing_high["confirmed_at"])}
        confirmed_at = zone_bounds["confirmed_at"]
        confirmation_time = confirmed_at - TIMEFRAME_MS[candles[-1].timeframe] + 1
        common = {"start_time": confirmation_time, "end_time": candles[-1].close_time, "source": "confirmed_swing", "confirmed_at": confirmed_at}
        zone_layers = [
            {**common, "low": 0.95 * top + 0.05 * bottom, "high": top, "direction": "down", "code": "PREMIUM", "label": "溢价区"},
            {**common, "low": 0.525 * bottom + 0.475 * top, "high": 0.525 * top + 0.475 * bottom, "direction": "neutral", "code": "EQUILIBRIUM", "label": "均衡区"},
            {**common, "low": bottom, "high": 0.95 * bottom + 0.05 * top, "direction": "up", "code": "DISCOUNT", "label": "折价区"},
        ]
    if swing_high is not None:
        weak = swing["direction"] != "down"
        strong_weak.append({
            "time": swing_high["confirmed_at"] - TIMEFRAME_MS[candles[-1].timeframe] + 1,
            "price": swing_high["price"], "direction": "down", "source": "swing",
            "code": "WEAK_HIGH" if weak else "STRONG_HIGH", "label": "弱高点" if weak else "强高点",
            "pivot_time": swing_high["pivot_time"], "confirmed_at": swing_high["confirmed_at"],
        })
    if swing_low is not None:
        weak = swing["direction"] != "up"
        strong_weak.append({
            "time": swing_low["confirmed_at"] - TIMEFRAME_MS[candles[-1].timeframe] + 1,
            "price": swing_low["price"], "direction": "up", "source": "swing",
            "code": "WEAK_LOW" if weak else "STRONG_LOW", "label": "弱低点" if weak else "强低点",
            "pivot_time": swing_low["pivot_time"], "confirmed_at": swing_low["confirmed_at"],
        })
    position_assessment = _position_assessment(
        candles, direction, swing_points, swing_low, swing_high, order_blocks, atr200[-1]
    )
    return {
        "ready": ready, "direction": direction,
        "direction_label": "向上" if direction == "up" else "向下" if direction == "down" else "未确认",
        "internal_direction": internal["direction"], "swing_direction": swing["direction"],
        "event": latest_event["event"] if latest_event else None, "event_direction": latest_event["direction"] if latest_event else None,
        "event_label": latest_event["event_label"] if latest_event else None,
        "event_time": latest_event["time"] if latest_event else None, "events": events,
        "support": max(supports) if supports else None, "resistance": min(resistances) if resistances else None,
        "order_blocks": order_blocks[-20:], "fvgs": fvgs[-20:], "equal_levels": equal_levels[-20:],
        "swing_points": swing_points[-100:], "strong_weak": strong_weak,
        "zone": zone, "zone_label": {"premium": "溢价区", "equilibrium": "均衡区", "discount": "折价区"}.get(zone),
        "zone_bounds": zone_bounds, "zone_layers": zone_layers,
        "position_assessment": position_assessment,
        "license": "CC BY-NC-SA 4.0", "attribution": "Adapted from Smart Money Concepts [LuxAlgo] © LuxAlgo",
    }, mapped_levels[-300:]


def _position_assessment(
    candles: list[Candle],
    structure_direction: str,
    swing_points: list[dict[str, Any]],
    swing_low: dict[str, Any] | None,
    swing_high: dict[str, Any] | None,
    order_blocks: list[dict[str, Any]],
    atr: float | None,
) -> dict[str, Any]:
    """Classify entry location using information known at the final close only.

    A swing boundary is published only after its delayed pivot confirmation.
    A break is deliberately *not* considered established on the breakout bar or
    its first follow-through bar: three completed closes beyond a volatility
    buffer are required.  This keeps an early break of a range high labelled as
    overhead pressure instead of converting it immediately into support.
    """
    price = candles[-1].close
    assessed_at = candles[-1].close_time
    base = {
        "assessed_at": assessed_at,
        "uses_closed_bars_only": True,
        "causal": True,
        "code": "insufficient_structure",
        "label": "结构位置待确认",
        "regime": "unknown",
        "regime_label": "结构不足",
        "reason": "最近已确认的摆动高低点不足，不能判断所处区间",
        "support": None,
        "resistance": None,
        "range": None,
        "breakout": None,
        "long_allowed": False,
        "short_allowed": False,
        "long_advice": "等待位置确认",
        "short_advice": "等待位置确认",
    }
    if swing_low is None or swing_high is None or swing_high["price"] <= swing_low["price"]:
        return base

    bottom, top = float(swing_low["price"]), float(swing_high["price"])
    width = top - bottom
    # A 5% range-edge band mirrors the SMC premium/discount edge layers.  ATR
    # broadens it on volatile contracts so a marginal close above a prior high
    # is not mistaken for a clean break.
    edge = max(width * 0.05, (float(atr) * 0.75) if atr is not None else 0.0, price * 0.001)
    confirmed_at = max(int(swing_low["confirmed_at"]), int(swing_high["confirmed_at"]))
    eligible = [bar for bar in candles if bar.close_time > confirmed_at]
    closes = [bar.close for bar in eligible]
    upper_held = len(closes) >= 3 and all(value > top + edge for value in closes[-3:])
    lower_held = len(closes) >= 3 and all(value < bottom - edge for value in closes[-3:])

    recent_codes = [point["code"] for point in swing_points[-6:]]
    if "HH" in recent_codes[-3:] and "HL" in recent_codes[-3:]:
        regime, regime_label = "uptrend", "上升趋势"
    elif "LH" in recent_codes[-3:] and "LL" in recent_codes[-3:]:
        regime, regime_label = "downtrend", "下降趋势"
    else:
        regime, regime_label = "range", "震荡区间"

    support = {
        "price": bottom,
        "zone_low": bottom - edge,
        "zone_high": bottom + edge,
        "distance_pct": abs(price - bottom) / price * 100,
        "relative": "下方" if bottom < price else "上方" if bottom > price else "当前",
        "label": "前低支撑位",
        "confirmed_at": int(swing_low["confirmed_at"]),
    }
    resistance = {
        "price": top,
        "zone_low": top - edge,
        "zone_high": top + edge,
        "distance_pct": abs(price - top) / price * 100,
        "relative": "上方" if top > price else "下方" if top < price else "当前",
        "label": "前高压力位",
        "confirmed_at": int(swing_high["confirmed_at"]),
    }
    range_info = {
        "low": bottom,
        "high": top,
        "mid": (bottom + top) / 2,
        "edge_buffer": edge,
        "width_pct": width / price * 100,
        "confirmed_at": confirmed_at,
    }

    bearish_blocks = [block for block in order_blocks if block["direction"] == "down"]
    bullish_blocks = [block for block in order_blocks if block["direction"] == "up"]
    in_bearish_block = next((block for block in reversed(bearish_blocks)
                             if block["low"] - edge <= price <= block["high"] + edge), None)
    in_bullish_block = next((block for block in reversed(bullish_blocks)
                             if block["low"] - edge <= price <= block["high"] + edge), None)

    result = {**base, "regime": regime, "regime_label": regime_label,
              "support": support, "resistance": resistance, "range": range_info}
    # A still-active opposite order block is stronger evidence than a simple
    # sequence of closes beyond the last swing.  It must be cleared/mitigated
    # before the breakout can be described as established.
    if upper_held and not in_bearish_block:
        return {**result,
                "code": "breakout_up_held", "label": "向上突破已站稳",
                "reason": "已连续3根收盘高于前高压力区，前压力位可观察为回踩支撑",
                "breakout": {"direction": "up", "status": "held", "label": "向上突破已站稳",
                             "level": top, "required_closes": 3, "confirmed_closes": 3},
                "reference_type": "former_swing_resistance", "level": top,
                "zone_low": top - edge, "zone_high": top + edge,
                "distance_pct": abs(price - top) / price * 100, "breakout_confirmed": True,
                "long_allowed": structure_direction == "up", "short_allowed": False,
                "long_advice": "突破站稳，可观察顺势做多" if structure_direction == "up" else "突破站稳但结构方向未同步",
                "short_advice": "不宜逆势做空"}
    if lower_held and not in_bullish_block:
        return {**result,
                "code": "breakout_down_held", "label": "向下突破已站稳",
                "reason": "已连续3根收盘低于前低支撑区，前支撑位可观察为反抽压力",
                "breakout": {"direction": "down", "status": "held", "label": "向下突破已站稳",
                             "level": bottom, "required_closes": 3, "confirmed_closes": 3},
                "reference_type": "former_swing_support", "level": bottom,
                "zone_low": bottom - edge, "zone_high": bottom + edge,
                "distance_pct": abs(price - bottom) / price * 100, "breakout_confirmed": True,
                "long_allowed": False, "short_allowed": structure_direction == "down",
                "long_advice": "不宜逆势做多",
                "short_advice": "突破站稳，可观察顺势做空" if structure_direction == "down" else "突破站稳但结构方向未同步"}

    above_count = 0
    for value in reversed(closes):
        if value > top:
            above_count += 1
        else:
            break
    below_count = 0
    for value in reversed(closes):
        if value < bottom:
            below_count += 1
        else:
            break
    if in_bearish_block or price >= top - edge:
        source = (("看跌订单块内" if in_bearish_block["low"] <= price <= in_bearish_block["high"] else "看跌订单块附近")
                  if in_bearish_block else ("震荡区间上沿" if price <= top else "刚突破前高"))
        zone_low = float(in_bearish_block["low"]) if in_bearish_block else top - edge
        zone_high = float(in_bearish_block["high"]) if in_bearish_block else top + edge
        distance = 0.0 if zone_low <= price <= zone_high else min(abs(price - zone_low), abs(price - zone_high)) / price * 100
        return {**result,
                "code": "upper_resistance", "label": "前方压力区（突破未站稳）",
                "reason": f"当前位于{source}，向上突破尚未满足连续3根收盘站稳",
                "breakout": {"direction": "up", "status": "pending", "label": "向上突破待确认",
                             "level": top, "required_closes": 3, "confirmed_closes": min(above_count, 2)},
                "reference_type": "bearish_order_block" if in_bearish_block else "swing_range_upper",
                "level": (zone_low + zone_high) / 2, "zone_low": zone_low, "zone_high": zone_high,
                "distance_pct": distance, "breakout_confirmed": False,
                "long_allowed": False, "short_allowed": structure_direction == "down",
                "long_advice": "不宜追多", "short_advice": "压力附近，可观察做空"}
    if in_bullish_block or price <= bottom + edge:
        source = (("看涨订单块内" if in_bullish_block["low"] <= price <= in_bullish_block["high"] else "看涨订单块附近")
                  if in_bullish_block else ("震荡区间下沿" if price >= bottom else "刚跌破前低"))
        zone_low = float(in_bullish_block["low"]) if in_bullish_block else bottom - edge
        zone_high = float(in_bullish_block["high"]) if in_bullish_block else bottom + edge
        distance = 0.0 if zone_low <= price <= zone_high else min(abs(price - zone_low), abs(price - zone_high)) / price * 100
        return {**result,
                "code": "lower_support", "label": "下方支撑区（跌破未站稳）",
                "reason": f"当前位于{source}，向下突破尚未满足连续3根收盘站稳",
                "breakout": {"direction": "down", "status": "pending", "label": "向下突破待确认",
                             "level": bottom, "required_closes": 3, "confirmed_closes": min(below_count, 2)},
                "reference_type": "bullish_order_block" if in_bullish_block else "swing_range_lower",
                "level": (zone_low + zone_high) / 2, "zone_low": zone_low, "zone_high": zone_high,
                "distance_pct": distance, "breakout_confirmed": False,
                "long_allowed": structure_direction == "up", "short_allowed": False,
                "long_advice": "支撑附近，可观察做多", "short_advice": "不宜追空"}

    middle_label = "趋势中部" if regime != "range" else "震荡区间中部"
    return {**result, "code": "structure_middle", "label": middle_label,
            "reason": "当前远离最近已确认的支撑区和压力区，位置优势不明显",
            "reference_type": "confirmed_swing_range", "level": (bottom + top) / 2,
            "zone_low": bottom, "zone_high": top,
            "distance_pct": abs(price - (bottom + top) / 2) / price * 100,
            "breakout_confirmed": False,
            "long_allowed": structure_direction == "up", "short_allowed": structure_direction == "down",
            "long_advice": "趋势同向但位置一般" if structure_direction == "up" else "等待更好做多位置",
            "short_advice": "趋势同向但位置一般" if structure_direction == "down" else "等待更好做空位置"}


def _empty_context(status: str, history: int) -> dict[str, Any]:
    return {"status": status, "available_history": history,
            "supertrend": {"ready": False, "direction": None, "value": None, "flipped": False, "flip_time": None, "atr": None},
            "smc": {"ready": False, "direction": "neutral", "direction_label": "未确认", "event": None, "event_label": None, "event_direction": None, "event_time": None, "events": [], "support": None, "resistance": None, "order_blocks": [], "fvgs": [], "equal_levels": [], "swing_points": [], "strong_weak": [], "zone": None, "zone_label": None, "zone_bounds": None, "zone_layers": [], "position_assessment": None},
            "ema": {"ready": False, "direction": "neutral", "fast": None, "slow": None, "cross_time": None},
            "chart": {"supertrend": [], "flips": [], "ema_fast": [], "ema_slow": [], "levels": [], "events": [], "order_blocks": [], "fvgs": [], "equal_levels": [], "zone_bounds": [], "strong_weak": [], "swing_points": []},
            "warnings": [f"指标数据不可用：{status}；不补造 K 线或指标值"]}


def indicator_context(candles: list[Candle], config: IndicatorConfig | None = None) -> dict[str, Any]:
    config = config or IndicatorConfig()
    config.__post_init__()
    status = _series_status(candles)
    if status != "ready":
        return _empty_context(status, len(candles))
    atrs = _atr(candles, config.atr_period, config.atr_method)
    supertrend, trend_points = _supertrend(candles, config, atrs)
    ema, fast_points, slow_points = _ema(candles, config)
    smc, levels = _smc(candles, config)
    warnings = []
    for name in config.enabled_indicators:
        if not {"supertrend": supertrend, "ema": ema, "smc": smc}[name]["ready"]:
            warnings.append(f"{next(item['name'] for item in _CATALOG if item['id'] == name)} 历史或确认点不足")
    if len(candles) < 200:
        warnings.append("SMC ATR200 尚未就绪：等高低确认未启用，订单块未使用 ATR200 波动过滤")
    if not config.enabled_indicators:
        warnings.append("已选择原始价格模式：不要求指标共振，仍执行质量与空间过滤")
    flips = [{
        "time": point["time"], "price": point["price"], "direction": point["direction"],
        "source": "supertrend", "code": point["signal"], "label": point["signal_label"],
        "confirmed_at": point["confirmed_at"],
    } for point in trend_points if point["signal"]]
    events = [{
        "time": event["bar_time"], "price": event["price"], "direction": event["direction"],
        "source": event["source"], "code": event["event"], "label": event["label"],
        "pivot_time": event["pivot_time"], "pivot_confirmed_at": event["confirmed_at"],
        "confirmed_at": event["event_confirmed_at"],
    } for event in smc["events"]]
    order_blocks = [{
        **block, "start_time": block["confirmation_time"], "end_time": candles[-1].close_time,
        "extend_right": True,
    } for block in smc["order_blocks"]]
    fvgs = [{
        **gap, "start_time": gap["confirmation_time"], "end_time": candles[-1].close_time,
        "extend_right": True,
    } for gap in smc["fvgs"]]
    equal_levels = [{
        "time": level["confirmation_time"], "start_time": level["confirmation_time"],
        "end_time": candles[-1].close_time, "price": level["price"], "direction": level["direction"],
        "source": "equal", "code": level["code"], "label": level["label_cn"],
        "pivot_time": level["pivot_time"], "previous_pivot_time": level["previous_pivot_time"],
        "confirmed_at": level["confirmed_at"], "extend_right": True,
    } for level in smc["equal_levels"]]
    return {"status": "ready", "available_history": len(candles), "supertrend": supertrend, "smc": smc, "ema": ema,
            "chart": {"supertrend": trend_points, "flips": flips, "ema_fast": fast_points, "ema_slow": slow_points,
                      "levels": levels, "events": events[-300:], "order_blocks": order_blocks[-100:],
                      "fvgs": fvgs[-100:], "equal_levels": equal_levels[-100:],
                      "zone_bounds": smc["zone_layers"], "strong_weak": smc["strong_weak"],
                      "swing_points": smc["swing_points"][-100:]}, "warnings": warnings}


def _metric(signal: dict[str, Any], key: str) -> float | None:
    value = signal.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _recent(timestamp: int | None, now: int, interval: int, age: int) -> bool:
    return timestamp is not None and 0 <= now - timestamp <= age * interval


def _confirmation(name: str, context: dict[str, Any], direction: str, reversal: bool, now: int, interval: int, config: IndicatorConfig) -> dict[str, Any]:
    data = context[name]
    label = next(item["name"] for item in _CATALOG if item["id"] == name)
    passed = data["ready"] and data["direction"] == direction
    reason = "方向一致" if passed else "尚未就绪或方向不一致"
    if passed and reversal:
        if name == "supertrend":
            passed = _recent(data["flip_time"], now, interval, config.structure_max_age)
            reason = "近期超级趋势翻转" if passed else "缺少近期超级趋势翻转"
        elif name == "ema":
            passed = _recent(data["cross_time"], now, interval, config.structure_max_age)
            reason = "近期 EMA 交叉" if passed else "缺少近期 EMA 交叉"
        else:
            passed = any(event["event"] == "CHoCH" and event["direction"] == direction and _recent(event["time"], now, interval, config.structure_max_age) for event in data["events"])
            reason = "近期已确认 CHoCH" if passed else "缺少近期已确认 CHoCH"
    elif passed and name == "smc":
        passed = any(event["event"] == "BOS" and event["direction"] == direction for event in data["events"])
        reason = "已有同向 BOS 且当前结构同向" if passed else "同向结构尚无 BOS 确认"
    if name == "smc":
        position = _directional_position(data.get("position_assessment"), direction)
        if position["blocked"]:
            passed = False
            reason = f"{position['label']}：{position['advice']}；{position['reason']}"
        elif passed:
            reason += f"；{position['label']}，{position['advice']}"
    return {"id": name, "name": label, "passed": bool(passed), "reason": reason}


def _directional_position(position: dict[str, Any] | None, direction: str) -> dict[str, Any]:
    """Stable direction-specific location payload consumed by UI and TG."""
    trade_direction = "long" if direction == "up" else "short" if direction == "down" else "wait"
    if not isinstance(position, dict):
        return {"code": "position_unconfirmed", "label": "位置未确认", "advice": "等待位置确认",
                "trade_direction": trade_direction, "blocked": True, "status": "wait", "reference_type": None,
                "level": None, "zone_low": None, "zone_high": None, "distance_pct": None,
                "breakout_confirmed": False, "reason": "缺少已确认的支撑、压力或区间边界",
                "as_of": None, "uses_closed_bars_only": True}
    allowed_key = "long_allowed" if direction == "up" else "short_allowed"
    advice_key = "long_advice" if direction == "up" else "short_advice"
    allowed = bool(position.get(allowed_key, False))
    blocked = not allowed
    code = position.get("code", "position_unconfirmed")
    favorable = ((direction == "up" and code in {"lower_support", "breakout_up_held"})
                 or (direction == "down" and code in {"upper_resistance", "breakout_down_held"}))
    adverse = ((direction == "up" and code in {"upper_resistance", "breakout_down_held"})
               or (direction == "down" and code in {"lower_support", "breakout_up_held"}))
    advice = position.get(advice_key, "等待位置确认")
    reason = position.get("reason", "位置判断依据不足")
    if allowed:
        status = "actionable"
    elif adverse:
        status = "blocked"
        advice = "不宜追多" if direction == "up" else "不宜追空"
    elif favorable:
        status = "wait"
        advice = "有利位置，等待结构转为向上" if direction == "up" else "有利位置，等待结构转为向下"
        reason += "；当前SMC结构方向尚未与" + ("做多" if direction == "up" else "做空") + "同步"
    elif code in {"insufficient_structure", "position_unconfirmed", "structure_middle"}:
        status = "wait"
    else:
        status = "wait"
        advice = "等待位置与结构共同确认"
    return {
        "code": code,
        "label": position.get("label", "位置未确认"),
        "advice": advice,
        "trade_direction": trade_direction,
        "blocked": blocked,
        "status": status,
        "reference_type": position.get("reference_type"),
        "level": position.get("level"),
        "zone_low": position.get("zone_low"),
        "zone_high": position.get("zone_high"),
        "distance_pct": position.get("distance_pct"),
        "breakout_confirmed": bool(position.get("breakout_confirmed", False)),
        "reason": reason,
        "as_of": position.get("assessed_at"),
        "uses_closed_bars_only": bool(position.get("uses_closed_bars_only", True)),
    }


def _trade_plan(context: dict[str, Any], direction: str, entry: float, costs: float) -> dict[str, Any] | None:
    support, resistance = context["smc"]["support"], context["smc"]["resistance"]
    if support is None or resistance is None or not 0 < support < entry < resistance:
        return None
    atr = context["supertrend"]["atr"]
    buffer = max(entry * 0.0001, (atr or 0) * 0.1)
    stop, target = (support - buffer, resistance - buffer) if direction == "up" else (resistance + buffer, support + buffer)
    reward = target - entry if direction == "up" else entry - target
    risk = entry - stop if direction == "up" else stop - entry
    if stop <= 0 or target <= 0 or reward <= 0 or risk <= 0:
        return None
    cost_amount = entry * costs / 10000
    return {"direction": "long" if direction == "up" else "short", "reference_entry": entry, "stop_loss": stop, "target": target,
            "risk_reward": (reward - cost_amount) / (risk + cost_amount), "gross_risk_reward": reward / risk,
            "stop_distance_pct": risk / entry * 100, "estimated_cost_bps": costs, "net_potential_pct": (reward - cost_amount) / entry * 100,
            "support": support, "resistance": resistance, "buffer": buffer, "basis": "confirmed_structure_levels", "not_order": True,
            "warning": "收盘参考方案；不保证成交、胜率或止损执行价格，未包含未来资金费率"}


def evaluate_candidate(signal: dict[str, Any], candles: list[Candle], config: IndicatorConfig | None = None, quality: dict[str, Any] | None = None) -> dict[str, Any]:
    """Enrich a base anomaly signal without modifying it or claiming a trade.

    Missing required score/volume/liquidity metrics fail closed. Only the
    explicit allow_missing_liquidity ablation can bypass historical liquidity.
    No-indicator mode is explicitly named raw_price_mode; quality gates remain.
    """
    config = config or IndicatorConfig()
    config.__post_init__()
    rules = validate_quality_config(quality)
    context = indicator_context(candles, config)
    return _evaluate_with_context(signal, candles, config, rules, context)


def evaluate_candidates(signal: dict[str, Any], candles: list[Candle], config: IndicatorConfig | None, qualities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evaluate up to 16 policies against one identical causal indicator state.

    Intended for strict-versus-ablation backtests. The same private evaluator
    is used by evaluate_candidate; no indicator settings or history is cached
    across candidate times. Returned contexts are independently mutable copies.
    """
    if not isinstance(qualities, list) or not 1 <= len(qualities) <= 16:
        raise ValueError("qualities must contain 1 to 16 quality objects")
    config = config or IndicatorConfig()
    config.__post_init__()
    rules = [validate_quality_config(quality) for quality in qualities]
    context = indicator_context(candles, config)
    return [_evaluate_with_context(signal, candles, config, rule, deepcopy(context)) for rule in rules]


def _evaluate_with_context(signal: dict[str, Any], candles: list[Candle], config: IndicatorConfig, rules: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(signal)
    reasons: list[str] = []
    pattern = signal.get("pattern")
    reversal = pattern in {"阳阴", "阴阳"}
    direction = "up" if pattern in {"阳阳", "阴阳"} else "down" if pattern in {"阴阴", "阳阴"} else "neutral"
    result.update(indicators=context, indicator_context=context, setup="reversal" if reversal else "trend", qualified=False,
                  quality_reasons=reasons, confirmations=[], trade_plan=None, raw_price_mode=not config.enabled_indicators,
                  position_assessment=None, entry_advice="等待位置确认")
    if context["status"] != "ready" or len(candles) < 2:
        reasons.append("缺少连续、完整、已收盘的指标数据")
        return result
    last = candles[-1]
    if signal.get("symbol") != last.symbol or signal.get("timeframe") != last.timeframe or signal.get("close_time") != last.close_time:
        reasons.append("候选信号与指标 K 线时点或合约不匹配")
    if pattern not in {"阳阳", "阴阴", "阳阴", "阴阳"}:
        reasons.append("仅四种主要形态参与可交易候选筛选")
    if direction == "neutral":
        return result
    position = _directional_position(context["smc"].get("position_assessment"), direction)
    result["position_assessment"] = position
    result["entry_advice"] = position["advice"]
    if position["blocked"]:
        reasons.append(f"位置硬门禁：{position['label']}，{position['advice']}；{position['reason']}")
    if (direction == "up" and last.close <= last.open) or (direction == "down" and last.close >= last.open):
        reasons.append("最后一根 K 线方向与形态不一致")
    first = candles[-2]
    observed_pattern = ("阳" if first.close > first.open else "阴" if first.close < first.open else "十字") + ("阳" if last.close > last.open else "阴" if last.close < last.open else "十字")
    if observed_pattern != pattern:
        reasons.append("候选形态与当前两根 K 线实体方向不一致")
    if not reversal and signal.get("direction") != direction:
        reasons.append("趋势形态与两根 K 线的净方向不一致")
    for key, threshold, title in (("activity_score", rules["min_activity_score"], "活跃度"), ("relative_volume", rules["min_relative_volume"], "相对成交额")):
        value = _metric(signal, key)
        if value is None:
            reasons.append(f"{title}数据缺失")
        elif value < threshold:
            reasons.append(f"{title} {value:.2f} 低于 {threshold:g}")
    percentile, spread = _metric(signal, "liquidity_percentile"), _metric(signal, "spread_bps")
    if percentile is not None and not 0 <= percentile <= 1:
        percentile = None
    if spread is not None and spread < 0:
        spread = None
    if not rules["allow_missing_liquidity"] and (percentile is None or spread is None):
        reasons.append("流动性分位或买卖价差缺失，严格模式不放行")
    if percentile is not None and percentile < rules["min_liquidity_percentile"]:
        reasons.append("流动性分位未达到门槛")
    if spread is not None and spread > rules["max_spread_bps"]:
        reasons.append("买卖价差过大")
    if rules["require_oi"] and (_metric(signal, "oi_change_pct") is None or _metric(signal, "oi_ratio") is None):
        reasons.append("要求持仓确认，但同周期 OI 数据缺失")
    costs = 2 * (rules["fee_bps_per_side"] + rules["slippage_bps_per_side"]) + (spread if spread is not None else rules["max_spread_bps"])
    move = abs(last.close - last.open) / last.open * 100 if reversal else abs(last.close - candles[-2].open) / candles[-2].open * 100
    result.update(effective_move_pct=move, estimated_cost_bps=costs, liquidity_ablation=rules["allow_missing_liquidity"] and (percentile is None or spread is None))
    if move < rules["min_move_pct"]:
        reasons.append(f"有效实体涨跌 {move:.3f}% 小于 {rules['min_move_pct']:g}%（过滤小信号）")
    if move - costs / 100 < rules["min_net_move_pct"]:
        reasons.append("扣除手续费、滑点及价差后的已发生位移不足；不代表未来利润")
    # Persist every built-in result so the dashboard can switch between an
    # empty strategy, either single indicator, or a multi-indicator filter
    # without changing the independent activity score.  Only the explicitly
    # enabled subset below is allowed to affect formal qualification.
    available_confirmations = [
        _confirmation(name, context, direction, reversal, last.close_time, TIMEFRAME_MS[last.timeframe], config)
        for name in ("supertrend", "smc", "ema")
    ]
    result["available_confirmations"] = available_confirmations
    by_name = {item["id"]: item for item in available_confirmations}
    confirmations = [deepcopy(by_name[name]) for name in config.enabled_indicators]
    result["confirmations"] = confirmations
    if confirmations and not (all(item["passed"] for item in confirmations) if config.confirmation_mode == "all" else any(item["passed"] for item in confirmations)):
        reasons.append("所选指标尚未满足" + ("全部共振" if config.confirmation_mode == "all" else "任一确认"))
    plan = _trade_plan(context, direction, last.close, costs)
    result["trade_plan"] = plan
    if plan is None:
        reasons.append("已确认支撑/阻力不足，无法给出可靠的失效位和目标空间")
    else:
        if plan["risk_reward"] < rules["min_risk_reward"]:
            reasons.append(f"成本后盈亏比 {plan['risk_reward']:.2f} 小于 {rules['min_risk_reward']:g}")
        if plan["net_potential_pct"] < rules["min_net_move_pct"]:
            reasons.append("最近已确认目标位的成本后空间不足")
    result["qualified"] = not reasons
    return result
