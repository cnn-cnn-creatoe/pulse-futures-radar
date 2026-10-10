import unittest
from monitor.intrabar import evaluate
from monitor.engine import Candle
from monitor.policy import Settings

class IntrabarTests(unittest.TestCase):
    def test_aggregation_never_mutates_live_or_saves_fake_close(self):
        minute=Candle('BTCUSDT','1m',0,59999,100,105,99,102,volume=2)
        live=Candle('BTCUSDT','1m',60000,119999,102,106,101,104,volume=3,closed=False).to_dict()
        live['event_time']=65000
        class Store:
            def get_candles(self,symbol,timeframe,**kw):return [minute] if timeframe=='1m' else []
        result=evaluate(Store(),live,Settings(),'BTCUSDT','5m','indicator','wt_extreme_cross',66000)
        self.assertFalse(result['available'])
        self.assertIn('预热',result['note'])
        self.assertFalse(live['closed'])
        self.assertFalse(evaluate(Store(),live,Settings(),'BTCUSDT','5m','indicator','wt_extreme_cross',90000)['available'])
        warming=evaluate(Store(),live,Settings(),'BTCUSDT','5m','indicator','wt_extreme_cross',66000)
        self.assertFalse(warming['available'])
        self.assertIn('预热',warming['note'])
    def test_missing_minutes_fail_closed(self):
        class Store:
            def get_candles(self,*args,**kw):return []
        live=Candle('BTCUSDT','1m',60000,119999,100,101,99,100,closed=False).to_dict();live['event_time']=65000
        self.assertFalse(evaluate(Store(),live,Settings(),'BTCUSDT','5m','indicator','wt_extreme_cross',66000)['available'])
