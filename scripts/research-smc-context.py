"""Fixed-rule chronological SMC close-signal proxy study, next-open fills."""
import json,sys,csv,statistics,time,random
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.engine import Candle
from monitor.smc_structure import Structure
from monitor.smc_context import context
OUT=ROOT/'research/smc-context-2026-10-08'
contracts=json.loads((OUT/'contracts.json').read_text());now=int(time.time()*1000)
trades=[];coverage=[]
for contract in contracts:
 for tf in ('1h','4h','1d'):
  path=OUT/f"{contract['symbol']}-{tf}.json"
  if not path.exists():continue
  raw=json.loads(path.read_text());captured=json.loads((OUT/'snapshot-times.json').read_text()).get(path.name,int(path.stat().st_mtime*1000)) if (OUT/'snapshot-times.json').exists() else int(path.stat().st_mtime*1000);bars=[Candle(symbol=contract['symbol'],timeframe=tf,open_time=int(r[0]),close_time=int(r[6]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5]),quote_volume=float(r[7]),taker_buy_quote=float(r[10]),trades=int(r[8]),closed=True) for r in raw if int(r[6])<min(now,captured)]
  coverage.append(dict(symbol=contract['symbol'],tf=tf,bars=len(bars),start=bars[0].open_time if bars else None,end=bars[-1].close_time if bars else None))
  machine=Structure();next_allowed=0
  for i,c in enumerate(bars):
   if machine.bars and c.open_time!=machine.bars[-1].close_time+1:machine=Structure()
   prior=list(machine.bars);signals=machine.advance(c)
   if machine.count<52 or i<next_allowed or i+3>=len(bars) or not signals:continue
   future=bars[i:i+4]
   if any(b.open_time!=a.close_time+1 for a,b in zip(future,future[1:])):continue
   directions={s['direction'] for s in signals}
   if len(directions)!=1:continue
   signal=signals[-1];features=context(prior,c.close,signal['direction'],signal['level'])
   if not features['ready']:continue
   entry=bars[i+1].open;exitprice=bars[i+3].close
   if entry<=0:continue
   sign=1 if signal['direction']=='long' else -1
   gross=sign*(exitprice/entry-1)*10000
   row=dict(symbol=c.symbol,tf=tf,time=c.close_time,exit_time=bars[i+3].close_time,direction=signal['direction'],gross_bps=gross,net_bps=gross-20,
     streak_risk=features['streak']>=3 and ((sign==1 and features['streak_side']=='阴') or (sign==-1 and features['streak_side']=='阳')),
     low_volume=features['volume_ratio'] is not None and features['volume_ratio']<.8,
     high_volume=features['volume_ratio'] is not None and features['volume_ratio']>=1.2,
     high_range=features['range_ratio'] is not None and features['range_ratio']>2,
     extended=features['extension_atr'] is not None and features['extension_atr']>1)
   trades.append(row);next_allowed=i+4
cuts={}
for tf in ('1h','4h','1d'):
 dates=sorted({t['time'] for t in trades if t['tf']==tf});cuts[tf]=dates[int(len(dates)*.7)] if dates else 0
for t in trades:t['split']='test' if t['time']>=cuts[t['tf']] else 'train' if t['exit_time']<cuts[t['tf']] else 'purged'
filters={'baseline':lambda t:True,'avoid_counter_streak3':lambda t:not t['streak_risk'],'avoid_low_volume_0.8':lambda t:not t['low_volume'],'require_volume_1.2':lambda t:t['high_volume'],'avoid_range2':lambda t:not t['high_range'],'avoid_extension1':lambda t:not t['extended']}
summary=[]
for tf in ('1h','4h','1d'):
 for split in ('train','test'):
  group=[t for t in trades if t['tf']==tf and t['split']==split]
  for name,predicate in filters.items():
   sample=[t for t in group if predicate(t)]
   if not sample:continue
   # Daily clustered means expose the small effective sample size.
   days=defaultdict(list)
   for t in sample:days[(t['time']+28800000)//86400000].append(t['net_bps'])
   daily=[statistics.mean(v) for v in days.values()]
   blocks=[(sum(v),len(v)) for v in days.values()];rng=random.Random(42);boot=[]
   for _ in range(1000):
    selected=rng.choices(blocks,k=len(blocks));boot.append(sum(v[0] for v in selected)/sum(v[1] for v in selected))
   boot.sort()
   summary.append(dict(tf=tf,split=split,rule=name,n=len(sample),symbols=len({t['symbol'] for t in sample}),days=len(days),retained=len(sample)/len(group),win_pct=100*sum(t['net_bps']>0 for t in sample)/len(sample),mean_net_bps=statistics.mean(t['net_bps'] for t in sample),median_net_bps=statistics.median(t['net_bps'] for t in sample),mean_gross_bps=statistics.mean(t['gross_bps'] for t in sample),daily_mean_bps=statistics.mean(daily),cluster_ci95_bps=[boot[24],boot[974]]))
result=dict(method='Closed-bar proxy only. Prior completed bars for context; entry next open, exit third bar close; no overlapping trades per symbol/timeframe; 20 bps roundtrip friction, funding excluded. Fixed thresholds. Global timestamp 70/30 split per timeframe with boundary purge. Current listed universe survivorship bias.',cuts=cuts,coverage=coverage,summary=summary)
(OUT/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
with (OUT/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
 writer=csv.DictWriter(f,fieldnames=list(trades[0]) if trades else ['symbol']);writer.writeheader();writer.writerows(trades)
print(json.dumps({'files':len(coverage),'trades':len(trades),'summary':summary},ensure_ascii=False,indent=2))
