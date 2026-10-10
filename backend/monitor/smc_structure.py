"""SMC structure adaptation of LuxAlgo, CC BY-NC-SA 4.0.
Source: user supplied 指标.txt, Smart Money Concepts [LuxAlgo].
BOS/CHoCH and order blocks; swing=50, internal=5, confluence disabled.
Change: preview unclosed bars, retain first observed alerts outside Pine state.
License: https://creativecommons.org/licenses/by-nc-sa/4.0/
"""
from collections import deque
from copy import deepcopy

RULE_ID = 'smc_equity_structure'
NAME = '美股 SMC · 盘中结构提醒'
CATALOG = dict(id=RULE_ID, name=NAME, kind='event', description='自选美股；内部5 / 波段50，BOS与CHoCH；盘中实时突破即提醒，同根同类去重。含内部/波段订单块与区域反应；不含FVG、EQH/EQL。')

class Structure:
    def __init__(self):
        self.bars = deque(maxlen=51)
        self.count = 0
        self.parsed = deque(maxlen=51)
        self.tr_seed = deque(maxlen=200)
        self.atr200 = None
        self.blocks = {'internal': [], 'swing': []}
        self.invalidated = []
        self.groups = {k: dict(leg=0, bias=0, high=None, low=None, high_crossed=False, low_crossed=False) for k in ('internal','swing')}

    def preview(self, candle):
        return deepcopy(self).advance(candle)

    def levels(self):
        return {name: dict(support=g['low'], resistance=g['high'],
                           support_broken=g['low_crossed'], resistance_broken=g['high_crossed'])
                for name, g in self.groups.items()}

    def preview_details(self, candle):
        preview = deepcopy(self)
        signals = preview.advance(candle)
        return signals, preview.levels()

    def preview_full(self, candle):
        preview = deepcopy(self)
        signals = preview.advance(candle)
        return signals, preview.levels(), preview.zone_snapshot(candle)

    def zone_snapshot(self, candle=None):
        candle = candle or (self.bars[-1] if self.bars else None)
        items=[]
        for name in ('internal','swing'):
            for block in self.blocks[name][:5]:
                z=dict(block)
                supply=z['side']=='supply'
                inside=candle and z['lower']<=candle.close<=z['upper']
                touched=candle and candle.high>=z['lower'] and candle.low<=z['upper']
                if candle and candle.open_time==z['formed_at']:
                    phase='formed'
                elif inside:phase='inside'
                elif touched and supply and candle.close<z['lower'] and candle.close<candle.open:phase='rejection'
                elif touched and not supply and candle.close>z['upper'] and candle.close>candle.open:phase='bounce'
                else:phase='waiting'
                z.update(phase=phase,confirmed=bool(candle and candle.closed))
                items.append(z)
        items.extend(dict(z,phase='invalidated',confirmed=bool(candle and candle.closed)) for z in self.invalidated)
        return dict(items=items,atr_ready=self.atr200 is not None,history_bars=self.count,
                    note='ATR200过滤；高低价越界失效；内部/波段各显示最近5个有效订单块。反应为新增规则，非原脚本买卖信号。')

    def advance(self, candle):
        if self.bars and candle.open_time != self.bars[-1].close_time + 1:
            raise ValueError('SMC历史K线有缺口，需重新预热')
        previous = deepcopy(self.groups)
        previous_close = self.bars[-1].close if self.bars else None
        tr=max(candle.high-candle.low,abs(candle.high-previous_close),abs(candle.low-previous_close)) if previous_close is not None else candle.high-candle.low
        self.tr_seed.append(tr)
        if self.atr200 is not None:self.atr200=(self.atr200*199+tr)/200
        elif len(self.tr_seed)==200:self.atr200=sum(self.tr_seed)/200
        volatile=self.atr200 is not None and candle.high-candle.low>=2*self.atr200
        self.parsed.append(dict(high=candle.low if volatile else candle.high,low=candle.high if volatile else candle.low,time=candle.open_time))
        self.invalidated=[]
        self.bars.append(candle)
        self.count += 1
        bars = list(self.bars)
        for name, size in (('swing',50),('internal',5)):
            if len(bars) <= size: continue
            group = self.groups[name]
            pivot, window = bars[-size-1], bars[-size:]
            leg = 0 if pivot.high > max(c.high for c in window) else 1 if pivot.low < min(c.low for c in window) else group['leg']
            if leg != group['leg']:
                side = 'low' if leg else 'high'
                group[side] = getattr(pivot,side)
                group[side+'_crossed'] = False
                # Pine slice(pivotIndex, bar_index) excludes the current bar.
                window_parsed=list(self.parsed)[-size-1:-1]
                group[side+'_candidate']=dict(min(window_parsed,key=lambda p:p['low']) if side=='high' else max(window_parsed,key=lambda p:p['high']))
            group['leg'] = leg
            if len(self.parsed)>1:
                prior_parsed=self.parsed[-2]
                for side,field in (('high','low'),('low','high')):
                    old=group.get(side+'_candidate')
                    if old and ((side=='high' and prior_parsed[field]<old[field]) or (side=='low' and prior_parsed[field]>old[field])):
                        group[side+'_candidate']=dict(prior_parsed)
        events = []
        for name in ('internal','swing'):
            group = self.groups[name]
            for side, direction, bias in (('high','long',1),('low','short',-1)):
                level, old_level = group[side], previous[name][side]
                if level is None or old_level is None or previous_close is None: continue
                # Pine's comparison with an undefined swing level is not true.
                swing = self.groups['swing'][side]
                if name == 'internal' and (swing is None or level == swing): continue
                cross = (candle.close > level and previous_close <= old_level) if bias == 1 else (candle.close < level and previous_close >= old_level)
                if cross and not group[side+'_crossed']:
                    tag = 'CHoCH' if group['bias'] == -bias else 'BOS'
                    group[side+'_crossed'] = True
                    group['bias'] = bias
                    events.append(dict(structure=name, tag=tag, direction=direction, level=level))
                    candidate=group.get(side+'_candidate')
                    if candidate and candidate['high']>=candidate['low']:
                        block=dict(id=f"{name}:{direction}:{candidate['time']}:{candle.open_time}",structure=name,
                            side='demand' if bias==1 else 'supply',lower=candidate['low'],upper=candidate['high'],
                            origin_at=candidate['time'],formed_at=candle.open_time)
                        self.blocks[name].insert(0,block)
                        self.blocks[name]=self.blocks[name][:100]
        for name in ('internal','swing'):
            kept=[]
            for block in self.blocks[name]:
                invalid=candle.high>block['upper'] if block['side']=='supply' else candle.low<block['lower']
                if invalid:self.invalidated.append(dict(block))
                else:kept.append(block)
            self.blocks[name]=kept
        return events
