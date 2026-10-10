"""Exploratory support/resistance reversal hypothesis, distinct from SMC breaks."""
import json,sys,csv,statistics,time
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.engine import Candle
from monitor.smc_structure import Structure
from monitor.smc_context import context
OUT=ROOT/'research/smc-context-2026-10-08';now=int(time.time()*1000);trades=[]
for c in json.loads((OUT/'contracts.json').read_text()):
 for tf in ('1h','4h','1d'):
  path=OUT/f"{c['symbol']}-{tf}.json";raw=json.loads(path.read_text());captured=json.loads((OUT/'snapshot-times.json').read_text()).get(path.name,int(path.stat().st_mtime*1000)) if (OUT/'snapshot-times.json').exists() else int(path.stat().st_mtime*1000)
  bars=[Candle(c['symbol'],tf,int(r[0]),int(r[6]),float(r[1]),float(r[2]),float(r[3]),float(r[4]),quote_volume=float(r[7])) for r in raw if int(r[6])<min(now,captured)]
  m=Structure();next_allowed=0
  for i,b in enumerate(bars):
   if m.bars and b.open_time!=m.bars[-1].close_time+1:m=Structure()
   prior=list(m.bars);levels=m.levels()['swing'];f=context(prior,b.close);m.advance(b)
   if not f['ready'] or m.count<52 or i<next_allowed or i+3>=len(bars) or f['atr']<=0:continue
   side=None
   if levels['support'] is not None and not levels['support_broken'] and 0<=b.close-levels['support']<=.25*f['atr']:side='long'
   if levels['resistance'] is not None and not levels['resistance_broken'] and 0<=levels['resistance']-b.close<=.25*f['atr']:
    if side:continue
    side='short'
   if not side:continue
   future=bars[i:i+4]
   if any(y.open_time!=x.close_time+1 for x,y in zip(future,future[1:])):continue
   sign=1 if side=='long' else -1;gross=sign*(bars[i+3].close/bars[i+1].open-1)*10000
   trades.append(dict(symbol=b.symbol,tf=tf,time=b.close_time,exit_time=bars[i+3].close_time,direction=side,net_bps=gross-20,counter_streak=f['streak']>=3 and f['streak_side']==('阴' if sign==1 else '阳')));next_allowed=i+4
summary=[]
for tf in ('1h','4h','1d'):
 dates=sorted({t['time'] for t in trades if t['tf']==tf})
 if not dates:continue
 cut=dates[int(len(dates)*.7)]
 for t in trades:
  if t['tf']==tf:t['split']='test' if t['time']>=cut else 'train' if t['exit_time']<cut else 'purged'
 for split in ('train','test'):
  for rule in ('baseline','avoid_counter_streak3','counter_streak3_only'):
   sample=[t for t in trades if t['tf']==tf and t['split']==split and (rule=='baseline' or (not t['counter_streak'] if rule=='avoid_counter_streak3' else t['counter_streak']))]
   if sample:summary.append(dict(tf=tf,split=split,rule=rule,n=len(sample),symbols=len({t['symbol'] for t in sample}),days=len({(t['time']+28800000)//86400000 for t in sample}),mean_net_bps=statistics.mean(t['net_bps'] for t in sample),win_pct=100*sum(t['net_bps']>0 for t in sample)/len(sample)))
(OUT/'reversal-results.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
with (OUT/'reversal-trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=list(trades[0]));w.writeheader();w.writerows(trades)
print(json.dumps(summary,indent=2))
