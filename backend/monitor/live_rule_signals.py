"""Read-only live rule notifications using the workbench's reviewed rules.

Only newly received, closed candles are eligible. No order execution, historical
notification replay, or arbitrary Pine evaluation is introduced here.
"""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from .workbench import CATALOG, signal_stream, prepare_signal_series
from .community_indicators import CATALOG as COMMUNITY
from .quant_research import CATALOG as QUANT, STRATEGY_INDICATORS, signal_rows
from .daily_journal import local_time
from .signal_scores import historical_scores
from .selected_period_rules import TIMEFRAMES,closed_history
from .periods import floor_bar,shift


def rule_catalog(kind):
    from .smc_structure import CATALOG as SMC
    values=QUANT if kind=='quant' else CATALOG+COMMUNITY+[SMC] if kind=='indicator' else []
    return list({r['id']:dict(id=r['id'],name=r['name'],description=r.get('description',''))
                 for r in values if kind=='quant' or r.get('kind')=='event'}.values())


def current_events(store,candles,settings,subscriptions):
    last=candles[-1];at=last.close_time
    day=(at+8*3600000)//86400000*86400000-8*3600000
    # Match the workbench's declared 2,020-bar prewarm plus today.
    first=next((i for i,c in enumerate(candles) if c.close_time>=day),len(candles)-1)
    candles=candles[max(0,first-2020):]
    selected={(kind,rule) for kind,rule in subscriptions if rule != "smc_equity_structure"}
    ids=sorted({i for kind,rule in selected for i in (STRATEGY_INDICATORS[rule] if kind=='quant' else [rule])})
    if not ids:return []
    options=dict(indicators=ids,combination='any',pattern='none',engine='indicator',
                 start=local_time(day),end=local_time(at+1))
    prepared=prepare_signal_series(candles,settings,ids)
    result=[]
    for kind,rule in sorted(selected):
        required=STRATEGY_INDICATORS[rule] if kind=='quant' else [rule]
        matched=list(signal_stream(candles,settings,[],{**options,'indicators':required,'combination':'all','engine':kind,'strategy':rule if kind=='quant' else None,
            'start':local_time(candles[0].open_time) if kind=='quant' else options['start']},prepared=prepared))
        rows=signal_rows(matched,rule if kind=='quant' else None)
        name=next(r['name'] for r in rule_catalog(kind) if r['id']==rule)
        for row in rows:
            if row['signal_time']!=at:continue
            scores=historical_scores(store,last.symbol,last.timeframe,at,row['direction'],settings.model_dump_json())
            result.append(dict(id=f"live:{kind}:{rule}:{row['id']}",source='live',
                symbol=last.symbol,timeframe=last.timeframe,close_time=at,
                entry_direction=row['direction'],pattern=row['pattern'],rule_kind=kind,
                rule_id=rule,rule_name=name,signal_kind=row['signal_kind'],
                signal_price=row['signal_price'],action_basis=row['action_basis'],
                **scores))
    return result


class LiveRuleSignals:
    def __init__(self,store,telegram,settings,now=None):
        self.store=store;self.telegram=telegram;self.settings=settings
        self.queue=asyncio.Queue(maxsize=512);self.task=None
        self.dropped=0;self.processed=0;self.last_error=None
        self.now=now or (lambda:time.time()*1000)
        self.executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix="live-rules")
        self.drop_reasons={"queue_full":0,"expired":0,"incomplete_history":0}
        self.errors=0;self.active=False;self.last_processed_at=None;self.last_duration_ms=None

    def drop(self,reason):
        self.dropped+=1;self.drop_reasons[reason]+=1

    def status(self):
        return dict(queue_depth=self.queue.qsize(),queue_capacity=self.queue.maxsize,
                    active=self.active,processed=self.processed,dropped=self.dropped,
                    drop_reasons=dict(self.drop_reasons),errors=self.errors,last_error=self.last_error,
                    last_processed_at=self.last_processed_at,last_duration_ms=self.last_duration_ms,
                    scope="个人订阅规则推送（含WT），不代表全部行情分析")

    async def compute(self,fn,*args):
        # Keep live rules out of the shared history/research executor backlog.
        return await asyncio.get_running_loop().run_in_executor(self.executor,partial(fn,*args))

    def subscriptions(self,timeframe,symbol=None):
        if not self.telegram._accept_events:return set()
        from .delivery_profiles import profiles
        return {(p['kind'],p['rule_id']) for s in self.telegram.state.subscribers()
                if s['preferences']['signals_enabled'] for p in profiles(s['preferences']) if p['rule_id'] != 'smc_equity_structure' and timeframe in p['timeframes']
                and (symbol is None or not p['symbols'] or symbol in p['symbols'])}

    def enqueue(self,candles):
        if not candles or not self.subscriptions(candles[-1].timeframe,candles[-1].symbol):return
        if self.task is None or self.task.done():self.task=asyncio.create_task(self.run())
        try:self.queue.put_nowait((candles,self.settings().model_copy(deep=True)))
        except asyncio.QueueFull:self.drop("queue_full")

    def enqueue_minute(self,candle):
        if not candle.closed or not 0<=self.now()-candle.close_time<90000:return
        periods=[tf for tf in TIMEFRAMES if self.subscriptions(tf,candle.symbol) and shift(floor_bar(candle.open_time,tf),tf,1)-1==candle.close_time]
        if not periods:return
        if self.task is None or self.task.done():self.task=asyncio.create_task(self.run())
        try:self.queue.put_nowait(((candle,periods),self.settings().model_copy(deep=True)))
        except asyncio.QueueFull:self.drop("queue_full")

    async def run(self):
        while True:
            payload,settings=await self.queue.get()
            started=time.monotonic();self.active=True
            try:
                batches=[payload] if isinstance(payload,list) else []
                if not batches:
                    minute,periods=payload
                    if not 0<=self.now()-minute.close_time<90000:
                        self.drop("expired");continue
                    for tf in periods:
                        candles=await self.compute(closed_history,self.store,minute,tf)
                        if candles:batches.append(candles)
                        else:self.drop("incomplete_history");self.last_error=f'{minute.symbol} {tf} 当前周期数据不完整，未生成信号'
                for candles in batches:
                    if not 0<=self.now()-candles[-1].close_time<90000:
                        self.drop("expired");continue
                    events=await self.compute(current_events,self.store,candles,settings,self.subscriptions(candles[-1].timeframe,candles[-1].symbol))
                    if self.now()-candles[-1].close_time>=90000:
                        self.drop("expired");continue
                    for event in events:self.telegram.enqueue('signal',event)
                    self.processed+=1;self.last_processed_at=int(self.now())
            except asyncio.CancelledError:raise
            except Exception as exc:self.errors+=1;self.last_error=type(exc).__name__+': '+str(exc)[:160]
            finally:
                self.active=False;self.last_duration_ms=round((time.monotonic()-started)*1000,1)
                self.queue.task_done()

    async def close(self):
        if self.task:
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
        await asyncio.to_thread(self.executor.shutdown,wait=True,cancel_futures=True)
