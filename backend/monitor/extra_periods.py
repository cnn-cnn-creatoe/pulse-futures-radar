"""Demand-driven extra market periods; one shared-budget worker, no notifications."""
import asyncio
import time
import threading
from .periods import NATIVE, floor_bar, shift
from .engine import analyze, EngineConfig
from .daily_journal import local_time, day_start
from .workbench_data import HistoricalPreparation

class ExtraPeriods:
    def __init__(self,store,research,collector):
        self.store,self.research,self.collector=store,research,collector
        self._cancel = threading.Event()
        self.selected=[];self.task=None;self.expires=0;self.cursors={};self.state={"message":"", "periods":[]}
    def select(self,periods):
        periods=sorted(set(periods)-{"5m","15m"})
        if any(tf not in NATIVE for tf in periods): raise ValueError("不支持的周期")
        if "1s" in periods: raise ValueError("当前合约接口暂不接受 1 秒。请选择 1 分钟及以上周期。")
        changed=periods!=self.selected
        self.expires=time.monotonic()+90
        if changed:
            self.selected=periods
            self._cancel.set()
            if self.task:self.task.cancel()
            self._cancel = threading.Event()
            self.state={"message":"等待按需补齐行情" if periods else "", "periods":periods}
            self.task=asyncio.create_task(self.run(self._cancel)) if periods else None
        elif periods and (not self.task or self.task.done()) and not self.state.get("error"):
            self._cancel = threading.Event()
            self.task=asyncio.create_task(self.run(self._cancel))
        return self.state
    async def close(self):
        self._cancel.set()
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task,return_exceptions=True)
    def scan(self,symbol,tf,begin,now,metadata,cancel=None):
        candles=self.store.get_candles(symbol,tf,start=max(0,shift(floor_bar(begin,tf),tf,-2020)),end=now,limit=200000)
        key=(symbol,tf);cursor=max(begin-1,self.cursors.get(key,0))
        for i,c in enumerate(candles):
            if cancel is not None and cancel.is_set():return
            if c.close_time<=cursor or not c.closed or c.close_time>=now:continue
            result=analyze(candles[max(0,i-2020):i+1],self.collector.config,metadata)
            signal=result.get("signal")
            if signal:
                signal.update(source="live",data_source="Binance 原生周期补数",quote_asset="USDT",market_type="UM",qualified=False,quality_schema_version=2,telegram_sent=False,delayed=True,rejection_reasons=["补数异常记录，仅供研究"],candles=[v.to_dict() for v in candles[max(0,i-79):i+1]])
                self.store.save_alert(signal)
            self.cursors[key]=c.close_time
    async def run(self,cancel=None):
        cancel = cancel or self._cancel
        async def fetch(*args, **kwargs):
            if cancel.is_set() or time.monotonic() >= self.expires:
                raise asyncio.CancelledError()
            return await self.collector.fetch_research_page(*args, **kwargs)
        prep=HistoricalPreparation(self.store,self.research,fetch)
        try:
            while self.selected and time.monotonic()<self.expires:
                metadata=await asyncio.to_thread(self.store.list_universe,False)
                symbols=[m for m in metadata if m.get("snapshot_active") and m.get("quoteAsset")=="USDT" and m.get("status")=="TRADING"]
                for tf in list(self.selected):
                    for i,m in enumerate(symbols):
                        if time.monotonic()>=self.expires:return
                        now=self.collector._confirmed_history_now()
                        begin=day_start(local_time(now)[:10])
                        job={"request":{"scope":"single","symbols":[m["symbol"]],"timeframes":[tf],"holding_bars":1},"as_of":now,"logs":[]}
                        self.state.update(message=f"{tf} · {m['symbol']} · {i+1}/{len(symbols)} 按需补齐中",current=m["symbol"])
                        await prep.run(job,begin,now)
                        await asyncio.to_thread(self.scan,m["symbol"],tf,begin,now,m,cancel)
                        await asyncio.sleep(0.05)
                self.state["message"]="所选周期已更新 · 每分钟检查"
                await asyncio.sleep(60)
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.state.update(message="周期补数已暂停："+str(exc)[:220],error=True)
        finally:
            cancel.set()
