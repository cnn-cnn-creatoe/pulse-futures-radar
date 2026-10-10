"""Ephemeral selected-period conditions from a real live one-minute candle."""
from dataclasses import replace
from datetime import datetime,timezone
from .engine import Candle
from .periods import floor_bar,shift


def evaluate(store,live,settings,symbol,timeframe,kind,rule,now):
    from .workbench import signal_stream,EVENT_IDS
    from .quant_research import STRATEGY_INDICATORS,signal_rows
    if kind not in ('indicator','quant') or rule not in (STRATEGY_INDICATORS if kind=='quant' else EVENT_IDS):raise ValueError('请选择已接入的指标或策略')
    if kind=='quant' and rule in ('hammers_stars','threecommas_default'):
        return dict(available=False,note='原版入场要求barstate.isconfirmed；本页不将未收盘形态冒充已确认入场。原生止盈止损触价在收盘记录中确认。')
    if not live or not 0<=now-live.get('event_time',0)<=15000:return dict(available=False,note='实时行情暂未到达；不使用旧报价生成盘中信号')
    start=floor_bar(live['open_time'],timeframe);end=shift(start,timeframe,1)-1
    minutes=store.get_candles(symbol,'1m',start=start,end=live['open_time']-1,limit=50000)
    minutes=[c for c in minutes if c.closed and c.open_time>=start and c.close_time<live['open_time']]
    fields=Candle.__dataclass_fields__
    current=Candle(**{k:v for k,v in live.items() if k in fields})
    parts=minutes+[current]
    if parts[0].open_time!=start or any(b.open_time!=a.close_time+1 for a,b in zip(parts,parts[1:])):
        return dict(available=False,note='所选周期内分钟数据有缺口，正在等待补齐')
    provisional=replace(current,timeframe=timeframe,open_time=start,close_time=end,open=parts[0].open,high=max(c.high for c in parts),low=min(c.low for c in parts),volume=sum(c.volume for c in parts),quote_volume=sum(c.quote_volume for c in parts),taker_buy_quote=sum(c.taker_buy_quote for c in parts),trades=sum(c.trades for c in parts))
    history=store.get_candles(symbol,timeframe,end=start-1,limit=2020)
    # Reuse formulas with a temporary calculation copy only. Neither this copy
    # nor its scheduled closing timestamp is written to historical records.
    bars=history+[replace(provisional,closed=True)]
    ids=STRATEGY_INDICATORS[rule] if kind=='quant' else [rule]
    contiguous=1
    for i in range(len(bars)-1,0,-1):
        if bars[i].open_time!=bars[i-1].close_time+1:break
        contiguous+=1
    warmup=35 if rule=='nan_strategy1_oversold' else 70
    if contiguous<=warmup:
        return dict(available=False,note=f'该规则需要{warmup}根连续历史K线预热；当前仅{contiguous-1}根')
    iso=lambda t:datetime.fromtimestamp(t/1000,timezone.utc).isoformat()
    opts=dict(indicators=ids,combination='all',pattern='none',engine=kind,strategy=rule if kind=='quant' else None,start=iso(history[0].open_time if history else start),end=iso(end))
    events=list(signal_stream(bars,settings,[],opts))
    rows=signal_rows(events,rule if kind=='quant' else None)
    latest=[dict(action=r['signal_kind'],direction=r['direction'],price=current.close) for r in rows if r['signal_time']==end]
    return dict(available=True,observed_at=live['event_time'],bar_open_time=start,bar_close_time=end,signals=latest,note='盘中条件，未收盘；价格变化可能撤回。不是已确认委托或成交。',candle=provisional.to_dict())
