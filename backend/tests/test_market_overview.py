import unittest
from monitor.store import Store
from monitor.engine import Candle
from monitor.market_overview import MarketOverview
from monitor.daily_journal import day_start

class MarketOverviewTests(unittest.TestCase):
 def setUp(self):
  self.store=Store(':memory:');self.start=day_start('2026-09-06')
  self.store.reconcile_universe([{'symbol':s,'quoteAsset':'USDT','status':'TRADING','onboardDate':self.start-86400000} for s in ['UPUSDT','DOWNUSDT','GAPUSDT']],self.start)
 def tearDown(self):self.store.close()
 def bar(self,s,t,close,q=100):return Candle(s,'5m',t,t+299999,10,max(10,close),min(10,close),close,q,q/2,2,10)
 def test_today_excludes_yesterday_and_incomplete_pairs(self):
  for s,close in [('UPUSDT',11),('DOWNUSDT',9)]:
   self.store.save_candles([self.bar(s,self.start-300000,close,99999),self.bar(s,self.start,close),self.bar(s,self.start+300000,close)])
  self.store.save_candles([self.bar('GAPUSDT',self.start+300000,12,999999)])
  data=MarketOverview(self.store).get(self.start+600000)
  self.assertEqual(data['covered'],2);self.assertEqual(data['partial'],1)
  self.assertEqual(data['turnover'],400)
  self.assertEqual(len(data['items']),3)
  partial=next(r for r in data['items'] if r['symbol']=='GAPUSDT')
  self.assertIsNone(partial['turnover']);self.assertIsNone(partial['change_pct'])
  self.assertEqual(data['positive'][0]['symbol'],'UPUSDT');self.assertEqual(data['negative'][0]['symbol'],'DOWNUSDT')
  self.assertAlmostEqual(data['positive'][0]['change_pct'],10)
 def test_midnight_resets_even_with_cache(self):
  service=MarketOverview(self.store)
  service.get(self.start+86400000-1000)
  data=service.get(self.start+86400000)
  self.assertEqual(data['date'],'2026-09-07');self.assertEqual(data['all'],[])
 def test_unclosed_bar_is_not_ranked(self):
  self.store.save_candles([self.bar('UPUSDT',self.start,11)])
  data=MarketOverview(self.store).get(self.start+100000)
  self.assertEqual(data['covered'],0);self.assertEqual(data['turnover'],0)

if __name__=='__main__':unittest.main()
