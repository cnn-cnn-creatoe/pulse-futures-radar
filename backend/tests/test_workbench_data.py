"""Backtest must not evaluate incomplete history, and may end one hour ago."""
import asyncio
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from monitor.engine import Candle
from monitor.store import Store
from monitor.research import Research
from monitor.custom_indicators import CustomIndicatorRegistry
from monitor.policy import Settings
from monitor.workbench import Workbench, WorkbenchRequest, timestamp
from monitor.workbench_data import HistoricalPreparation, missing_ranges

I = 300000
class PreparationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.store=Store(root/'data.db')
        self.research=Research(root/'history')
        self.service=Workbench(self.store,self.research,CustomIndicatorRegistry(root/'custom.json'),root/'runs')
        self.options=WorkbenchRequest(start='2026-09-01T00:00',end='2026-09-01T01:00').model_dump()
        self.begin=timestamp(self.options['start']); self.end=timestamp(self.options['end'])
        self.job=dict(id='a'*32,state='running',request=self.options,as_of=self.end+3600000,logs=[],created_at=1)
        self.calls=[]
    def tearDown(self):
        self.store.close();self.tmp.cleanup()
    def candle(self,t):
        return Candle('BTCUSDT','5m',t,t+I-1,100,101,99,100,10000,5000,100,100)
    async def fetch(self,s,tf,a,b):
        self.calls.append((a,b))
        return [self.candle(t) for t in range(a,b+1,I)]
    async def test_download_warmup_range_and_observation_then_compute(self):
        self.service.fetch_page=self.fetch
        self.service.work=Mock()
        await self.service.execute(self.job,Settings(),[])
        self.service.work.assert_called_once()
        self.assertEqual(self.job['preparation']['stage'],'ready')
        self.assertEqual(self.calls[0][0],self.begin-2020*I)
        self.assertEqual(self.calls[-1][1],self.end+4*I)
        self.assertTrue(all((b-a)//I+1<=499 for a,b in self.calls))
        self.assertTrue(all(b+I<=self.job['as_of'] for a,b in self.calls))
    async def test_incomplete_exchange_page_preserved_but_no_compute(self):
        async def partial(s,tf,a,b): return [self.candle(a)]
        self.service.fetch_page=partial; self.service.work=Mock()
        await self.service.execute(self.job,Settings(),[])
        self.service.work.assert_not_called()
        self.assertEqual(self.job['state'],'failed')
        self.assertIn('未开始回测',self.job['message'])
        self.assertEqual(len(self.store.get_candles('BTCUSDT','5m',limit=3000)),1)
        self.calls.clear();self.service.fetch_page=self.fetch
        self.job.update(state='running',logs=[])
        await self.service.execute(self.job,Settings(),[])
        self.service.work.assert_called_once()
        self.assertEqual(self.calls[0][0],self.begin-2019*I)
    async def test_complete_local_history_never_downloads(self):
        first=self.begin-2020*I;last=self.end+4*I
        self.store.save_candles([self.candle(t) for t in range(first,last+1,I)])
        prep=HistoricalPreparation(self.store,self.research,self.fetch)
        await prep.run(self.job,self.begin,self.end)
        self.assertEqual(self.calls,[])
    async def test_cancel_keeps_downloaded_page_without_compute(self):
        async def cancel(s,tf,a,b):
            rows=await self.fetch(s,tf,a,b);self.job['cancel_requested']=True;return rows
        self.service.fetch_page=cancel;self.service.work=Mock()
        await self.service.execute(self.job,Settings(),[])
        self.service.work.assert_not_called()
        self.assertEqual(self.job['state'],'cancelled')
        self.assertEqual(len(self.store.get_candles('BTCUSDT','5m',limit=3000)),499)
    async def test_current_day_does_not_require_future_candles(self):
        self.job['as_of']=self.end-2*I+12345
        prep=HistoricalPreparation(self.store,self.research,self.fetch)
        await prep.run(self.job,self.begin,min(self.end,self.job['as_of']))
        self.assertEqual(self.calls[-1][1],self.end-3*I)
    async def test_network_failure_never_computes(self):
        async def fail(*args):raise ConnectionError('download failed')
        self.service.fetch_page=fail;self.service.work=Mock()
        await self.service.execute(self.job,Settings(),[])
        self.service.work.assert_not_called();self.assertEqual(self.job['state'],'failed')
    def test_missing_ranges_include_edges_and_internal_gaps(self):
        self.assertEqual(missing_ranges([self.candle(2*I),self.candle(4*I)],0,5*I,I),[(0,I),(3*I,3*I),(5*I,5*I)])

if __name__=='__main__':unittest.main()
