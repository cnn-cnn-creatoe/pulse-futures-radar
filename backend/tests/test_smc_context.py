import unittest
from monitor.engine import Candle
from monitor.smc_context import context

def bars():
 return [Candle('AAPLUSDT','1h',i*3600000,(i+1)*3600000-1,100,102,98,100,quote_volume=1000) for i in range(30)]

class ContextTests(unittest.TestCase):
 def test_falling_streak_warns_against_support_catch(self):
  b=bars()
  for c in b[-3:]:c.close=99
  r=context(b,100,'long',99)
  self.assertEqual(r['streak'],3)
  self.assertTrue(any('不宜仅凭支撑抢多' in w for w in r['warnings']))
  self.assertEqual(r['direction'],'偏多观察')
 def test_volume_uses_completed_history_not_current_price(self):
  b=bars();b[-1].quote_volume=400
  a=context(b,101,'long',100);c=context(b,120,'long',100)
  self.assertEqual(a['volume_ratio'],.4)
  self.assertEqual(a['volume_ratio'],c['volume_ratio'])
  self.assertTrue(any('追涨杀跌' in w for w in c['warnings']))
  self.assertFalse(any('追涨杀跌' in w for w in a['warnings']))
 def test_insufficient_gap_and_zero_volume(self):
  self.assertFalse(context(bars()[:10])['ready'])
  b=bars();b[-1].open_time+=1
  self.assertFalse(context(b)['ready'])
  b=bars()
  for c in b:c.quote_volume=0;c.high=c.low=c.close=c.open=100
  r=context(b)
  self.assertIsNone(r['volume_ratio']);self.assertIsNone(r['range_ratio'])
  self.assertEqual(r['warnings'],[])

 def test_trigger_snapshot_survives_restart_and_keeps_signal(self):
  import json
  from tempfile import TemporaryDirectory
  from pathlib import Path
  from types import SimpleNamespace
  from unittest.mock import Mock
  from monitor.smc_live import SmcLive
  b=bars()
  for c in b[-3:]:c.close=99
  with TemporaryDirectory() as folder:
   path=Path(folder)/'smc.db';tg=SimpleNamespace(enqueue=Mock(return_value=True));market=SimpleNamespace(now=lambda:1000)
   live=SmcLive(market,tg,path);live.desired=lambda:{('AAPLUSDT','1h')}
   signal=dict(structure='swing',tag='BOS',direction='long',level=101)
   live.publish(Candle('AAPLUSDT','1h',108000000,111599999,100,104,99,103,closed=False),1000,[signal],b)
   self.assertEqual(tg.enqueue.call_count,1)
   live.db.close();reopened=SmcLive(market,tg,path)
   saved=json.loads(reopened.db.execute('SELECT payload FROM triggers').fetchone()[0])
   self.assertEqual(saved['assessment']['streak'],3)
   self.assertTrue(saved['assessment']['warnings'])
   self.assertEqual(saved['assessment']['asof'],b[-1].close_time)
   reopened.db.close()
