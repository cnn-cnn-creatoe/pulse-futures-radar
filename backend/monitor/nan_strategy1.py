"""Faithful closed-bar port of the user's WaveTrend B indicator.
No synthetic stops, cross requirement, zone deduplication or repeated entries.
"""
RULE_ID='nan_strategy1_oversold'
NAME='楠策略1超买超卖'
DESCRIPTION='WT1超买超卖区极值后转折入场；上一根WT双线位置过滤；相反转折先平仓，满足完整条件才反手。不加仓、不额外设止盈止损。'

def actions(candles, wt1=None, wt2=None, one_per_zone=False):
    if wt1 is None:
        from .community_indicators import ema,sma
        prices=[(c.high+c.low+c.close)/3 for c in candles]
        esa=ema(prices,10)
        dev=ema([abs(a-b) for a,b in zip(prices,esa)],10)
        wt1=ema([(a-b)/(.015*d) if d>0 else None for a,b,d in zip(prices,esa,dev)],21)
        wt2=sma(wt1,4)
    result=[];pos=0;long_armed=short_armed=True
    for i,c in enumerate(candles):
        if i<35 or any(v is None for v in (wt1[i],wt1[i-1],wt1[i-2],wt2[i-1])):continue
        up=wt1[i-1]<wt1[i-2] and wt1[i]>wt1[i-1]
        down=wt1[i-1]>wt1[i-2] and wt1[i]<wt1[i-1]
        long=up and wt1[i-1]<=-53 and wt1[i-1]<wt2[i-1]
        short=down and wt1[i-1]>=53 and wt1[i-1]>wt2[i-1]
        if wt1[i-1]>-53:long_armed=True
        if wt1[i-1]<53:short_armed=True
        if pos==1 and down:result.append((i,'close_long'));pos=0
        elif pos==-1 and up:result.append((i,'close_short'));pos=0
        if pos==0:
            if long and (not one_per_zone or long_armed):
                result.append((i,'long'));pos=1;long_armed=False
            elif short and (not one_per_zone or short_armed):
                result.append((i,'short'));pos=-1;short_armed=False
    return result

def events(candles,start,end,cancelled=lambda:False):
    from .engine import _kind
    labels={'long':'做多','short':'做空','close_long':'卖出平多','close_short':'买回平空'}
    for i,action in actions(candles):
        if cancelled():return
        c=candles[i]
        if not start<=c.close_time<=end:continue
        pattern=_kind(candles[i-1],1e-12)+_kind(c,1e-12)
        side='long' if action in ('long','close_long') else 'short'
        yield dict(id=f'{c.symbol}:{c.timeframe}:{c.close_time}:{action}',symbol=c.symbol,timeframe=c.timeframe,
            close_time=c.close_time,price=c.close,direction=side,pattern=pattern,segment_start=candles[0].open_time,
            signal_kind=labels[action],action=action,indicator_results={RULE_ID:True},rule_id=RULE_ID)

def outcomes(signals,candles,options):
    """Pair explicit close events, preserving close-before-entry on a reversal bar."""
    from .quant_research import signal_rows
    rows=[];position=None
    indices={c.close_time:i for i,c in enumerate(candles)}
    on_close=options.get('fill_mode','next_open')=='signal_close'
    fee=options.get('fee_bps',5)/10000;slip=options.get('slippage_bps',3)/10000
    for signal in signals:
        index=indices[signal['close_time']]+(0 if on_close else 1)
        bar=candles[index] if index<len(candles) else None
        price=(bar.close if on_close else bar.open) if bar else None
        at=(bar.close_time if on_close else bar.open_time) if bar else None
        if signal['action'].startswith('close_'):
            if position is not None and signal['action']=='close_'+position['direction']:
                position['exit_signal_time']=signal['close_time']
                if price is not None:
                    side=1 if position['direction']=='long' else -1
                    entry=position['entry_price'];exit_price=price*(1-side*slip)
                    net=(side*(exit_price-entry)-fee*(entry+exit_price))/entry*100
                    position.update(exit_time=at,exit_price=exit_price,exit_raw_price=price,exit_phase='close' if on_close else 'open',net_return_pct=net,status='已平仓 · 原代码相反转折')
                position=None
        elif position is None:
            row=signal_rows([signal])[0]
            row.update(rule_id=RULE_ID,source_order_rule=RULE_ID,action_basis='原代码先平后开；相反转折平仓',entry_phase='close' if on_close else 'open')
            if price is not None:
                side=1 if signal['direction']=='long' else -1
                row.update(entry_time=at,entry_price=price*(1+side*slip),status='持仓中 · 等待原代码平仓')
                position=row
            rows.append(row)
    return rows
