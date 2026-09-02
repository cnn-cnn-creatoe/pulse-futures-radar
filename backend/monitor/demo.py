"""Reproducible synthetic fixtures, evaluated by the exact production engine.
Never stored in the market database and never represented as historical quotes.
"""
from datetime import datetime, timezone
import math
import random
from .engine import Candle, EngineConfig, LOOKBACK, MONITOR_TIMEFRAMES, TIMEFRAME_MS, detect_signal
from .indicators import IndicatorConfig, evaluate_candidate

ASSETS = [('BTCUSDT', 64520), ('ETHUSDT', 2740), ('SOLUSDT', 148), ('1000PEPEUSDT', .0098), ('BNBUSDT', 573), ('DOGEUSDT', .112), ('XRPUSDT', .58), ('SUIUSDT', 1.06), ('WIFUSDT', 1.65), ('AVAXUSDT', 25.7), ('ETHUSDC', 2740), ('BTCUSDC', 64520)]

def demo_signals(
    config: EngineConfig | None = None,
    *,
    indicator_config: IndicatorConfig | None = None,
    quality: dict | None = None,
) -> list[dict]:
    """Compute deterministic engine and indicator results on synthetic prices.

    The eight identities cover four candle patterns on the active 5m/15m scope. Indicators
    receive the complete generated history, not the 50-bar detail excerpt.
    Quality rejections and missing confirmations are retained honestly; demo
    presentation must not turn an unqualified fixture into a trade signal.
    Optional policies let the demo preview the same saved settings as live.
    """
    signals = []
    anchor = int(datetime(2026, 8, 31, 10, tzinfo=timezone.utc).timestamp() * 1000)
    for index in range(len(MONITOR_TIMEFRAMES) * 4):
        tf = MONITOR_TIMEFRAMES[index // 4]
        pattern = index % 4
        symbol, price = ASSETS[index % len(ASSETS)]
        rng = random.Random(871 + index)
        step = TIMEFRAME_MS[tf]
        count = LOOKBACK[tf]['minimum_bars'] + 20
        end = (anchor // step - index % 4) * step
        start = end - count * step
        scale = .00055 * (1 + math.log2(step / 60000 + 1) * .55)
        candles = []
        qbase = [800000, 430000, 310000, 120000, 90000, 135000, 110000, 75000, 44000, 78000, 170000, 400000][index % 12] * (step / 60000) ** .62
        for k in range(count):
            op = price
            delta = rng.uniform(-.7, .7) * scale
            mult = 1
            if k >= count - 2:
                sign = [(1,1),(-1,-1),(1,-1),(-1,1)][pattern][k - count + 2]
                delta = sign * scale * (3.1 + (index % 5) * .55) * (1.15 if k == count-1 else 1)
                mult = 2.0 + (index % 7) * .6
            price = op * (1 + delta)
            wick = op * scale * (rng.uniform(.3,.5) if k < count-2 else .52)
            q = qbase * rng.uniform(.8,1.2) * mult
            buy = (.66 if delta > 0 else .32) if k >= count-2 else rng.uniform(.43,.57)
            candles.append(Candle(symbol,tf,start+k*step,start+(k+1)*step-1,op,max(op,price)+wick,min(op,price)-wick,price,q,q*buy,int(q/max(price,1)*6),q/price,True))
        enrichment = {'oi_change_pct': (1 if index%3 else -1)*(1.2+index%4*.5), 'oi_ratio': 1.3+index%6*.7,'liquidity_percentile':max(.1,.99-index%12*.068),'spread_bps':2.0+index%9*1.1}
        signal = detect_signal(candles,config=config,enrichment=enrichment,tick_size=price*1e-7)
        if signal:
            signal = evaluate_candidate(signal, candles, config=indicator_config, quality=quality)
            signal.update(source='demo',synthetic=True,created_at=signal['close_time'],token_address=None,metadata_note='演示样本，无真实代币地址映射')
            signal['quality_schema_version'] = 2
            signals.append(signal)
    return sorted(signals,key=lambda a:a['close_time'],reverse=True)
