"""Chronological, shared-wallet USDT research simulation. Never places orders.

Fixed margin per position, one position per symbol, isolated risk approximation.
Last-trade OHLC is a proxy, not Binance mark-price/tier liquidation replication.
"""
from collections import defaultdict
from heapq import merge
from itertools import groupby


def simulate(rows, market, options, start, end, cancelled=lambda: False):
    initial = float(options["initial_capital"])
    margin_size, leverage = options["margin_per_trade"], options["leverage"]
    fee, slip = options["fee_bps"]/10000, options["slippage_bps"]/10000
    maintenance = options["maintenance_margin_pct"]/100
    balance = initial
    positions, marks = {}, {}
    entries = defaultdict(list)
    completed, skipped, liquidations, fees = [], 0, 0, 0.
    for row in rows:
        row["account_status"] = "等待下一根开盘"
        if row.get("entry_time") is not None and start <= row["entry_time"] <= end:
            entries[row["entry_time"]].append(row)

    def stream(key, candles):
        for c in candles:
            if c.open_time >= start and c.close_time <= end:
                yield (c.open_time,0,key,c)
                yield (c.close_time,1,key,c)

    def equity():
        return balance + sum(p["side"]*p["qty"]*(marks.get(p["key"],p["entry"])-p["entry"]) for p in positions.values())

    def close(symbol, raw, at, reason, liquidation=False):
        nonlocal balance, fees, liquidations
        p = positions.pop(symbol)
        price = raw*(1-p["side"]*slip)
        charge = abs(price*p["qty"])*fee
        gross = p["side"]*p["qty"]*(price-p["entry"])
        # Isolated collateral is the maximum collateral lost in this model.
        # Bankruptcy handling and exchange clearance fees are deliberately not asserted.
        settled = max(-p["margin"],gross-charge) if liquidation else gross-charge
        balance += settled
        fees += charge
        pnl = settled-p["entry_fee"]
        p["row"].update(account_status=reason,account_exit_time=at,account_exit_price=price,
            account_pnl=pnl,account_fees=p["entry_fee"]+charge,account_return_pct=pnl/p["margin"]*100)
        completed.append(pnl)
        liquidations += int(liquidation)

    curve = [dict(time=start,equity=initial,balance=initial,margin=0,unrealized=0,drawdown_pct=0)]
    peak, max_dd = initial, 0.
    events = merge(*(stream(key,cs) for key,cs in sorted(market.items())),key=lambda e:(e[0],e[1],e[2]))
    for at, batch in groupby(events,key=lambda e:e[0]):
        if cancelled():
            raise ValueError("已停止账户回测")
        batch = list(batch)
        available = {key:c for _,phase,key,c in batch if phase==0}
        closing_available={key:c for _,phase,key,c in batch if phase==1}
        # First publish only prices observable at this timestamp.
        for _,phase,key,c in batch:
            marks[key] = c.open if phase==0 else c.close
        for _,phase,key,c in batch:
            symbol=key[0]
            p=positions.get(symbol)
            if not p or p["key"]!=key:
                continue
            threshold=(p["entry"]-p["side"]*p["margin"]/p["qty"])/(1-p["side"]*(maintenance+fee))
            adverse=c.open if phase==0 else (c.low if p["side"]==1 else c.high)
            native_exit=phase==1 and p['exit_time']==at and p['row'].get('exit_phase')=='intrabar'
            if native_exit:
                path=p['row']['exit_path']
                adverse=min(path) if p['side']==1 else max(path)
            hit=adverse <= threshold if p["side"]==1 else adverse >= threshold
            if hit:
                raw = min(c.open,threshold) if p["side"]==1 else max(c.open,threshold)
                close(symbol,raw,at,"风险强平 · OHLC近似",True)
            elif p["exit_time"] == at:
                close(symbol,p['row']['exit_raw_price'] if native_exit else c.open if phase==0 else c.close,at,"已平仓")
        for row in sorted(entries.get(at,[]),key=lambda r:(r["symbol"],r["timeframe"],r["id"])):
            symbol,key=row["symbol"],(row["symbol"],row["timeframe"])
            on_close=row.get('entry_phase')=='close'
            selected=closing_available if on_close else available
            available_margin=max(0,balance-sum(p['margin'] for p in positions.values()))
            margin_size=available_margin/(1+leverage*fee) if options.get('capital_mode')=='equity' else options['margin_per_trade']
            reason=None
            if margin_size<=0: reason='跳过 · 资金耗尽'
            elif symbol in positions: reason="跳过 · 同币已有仓位"
            elif len(positions)>=options["max_positions"]: reason="跳过 · 持仓数上限"
            elif key not in selected: reason="跳过 · 缺少入场 K 线"
            elif balance-sum(p["margin"] for p in positions.values()) + 1e-9 < margin_size*(1+leverage*fee): reason="跳过 · 可用资金不足"
            if reason:
                row["account_status"]=reason; skipped+=1; continue
            side=1 if row["direction"]=="long" else -1
            price=(selected[key].close if on_close else selected[key].open)*(1+side*slip)
            qty=margin_size*leverage/price
            charge=qty*price*fee
            balance-=charge; fees+=charge
            row.update(account_status="持仓中",quantity=qty,margin=margin_size,notional=qty*price,
                account_entry_price=price,account_entry_time=at,account_fees=charge,
                account_exit_price=None,account_exit_time=None,account_pnl=None)
            positions[symbol]=dict(row=row,key=key,side=side,qty=qty,entry=price,entry_fee=charge,margin=margin_size,exit_time=row.get("exit_time"))
        value=equity()
        peak=max(peak,value)
        dd=max(0,(peak-value)/peak*100) if peak else 0
        max_dd=max(max_dd,dd)
        curve.append(dict(time=at,equity=value,balance=balance,margin=sum(p["margin"] for p in positions.values()),unrealized=value-balance,drawdown_pct=dd))
    value=equity()
    for p in positions.values():
        p["row"].update(account_unrealized=p["side"]*p["qty"]*(marks[p["key"]]-p["entry"]))
    # Keep extrema per bucket so display compression cannot hide drawdown spikes.
    if len(curve)>1200:
        step=(len(curve)+299)//300
        selected={0:curve[0],len(curve)-1:curve[-1]}
        for i in range(0,len(curve),step):
            group=list(enumerate(curve[i:i+step],i))
            for index,point in [group[0],group[-1],min(group,key=lambda p:p[1]["equity"]),max(group,key=lambda p:p[1]["equity"])]: selected[index]=point
        curve=[v for _,v in sorted(selected.items())]
    gains=sum(p for p in completed if p>0); losses=-sum(p for p in completed if p<0)
    return dict(initial_capital=initial,final_equity=value,balance=balance,unrealized=value-balance,
        net_profit=value-initial,return_pct=(value/initial-1)*100,max_drawdown_pct=max_dd,
        closed=len(completed),open=len(positions),skipped=skipped,liquidations=liquidations,fees=fees,
        win_rate=sum(p>0 for p in completed)/len(completed) if completed else None,
        profit_factor=gains/losses if losses else None,curve=curve,
        model=("共享钱包＋每次全部可用权益复利（预留入场手续费）；" if options.get('capital_mode')=='equity' else "共享钱包＋固定逐仓保证金；")+"按选择的成交时点估值，OHLC近似风控。",
        limitations=["未计资金费率、标记价格、真实保证金档位、强平清算费及成交量限制；不是币安真实强平回放。",
            "同币不同周期只允许一笔持仓；同一时间先平仓再按币种、周期顺序尝试开仓，资金不足记录跳过。",
            ("每次按平仓后的可用权益重新计算保证金；期末浮盈亏计入权益。" if options.get('capital_mode')=='equity' else "每笔保证金固定；未平仓浮盈亏计入权益，不追加保证金。"),
            "回撤按可观测开盘／收盘时点计算，不能完整重建分钟内账户波动。"])
