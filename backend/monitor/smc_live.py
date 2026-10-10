"""Selected equity native-kline streams, isolated from close-only alerts."""
import asyncio
import json
import sqlite3
import time
import logging
from datetime import datetime, timedelta, timezone
import aiohttp
from .collector import WS_BASE, rest_candle, eligible_contract
from .engine import Candle
from .periods import shift
from .delivery_profiles import profiles
from .smc_structure import Structure, RULE_ID, NAME
from .smc_context import context
from .smc_zones import zone_signals


def equity(meta):
    return eligible_contract(meta) and meta.get('underlyingType') in ('EQUITY','STOCK')

class SmcLive:
    def __init__(self, market, telegram, path):
        self.market, self.telegram = market, telegram
        self.task = None
        self.workers = {}
        self.states = {}
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute('CREATE TABLE IF NOT EXISTS triggers (id TEXT PRIMARY KEY, observed INTEGER, payload TEXT)')
        self.db.execute('CREATE INDEX IF NOT EXISTS triggers_observed ON triggers(observed)')
        self.db.commit()

    def desired(self):
        # Local monitoring is always independent of Telegram configuration.
        wanted = {(symbol, period) for symbol, meta in self.market._metadata.items()
                  if equity(meta) for period in ('1h', '4h', '1d')}
        # Keep existing subscribers' additional periods without changing their rules.
        if self.telegram and self.telegram._accept_events:
            wanted.update((symbol, period)
                for member in self.telegram.state.subscribers() if member['preferences']['signals_enabled']
                for profile in profiles(member['preferences']) if profile['rule_id'] == RULE_ID
                for symbol in profile['symbols'] if equity(self.market._metadata.get(symbol, {}))
                for period in profile['timeframes'])
        return wanted

    def start(self):
        self.task = asyncio.create_task(self.run())

    async def close(self):
        tasks = [t for t in [self.task,*self.workers.values()] if t]
        for t in tasks: t.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        self.db.close()

    def status(self, day=None):
        wanted = sorted(self.desired())
        items = [dict(symbol=symbol, timeframe=period, **self.states.get((symbol, period),
                 dict(state='warming', note='等待本机加载历史行情，与 Telegram 推送无关')))
                 for symbol, period in wanted]
        for item in items:
            if item.get('last_event') and self.market.now()-item['last_event']>15000:
                item.update(state='waiting',note='最近15秒暂无新行情，等待更新')
        tz = timezone(timedelta(hours=8))
        selected = datetime.strptime(day, '%Y-%m-%d').replace(tzinfo=tz) if day else datetime.fromtimestamp(self.market.now()/1000, tz).replace(hour=0, minute=0, second=0, microsecond=0)
        start = int(selected.timestamp()*1000)
        recent = [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM triggers WHERE observed>=? AND observed<? ORDER BY observed DESC,id', (start,start+86400000))]
        days = [r[0] for r in self.db.execute("SELECT DISTINCT date(observed/1000, 'unixepoch', '+8 hours') AS day FROM triggers ORDER BY day DESC")]
        return dict(items=items, recent=recent, date=selected.strftime('%Y-%m-%d'), days=days, desired=len(self.desired()),
                    note='本机默认全选可交易美股及1h/4h/1d，无需配置Telegram；新增上市品种自动加入。')

    async def run(self):
        while True:
            desired = sorted(self.desired())
            # Combined streams keep a full-equity selection below the HTTP
            # connector's connection budget. Each group has at most 80 streams.
            groups = {tuple(desired[i:i+80]) for i in range(0,len(desired),80)}
            for group in list(self.workers):
                if group not in groups:
                    task=self.workers.pop(group);task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
            wanted=set(desired)
            for key in list(self.states):
                if key not in wanted:self.states.pop(key,None)
            for group in sorted(groups):
                if group not in self.workers or self.workers[group].done():
                    for key in group:self.states[key]=dict(state='warming',note='排队加载历史，完成后实时监测')
                    self.workers[group] = asyncio.create_task(self.watch(group))
            await asyncio.sleep(2)

    def publish(self, candle, event_time, signals, prior_bars=None, zone_context=None):
        if not signals:return
        if (candle.symbol,candle.timeframe) not in self.desired(): return
        if not 0 <= self.market.now()-event_time <= 5000: return
        for signal in signals:
            identity = f"smc:{candle.symbol}:{candle.timeframe}:{candle.open_time}:{signal['structure']}:{signal['tag']}:{signal['direction']}"
            if signal.get('zone'):identity+=':'+signal['zone']['id']
            if self.db.execute('SELECT 1 FROM triggers WHERE id=?',(identity,)).fetchone(): continue
            event = dict(id=identity,source='live',rule_kind='indicator',rule_id=RULE_ID,rule_name=NAME,
                         symbol=candle.symbol,timeframe=candle.timeframe,close_time=candle.close_time,
                         observed_at=event_time,entry_direction=signal['direction'],signal_price=candle.close,
                         structure_level=signal['level'],intrabar=not candle.closed,
                         signal_kind=('内部' if signal['structure']=='internal' else '波段')+' '+signal['tag'])
            event['event_type']=signal.get('event_type','structure')
            if signal.get('zone'):event['zone']=signal['zone']
            if zone_context is not None:
                event['zone_context']=zone_context
                conflicts=[z for z in zone_context['items'] if z['phase']=='inside' and z['side']==('supply' if signal['direction']=='long' else 'demand')]
                if event['event_type']=='structure' and conflicts:
                    event['zone_conflict']='结构向上突破，但价格仍在供给压力区内，暂不追多' if signal['direction']=='long' else '结构向下跌破，但价格仍在需求支撑区内，暂不追空'
            if prior_bars is not None:
                event['assessment']=context(prior_bars,candle.close,signal['direction'],signal['level'])
            self.db.execute('INSERT OR IGNORE INTO triggers VALUES (?,?,?)',(identity,event_time,json.dumps(event,ensure_ascii=False)))
            self.db.commit()
            # Persist first; unavailable/full/broken delivery must not lose local signals.
            if self.telegram and self.telegram._accept_events:
                try:
                    self.telegram.enqueue('signal',event)
                except Exception:
                    logging.getLogger(__name__).exception('SMC 信号已本地保存，推送入队失败')

    async def watch(self, group):
        while True:
            machines={};last_events={};zone_states={}
            try:
                for symbol,timeframe in group:
                    key=(symbol,timeframe)
                    self.states[key]=dict(state='warming',note='正在加载原生周期历史')
                    rows=await self.market.fetch_klines(dict(symbol=symbol,interval=timeframe,limit=499,endTime=self.market.now()))
                    machine=Structure()
                    for row in rows:
                        c=rest_candle(symbol,timeframe,row,self.market.now())
                        if not c.closed:continue
                        if not self.market._valid_candle(c):raise ValueError('无效历史K线')
                        if machine.bars and c.open_time!=machine.bars[-1].close_time+1:machine=Structure()
                        machine.advance(c)
                    if machine.count<51:
                        self.states[key]=dict(state='waiting',note='连续历史不足51根，等待结构预热')
                        continue
                    machines[key]=machine
                    self.states[key]=dict(state='warming',note='历史区域已计算，等待本组实时连接',history_bars=machine.count,
                        levels=machine.levels(),price=machine.bars[-1].close,price_time=machine.bars[-1].close_time,
                        assessment=context(machine.bars),zones=machine.zone_snapshot())
                if not machines:
                    await asyncio.sleep(60)
                    continue
                await self.market._pace_ws_connection()
                streams='/'.join(symbol.lower()+'@kline_'+tf for symbol,tf in machines)
                async with self.market._session.ws_connect(WS_BASE+'?streams='+streams,
                        heartbeat=30,receive_timeout=60,autoping=True) as ws:
                    began=time.monotonic()
                    for key,machine in machines.items():
                        self.states[key]=dict(state='connected',note='已连接，等待实时行情',history_bars=machine.count,levels=machine.levels(),price=machine.bars[-1].close,price_time=machine.bars[-1].close_time,assessment=context(machine.bars),zones=machine.zone_snapshot())
                    async for msg in ws:
                        # Revisit history-insufficient members periodically, while
                        # retaining the normal 22-hour rollover for complete groups.
                        if time.monotonic()-began>(600 if len(machines)<len(group) else 22*3600):break
                        if msg.type!=aiohttp.WSMsgType.TEXT:
                            if msg.type in (aiohttp.WSMsgType.ERROR,aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.CLOSE):break
                            continue
                        payload=json.loads(msg.data);event=payload.get('data',payload);k=event.get('k',{})
                        symbol,timeframe=event.get('s'),k.get('i');key=(symbol,timeframe)
                        event_time=int(event.get('E',0))
                        if event.get('e')!='kline' or key not in machines or event.get('st') not in (None,1,'1'):continue
                        if event_time<=last_events.get(key,0) or not 0<=self.market.now()-event_time<=5000:continue
                        last_events[key]=event_time;machine=machines[key]
                        candle=Candle(symbol=symbol,timeframe=timeframe,open_time=int(k['t']),close_time=int(k['T']),
                            open=float(k['o']),high=float(k['h']),low=float(k['l']),close=float(k['c']),volume=float(k['v']),
                            quote_volume=float(k['q']),taker_buy_quote=float(k['Q']),trades=int(k['n']),closed=k.get('x') is True)
                        if not self.market._valid_candle(candle) or candle.close_time!=shift(candle.open_time,timeframe,1)-1: raise ValueError('实时K线字段无效')
                        if candle.open_time<=machine.bars[-1].open_time: continue
                        if candle.open_time != machine.bars[-1].close_time+1:
                            self.states[key]=dict(state='warming',note='连接跨过收盘边界，正在补齐缺失K线')
                            missing=await self.market.fetch_klines(dict(symbol=symbol,interval=timeframe,startTime=machine.bars[-1].close_time+1,endTime=candle.open_time-1,limit=499))
                            for row in missing:
                                closed=rest_candle(symbol,timeframe,row,self.market.now())
                                if not closed.closed or closed.close_time>=candle.open_time or not self.market._valid_candle(closed): raise ValueError('补齐K线无效')
                                machine.advance(closed)
                        # Every preview rolls back to the last closed state, as Pine does.
                        signals,levels,zones=machine.preview_full(candle)
                        region_signals,zone_states[key]=zone_signals(zones,candle,zone_states.get(key,{}))
                        self.publish(candle,event_time,signals+region_signals,list(machine.bars),zones)
                        assessment=context(machine.bars,candle.close,'long' if machine.groups['swing']['bias']==1 else 'short' if machine.groups['swing']['bias']==-1 else None)
                        if candle.closed: machine.advance(candle)
                        self.states[key]=dict(state='live',note='盘中实时监测',last_event=event_time,history_bars=machine.count,levels=levels,price=candle.close,price_time=event_time,assessment=assessment,zones=zones)
            except asyncio.CancelledError:raise
            except Exception as error:
                for key in group:
                    self.states[key]=dict(state='waiting',note=(str(error)[:180] or '行情暂未更新，等待重新连接'),error_type=type(error).__name__)
            await asyncio.sleep(5)
