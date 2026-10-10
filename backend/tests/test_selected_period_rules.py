import tempfile
import unittest
from pathlib import Path
from dataclasses import replace
from monitor.engine import Candle
from monitor.store import Store
from monitor.periods import shift
from monitor.selected_period_rules import closed_history

class SelectedPeriodTests(unittest.TestCase):
    def setUp(self):
        self.folder=tempfile.TemporaryDirectory()
        self.store=Store(Path(self.folder.name)/'test.db')
    def tearDown(self):
        self.store.close();self.folder.cleanup()
    def candle(self,start,tf='15m'):
        return Candle('BTCUSDT',tf,start,shift(start,tf)-1,100,110,90,105,1000,400,10,20)
    def test_complete_hour_aggregates_and_is_idempotent(self):
        parts=[self.candle(i*900000) for i in range(4)]
        self.store.save_candles(parts)
        minute=self.candle(3540000,'1m')
        actual=closed_history(self.store,minute,'1h')[-1]
        self.assertEqual((actual.open_time,actual.close_time,actual.quote_volume,actual.taker_buy_quote,actual.volume,actual.trades),(0,3599999,4000,1600,80,40))
        self.assertEqual(closed_history(self.store,minute,'1h'),[actual])
    def test_missing_and_corrupt_bars_do_not_create_hour(self):
        parts=[self.candle(i*900000) for i in range(4)]
        self.store.save_candles(parts[:3])
        self.assertEqual(closed_history(self.store,self.candle(3540000,'1m'),'1h'),[])
        self.store.save_candles([replace(parts[-1],high=99)])
        self.assertEqual(closed_history(self.store,self.candle(3540000,'1m'),'1h'),[])
        self.assertEqual(self.store.get_candles('BTCUSDT','1h'),[])
    def test_calendar_month_and_week_boundaries(self):
        for tf,start in [('1M',1769904000000),('1w',1769990400000)]:
            end=shift(start,tf)
            self.store.save_candles([self.candle(t,'4h') for t in range(start,end,14400000)])
            minute=self.candle(end-60000,'1m')
            result=closed_history(self.store,minute,tf)
            self.assertEqual((result[-1].open_time,result[-1].close_time),(start,end-1))
            self.assertEqual(closed_history(self.store,self.candle(end-120000,'1m'),tf),[])
    def test_minute_identity_and_closed_status_are_required(self):
        c=self.candle(0,'1m')
        self.assertEqual(closed_history(self.store,c,'1m'),[c])
        for invalid in [replace(c,closed=False),replace(c,open_time=1),replace(c,taker_buy_quote=2000),replace(c,close=float('nan'))]:
            self.assertEqual(closed_history(self.store,invalid,'1m'),[])
