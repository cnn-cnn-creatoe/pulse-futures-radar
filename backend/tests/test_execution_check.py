import time
import unittest
from unittest.mock import patch
from monitor.collector import MonitorService
from monitor.store import Store

class ExecutionCheckTests(unittest.TestCase):
    def test_close_snapshot_excludes_later_quotes_and_preserves_rank_bounds(self):
        from collections import deque
        store=Store(':memory:');self.addCleanup(store.close);service=MonitorService(store);at=service.now()-1000
        service._metadata={str(i):{'quoteAsset':'USDT'} for i in range(10)}
        service._ticker_history={str(i):deque([(at-100,i*100),(at+500,99999-i)]) for i in range(8)}
        service._book_history['7']=deque([(at-100,2),(at+500,30)])
        evidence=service._base_enrichment('7',at)
        self.assertEqual(evidence['spread_bps'],2)
        self.assertEqual(evidence['quote_time'],at-100)
        self.assertEqual(evidence['liquidity_bounds'],[7/9,1])
        self.assertIsNone(evidence['liquidity_percentile'])
        store.save_market_evidence('7',at,evidence)
        store.save_market_evidence('7',at,{**evidence,'spread_bps':99})
        self.assertEqual(store.get_market_evidence('7',at)['spread_bps'],2)
        self.assertEqual(store.get_market_evidence('7',at+1),{})
        with self.assertRaises(ValueError):store.save_market_evidence('7',at+1,evidence)

    def test_incomplete_rank_is_verified_only_when_all_possible_ranks_agree(self):
        store=Store(':memory:');self.addCleanup(store.close);service=MonitorService(store);now=service.now()
        service._metadata={str(i):{'quoteAsset':'USDT'} for i in range(10)}
        service._ticker={str(i):{'time':now,'quote_volume':i*100} for i in range(8)}
        for key in ('0','3','7'):service._book_cache[key]=(time.monotonic(),1,now)
        with patch.object(service,'_base_enrichment',return_value={'liquidity_percentile':None}):
            high=service.execution_snapshot('7');self.assertEqual(high['status'],'通过');self.assertIsNone(high['liquidity_percentile'])
            self.assertEqual(service.execution_snapshot('0')['status'],'不通过')
            self.assertEqual(service.execution_snapshot('3')['status'],'待验证')
    def test_fresh_failed_missing_and_stale_remain_distinct(self):
        store=Store(':memory:');self.addCleanup(store.close)
        service=MonitorService(store);now=service.now()
        service._book_cache['BTCUSDT']=(time.monotonic(),2,now)
        with patch.object(service,'_base_enrichment',return_value={'liquidity_percentile':.9}):
            r=service.execution_snapshot('BTCUSDT');self.assertEqual(r['status'],'通过')
            self.assertEqual(r['quote_time'],now)
            service._book_cache['BTCUSDT']=(time.monotonic()-1000,2,now)
            r=service.execution_snapshot('BTCUSDT');self.assertEqual(r['status'],'待验证');self.assertIsNone(r['spread_bps'])
        service._book_cache['BTCUSDT']=(time.monotonic(),9,now)
        with patch.object(service,'_base_enrichment',return_value={'liquidity_percentile':.3}):
            r=service.execution_snapshot('BTCUSDT');self.assertEqual(r['status'],'不通过');self.assertEqual(len(r['reasons']),2)
