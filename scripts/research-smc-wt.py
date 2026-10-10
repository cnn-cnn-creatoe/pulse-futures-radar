"""Exploratory WT filters on frozen SMC trades; never modifies production strategies."""
import sys,json,csv,statistics,random
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.community_indicators import ema,sma
DATA=ROOT/'research/smc-context-2026-10-08';OUT=ROOT/'research/smc-wt-2026-10-08';OUT.mkdir(exist_ok=True)
trades=list(csv.DictReader((DATA/'trades.csv').open(encoding='utf-8-sig'))); grouped=defaultdict(list)
for t in trades:grouped[t['symbol'],t['tf']].append(t)
for (symbol,tf),rows in grouped.items():
 raw=json.loads((DATA/f'{symbol}-{tf}.json').read_text()); segments=[];segment=[]
 for r in raw:
  if segment and int(r[0])!=int(segment[-1][6])+1:segments.append(segment);segment=[]
  segment.append(r)
 if segment:segments.append(segment)
 features={}
 for bars in segments:
  ap=[(float(r[2])+float(r[3])+float(r[4]))/3 for r in bars];base=ema(ap,10);dev=ema([abs(a-b) for a,b in zip(ap,base)],10);w=ema([(a-b)/(.015*d) if d>0 else None for a,b,d in zip(ap,base,dev)],21);sig=sma(w,4)
  for i,r in enumerate(bars):
   # The signal candle itself is excluded, matching live pre-trigger context.
   j=i-1
   if j<70 or w[j] is None or sig[j] is None:continue
   crosses=[]
   for k in range(max(70,j-2),j+1):
    if any(x is None for x in (w[k],sig[k],w[k-1],sig[k-1])):continue
    if w[k]>sig[k] and w[k-1]<=sig[k-1] and w[k]<-53:crosses.append('long')
    if w[k]<sig[k] and w[k-1]>=sig[k-1] and w[k]>53:crosses.append('short')
   features[int(r[6])]=(w[j],sig[j],crosses)
 for t in rows:
  f=features.get(int(t['time']));t['wt_ready']=f is not None
  t['summary_pass']=not any(t[k]=='True' for k in ('streak_risk','low_volume','high_range','extended'))
  t['avoid_extreme']=bool(f and (f[0]<=53 if t['direction']=='long' else f[0]>=-53))
  t['momentum']=bool(f and (f[0]>f[1] if t['direction']=='long' else f[0]<f[1]))
  t['extreme_cross3']=bool(f and t['direction'] in f[2]);t['wt1']=f[0] if f else None
rules={'baseline':lambda t:True,'wt_ready_baseline':lambda t:t['wt_ready'],'current_risk_summary':lambda t:t['summary_pass'],'avoid_extreme':lambda t:t['avoid_extreme'],'momentum':lambda t:t['momentum'],'extreme_cross3':lambda t:t['extreme_cross3'],'summary_plus_momentum':lambda t:t['summary_pass'] and t['momentum'],'summary_plus_avoid_extreme':lambda t:t['summary_pass'] and t['avoid_extreme']}
summary=[]
for tf in ('1h','4h','1d'):
 for split in ('train','test'):
  for name,predicate in rules.items():
   sample=[t for t in trades if t['tf']==tf and t['split']==split and predicate(t)]
   vals=[float(t['net_bps']) for t in sample];days=defaultdict(list)
   for t in sample:days[(int(t['time'])+28800000)//86400000].append(float(t['net_bps']))
   blocks=[(sum(v),len(v)) for v in days.values()];boot=[];rng=random.Random(42)
   if blocks:
    for _ in range(1000):
     b=rng.choices(blocks,k=len(blocks));boot.append(sum(x[0] for x in b)/sum(x[1] for x in b))
    boot.sort()
   summary.append(dict(tf=tf,split=split,rule=name,n=len(vals),days=len(days),mean_bps=statistics.mean(vals) if vals else None,win_pct=100*sum(v>0 for v in vals)/len(vals) if vals else None,ci95_bps=[boot[24],boot[974]] if boot else None))
(OUT/'results.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
with (OUT/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
 writer=csv.DictWriter(f,fieldnames=list(trades[0]));writer.writeheader();writer.writerows(trades)
print(json.dumps([s for s in summary if s['split']=='test'],indent=2))
