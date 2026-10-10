"""Construct a newly closed selected period only from complete local bars."""
from dataclasses import replace
from math import isfinite
from .periods import floor_bar,shift,FIXED,NATIVE

TIMEFRAMES=tuple(tf for tf in NATIVE if tf!='1s')

def valid(c):
    return (c.closed and all(isfinite(v) for v in (c.open,c.high,c.low,c.close,c.volume,c.quote_volume,c.taker_buy_quote))
            and 0<c.low<=min(c.open,c.close)<=max(c.open,c.close)<=c.high
            and c.volume>=0 and 0<=c.taker_buy_quote<=c.quote_volume and c.trades>=0)

def closed_history(store,minute,timeframe):
    if timeframe not in TIMEFRAMES or minute.timeframe!='1m' or not valid(minute):return []
    if minute.open_time%60000 or minute.close_time!=minute.open_time+59999:return []
    start=floor_bar(minute.open_time,timeframe)
    if shift(start,timeframe,1)-1!=minute.close_time:return []
    exact=store.get_candles(minute.symbol,timeframe,start=start,end=start,limit=1)
    closed=next((c for c in exact if valid(c) and c.symbol==minute.symbol and c.timeframe==timeframe and c.open_time==start and c.close_time==minute.close_time),None)
    if timeframe=='1m':closed=minute
    if closed is None:
        for child in ('4h','1h','15m','5m','1m'):
            step=FIXED[child];span=minute.close_time+1-start
            if step>=span or span%step or floor_bar(start,child)!=start:continue
            count=span//step
            members=store.get_candles(minute.symbol,child,start=start,end=minute.close_time,limit=count+1)
            if len(members)!=count:continue
            if any(c.symbol!=minute.symbol or c.timeframe!=child or not valid(c) or c.open_time!=start+i*step or c.close_time!=start+(i+1)*step-1 for i,c in enumerate(members)):continue
            closed=replace(minute,timeframe=timeframe,open_time=start,open=members[0].open,high=max(c.high for c in members),low=min(c.low for c in members),volume=sum(c.volume for c in members),quote_volume=sum(c.quote_volume for c in members),taker_buy_quote=sum(c.taker_buy_quote for c in members),trades=sum(c.trades for c in members))
            store.save_candles([closed]);break
    if closed is None:return []
    history=store.get_candles(minute.symbol,timeframe,end=start-1,limit=4000)
    return history+[closed]
