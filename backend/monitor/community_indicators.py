"""Causal WaveTrend extreme crosses, LazyBear HLC3 / 10 / 21 / 4 / ±53."""
from collections import deque
WT = "https://www.tradingview.com/script/2KE8wTuF-Indicator-WaveTrend-Oscillator-WT/"
CATALOG = [dict(id="wt_extreme_cross", name="WaveTrend · 超买超卖交叉", kind="event",
    category="震荡 / WaveTrend", author="LazyBear", source=WT,
    description="HLC3，通道10、平滑21、信号线4；WT1低于-53金叉或高于53死叉，收盘确认。无原生平仓规则。")]
from .nan_strategy1 import RULE_ID as NAN_ID, NAME as NAN_NAME, DESCRIPTION as NAN_DESCRIPTION
CATALOG.append(dict(id=NAN_ID,name=NAN_NAME,kind='event',category='自创 / WaveTrend',author='楠（WT核心：LazyBear）',source='',native_exits=True,description=NAN_DESCRIPTION))
IDS = {r['id'] for r in CATALOG}

def ema(values, period):
    out, current = [], None
    alpha = 2 / (period + 1)
    for value in values:
        if value is not None:
            current = value if current is None else current + alpha * (value-current)
        out.append(current if value is not None else None)
    return out

def sma(values, period):
    out=[];window=deque();total=0.;missing=0
    for i,value in enumerate(values):
        window.append(value)
        if value is None:missing+=1
        else:total+=value
        if len(window)>period:
            old=window.popleft()
            if old is None:missing-=1
            else:total-=old
        if i%256==0:total=sum(v for v in window if v is not None)
        out.append(total/period if len(window)==period and not missing else None)
    return out

def states(candles, ids):
    out = [{} for _ in candles]
    if not IDS.intersection(ids):
        return out
    price = [(c.high+c.low+c.close)/3 for c in candles]
    base = ema(price, 10)
    deviation = ema([abs(p-b) for p,b in zip(price, base)], 10)
    ci = [(p-b)/(.015*d) if d > 0 else None for p,b,d in zip(price, base,deviation)]
    wt1 = ema(ci, 21)
    wt2 = sma(wt1, 4)
    if NAN_ID in ids:
        from .nan_strategy1 import actions
        for i in range(35,len(candles)):out[i][NAN_ID]='neutral'
        for i,action in actions(candles,wt1,wt2):
            if action in ('long','short'):out[i][NAN_ID]='up' if action=='long' else 'down'
    if 'wt_extreme_cross' not in ids:return out
    for i in range(70, len(candles)):
        if any(x is None for x in (wt1[i],wt2[i],wt1[i-1],wt2[i-1])):
            continue
        delta, prev = wt1[i]-wt2[i], wt1[i-1]-wt2[i-1]
        cross = "up" if delta > 0 and prev <= 0 else "down" if delta < 0 and prev >= 0 else "neutral"
        out[i]["wt_extreme_cross"] = cross if (cross == "up" and wt1[i] < -53) or (cross == "down" and wt1[i] > 53) else "neutral"
    return out
