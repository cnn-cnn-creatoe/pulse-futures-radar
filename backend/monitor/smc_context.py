"""Descriptive SMC context. Uses completed bars only; never gates signals."""
from statistics import mean

def context(bars, price=None, direction=None, level=None):
    bars=list(bars)
    if len(bars)<22:
        return dict(ready=False,note='历史不足，量价风险暂不判断')
    if any(b.open_time!=a.close_time+1 for a,b in zip(bars,bars[1:])):
        return dict(ready=False,note='历史有缺口，量价风险暂不判断')
    last=bars[-1];price=last.close if price is None else price
    tr=[max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(bars,bars[1:])]
    atr=mean(tr[-14:])
    base=mean(b.quote_volume for b in bars[-21:-1])
    volume_ratio=last.quote_volume/base if base>0 else None
    range_ratio=tr[-1]/atr if atr>0 else None
    streak=0;color=1 if last.close>last.open else -1 if last.close<last.open else 0
    if color:
        for b in reversed(bars):
            if (b.close-b.open)*color<=0:break
            streak+=1
    warnings=[]
    if color<0 and streak>=3:warnings.append(f'前{streak}根连续阴线，不宜仅凭支撑抢多，等待止跌')
    if color>0 and streak>=3:warnings.append(f'前{streak}根连续阳线，不宜仅凭压力抢空，等待转弱')
    if volume_ratio is not None and volume_ratio<.8:warnings.append('前一根缩量，突破参与度需观察')
    if range_ratio is not None and range_ratio>2:warnings.append('前一根波幅超过2倍ATR，注意追价与滑点')
    extension=abs(price-level)/atr if level is not None and atr>0 else None
    if extension is not None and extension>1:warnings.append('触发价距结构位超过1倍ATR，注意追涨杀跌')
    return dict(ready=True,asof=last.close_time,streak=streak,streak_side='阴' if color<0 else '阳' if color>0 else '平',
                volume_ratio=volume_ratio,atr=atr,atr_pct=atr/last.close*100 if last.close>0 else None,
                range_ratio=range_ratio,extension_atr=extension,
                direction='偏多观察' if direction=='long' else '偏空观察' if direction=='short' else '等待结构信号',
                warnings=warnings,note='仅风险提示，未经充分样本外验证，不作为开仓许可')
