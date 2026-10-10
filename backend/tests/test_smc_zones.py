import math,unittest
from copy import deepcopy
from monitor.engine import Candle
from monitor.smc_structure import Structure
from monitor.smc_zones import zone_signals

def bar(i,o=100,h=104,l=98,c=102,closed=True):return Candle('XOMUSDT','1h',i*3600000,(i+1)*3600000-1,o,h,l,c,closed=closed)
def block(side='supply',ident='a'):
 return dict(id=ident,structure='internal',side=side,lower=100,upper=110,origin_at=0,formed_at=0)

class ZoneTests(unittest.TestCase):
 def test_touch_reject_bounce_invalidate_and_rollback(self):
  m=Structure();m.advance(bar(0));m.blocks['internal']=[block()]
  _,_,z=m.preview_full(bar(1,o=99,h=105,l=98,c=104,closed=False))
  self.assertEqual(z['items'][0]['phase'],'inside')
  _,_,z=m.preview_full(bar(1,o=99,h=105,l=97,c=98,closed=False))
  self.assertEqual(z['items'][0]['phase'],'rejection')
  _,_,z=m.preview_full(bar(1,o=99,h=111,l=98,c=99,closed=False))
  self.assertEqual(z['items'][0]['phase'],'invalidated')
  self.assertEqual(len(m.blocks['internal']),1)
  m.advance(bar(1,o=99,h=111,l=98,c=99));self.assertFalse(m.blocks['internal'])
  m=Structure();m.advance(bar(0));m.blocks['internal']=[block('demand')]
  _,_,z=m.preview_full(bar(1,o=111,h=113,l=105,c=112))
  self.assertEqual(z['items'][0]['phase'],'bounce')
 def test_equality_is_touch_not_mitigation_and_all_invalid_removed(self):
  m=Structure();m.advance(bar(0));m.blocks['internal']=[block(),block(ident='b')]
  _,_,z=m.preview_full(bar(1,o=100,h=110,l=99,c=110))
  self.assertTrue(all(x['phase']=='inside' for x in z['items']))
  m.advance(bar(1,o=100,h=111,l=99,c=110));self.assertEqual(len(m.invalidated),2)
  self.assertFalse(m.blocks['internal'])
 def test_live_reaction_requires_observed_touch_and_phase_change(self):
  c=bar(1,closed=False);z=dict(block(),phase='rejection',confirmed=False)
  signals,states=zone_signals({'items':[z]},c,{})
  self.assertFalse(signals)
  z['phase']='inside';signals,states=zone_signals({'items':[z]},c,states)
  self.assertEqual(signals[0]['event_type'],'zone_inside')
  signals,states=zone_signals({'items':[z]},c,states);self.assertFalse(signals)
  z['phase']='rejection';signals,states=zone_signals({'items':[z]},c,states)
  self.assertEqual(signals[0]['direction'],'short')
  z['phase']='invalidated';signals,states=zone_signals({'items':[z]},c,states)
  self.assertEqual(signals[0]['event_type'],'zone_invalidated')
 def test_bounded_candidates_match_full_pine_slices(self):
  m=Structure();bars=[];parsed=[];trs=[];atr=None;legs={'internal':0,'swing':0};pivots={};expected={'internal':[],'swing':[]};created=0
  for i in range(850):
   center=100+12*math.sin(i/29)+3*math.sin(i/3)
   c=bar(i,center-1,center+2+(20 if i%137==0 else 0),center-2,center+math.sin(i))
   tr=max(c.high-c.low,abs(c.high-bars[-1].close),abs(c.low-bars[-1].close)) if bars else c.high-c.low
   trs.append(tr)
   if atr is not None:atr=(199*atr+tr)/200
   elif len(trs)==200:atr=sum(trs)/200
   volatile=atr is not None and c.high-c.low>=2*atr
   parsed.append(dict(high=c.low if volatile else c.high,low=c.high if volatile else c.low,time=c.open_time))
   bars.append(c)
   for name,n in [('internal',5),('swing',50)]:
    if i<n:continue
    pivot=bars[i-n];window=bars[i-n+1:i+1]
    leg=0 if pivot.high>max(b.high for b in window) else 1 if pivot.low<min(b.low for b in window) else legs[name]
    if leg!=legs[name]:pivots[(name,'long' if leg==0 else 'short')]=i-n
    legs[name]=leg
   signals=m.advance(c)
   for signal in signals:
    name,d=signal['structure'],signal['direction'];start=pivots[(name,d)]
    candidate=min(parsed[start:i],key=lambda x:x['low']) if d=='long' else max(parsed[start:i],key=lambda x:x['high'])
    if candidate['high']<candidate['low']:continue
    expected[name].insert(0,dict(lower=candidate['low'],upper=candidate['high'],origin_at=candidate['time'],side='demand' if d=='long' else 'supply'));expected[name]=expected[name][:100];created+=1
   for name in legs:
    expected[name]=[b for b in expected[name] if not (c.high>b['upper'] if b['side']=='supply' else c.low<b['lower'])]
    actual=[{k:b[k] for k in ('lower','upper','origin_at','side')} for b in m.blocks[name]]
    self.assertEqual(actual,expected[name],(i,name))
  self.assertGreater(created,15)
  self.assertAlmostEqual(m.atr200,atr)

 def test_zone_records_dedup_per_zone_and_structure_conflict(self):
  import json
  from tempfile import TemporaryDirectory
  from pathlib import Path
  from types import SimpleNamespace
  from unittest.mock import Mock
  from monitor.smc_live import SmcLive
  from monitor.telegram import format_signal
  with TemporaryDirectory() as folder:
   market=SimpleNamespace(now=lambda:1000);tg=SimpleNamespace(enqueue=Mock(return_value=True))
   live=SmcLive(market,tg,Path(folder)/'smc.db');live.desired=lambda:{('XOMUSDT','1h')}
   c=bar(1,o=99,h=105,l=98,c=104,closed=False)
   zones={'items':[dict(block(),phase='inside',confirmed=False)],'atr_ready':True,'history_bars':220}
   struct=dict(structure='internal',tag='CHoCH',direction='long',level=103)
   live.publish(c,1000,[struct],zone_context=zones)
   event=json.loads(live.db.execute('SELECT payload FROM triggers').fetchone()[0])
   self.assertIn('供给压力区',event['zone_conflict'])
   self.assertIn('区域冲突',format_signal(event,{}))
   zones['items'].append(dict(block(ident='b'),phase='inside',confirmed=False))
   signals,_=zone_signals(zones,c,{})
   live.publish(c,1000,signals,zone_context=zones);live.publish(c,1000,signals,zone_context=zones)
   self.assertEqual(live.db.execute('SELECT COUNT(*) FROM triggers').fetchone()[0],3)
   touch=tg.enqueue.call_args.args[1]
   self.assertIn('触区观察',format_signal(touch,{}))
   self.assertNotIn('压力位（向上突破）',format_signal(touch,{}))
   live.db.close()
