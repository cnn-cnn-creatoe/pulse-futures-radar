"""Causal close-bar study of known levels/blocks + WT. Production files read-only."""
import json,sys,csv,statistics,random,hashlib
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.smc_structure import Structure
from monitor.engine import Candle
from monitor.community_indicators import ema,sma
OLD=ROOT/'research/smc-context-2026-10-08';OUT=ROOT/'research/smc-zone-wt-2026-10-09'

def study(raw,symbol,tf,cutoff):
 bars=[Candle(symbol=symbol,timeframe=tf,open_time=int(r[0]),close_time=int(r[6]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=0,quote_volume=0,taker_buy_quote=0,trades=0,closed=True) for r in raw if int(r[6])<cutoff]
 machine=Structure();history=[];events=[];next_allowed={};prev_hits=set()
 for i,c in enumerate(bars):
  if history and c.open_time!=history[-1].close_time+1:machine=Structure();history=[];prev_hits=set();next_allowed={}
  prior_levels=machine.levels();prior_zones=machine.zone_snapshot()['items'] if history else [];ready=machine.atr200 is not None
  history.append(c);machine.advance(c)
  if len(history)<201:continue
  tr=[max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(history[-15:-1],history[-14:])];atr=statistics.mean(tr)
  if atr<=0:continue
  # All indicators use at most this closed signal candle. Fill only at next open.
  ap=[(b.high+b.low+b.close)/3 for b in history];base=ema(ap,10);dev=ema([abs(a-b) for a,b in zip(ap,base)],10);w=ema([(a-b)/(.015*d) if d>0 else None for a,b,d in zip(ap,base,dev)],21);wt=sma(w,4)
  if any(x is None for x in (w[-1],w[-2],wt[-1])):continue
  candidates=[]
  for group,lv in prior_levels.items():
   for side,key in [('long','support'),('short','resistance')]:
    price=lv[key]
    if price is not None and not lv[key+'_broken']:candidates.append((f'level_{group}',side,price,price,f'{group}:{key}:{price}',False))
  if ready:
   for z in prior_zones:
    if z['phase']=='invalidated':continue
    candidates.append(('block_'+z['structure'],'short' if z['side']=='supply' else 'long',z['lower'],z['upper'],z['id'],True))
  hits=set();picked={}
  for family,side,lo,hi,key,isblock in candidates:
   short=side=='short';edge=lo if short else hi
   if (short and c.high>hi) or (not short and c.low<lo):continue # invalidated zones/strict broken level excluded
   distance=(lo-c.high if short else c.low-hi)/atr
   if distance>0.25:continue
   if short and c.close>=lo or not short and c.close<=hi:continue
   hit=(family,side,key);hits.add(hit)
   if hit in prev_hits:continue # require a fresh approach, not repeated same-zone bars
   reaction=(c.close<c.open if short else c.close>c.open)
   touched=c.high>=lo and c.low<=hi
   # Keep nearest candidate, same family/direction; long+short conflict dropped below.
   choice=(abs(distance),dict(family=family,side=side,lower=lo,upper=hi,distance_atr=max(0,distance),reaction=reaction,touched=touched,zone_id=key))
   if (family,side) not in picked or choice[0]<picked[family,side][0]:picked[family,side]=choice
  prev_hits=hits
  if i+3>=len(bars) or any(b.open_time!=a.close_time+1 for a,b in zip(bars[i:i+3],bars[i+1:i+4])):continue
  for (family,side),(_,item) in picked.items():
   if (family,'short' if side=='long' else 'long') in picked or i<next_allowed.get(family,0):continue
   sign=1 if side=='long' else -1;entry=bars[i+1].open
   if entry<=0:continue
   gross=sign*(bars[i+3].close/entry-1)*10000
   extreme=any(v is not None and (v<=-53 if side=='long' else v>=53) for v in w[-3:])
   turn=(w[-1]>w[-2] if side=='long' else w[-1]<w[-2]);momentum=(w[-1]>wt[-1] if side=='long' else w[-1]<wt[-1])
   item.update(symbol=symbol,tf=tf,time=c.close_time,exit_time=bars[i+3].close_time,wt1=w[-1],wt2=wt[-1],wt_region='oversold' if w[-1]<-53 else 'overbought' if w[-1]>53 else 'middle',recent_extreme=extreme,turn=turn,momentum=momentum,gross_bps=gross,net_bps=gross-20)
   events.append(item);next_allowed[family]=i+4
 return events

def aggregate(events):
 result=[]
 rules={'approach':lambda e:True,'reaction':lambda e:e['reaction'],'extreme':lambda e:e['recent_extreme'],'extreme_turn':lambda e:e['recent_extreme'] and e['turn'],'reaction_extreme_turn':lambda e:e['reaction'] and e['recent_extreme'] and e['turn']}
 for tf in ('1h','4h','1d'):
  for family in sorted({e['family'] for e in events}):
   for split in ('train','test'):
    group=[e for e in events if e['tf']==tf and e['family']==family and e['split']==split]
    for rule,pred in rules.items():
     sample=[e for e in group if pred(e)];days=defaultdict(list)
     for e in sample:days[(e['time']+28800000)//86400000].append(e['net_bps'])
     blocks=[(sum(v),len(v)) for v in days.values()];boot=[];rng=random.Random(42)
     if blocks:
      for _ in range(1000):
       b=rng.choices(blocks,k=len(blocks));boot.append(sum(x[0] for x in b)/sum(x[1] for x in b))
      boot.sort()
     result.append(dict(tf=tf,family=family,split=split,rule=rule,n=len(sample),symbols=len({e['symbol'] for e in sample}),days=len(days),win_pct=100*sum(e['net_bps']>0 for e in sample)/len(sample) if sample else None,mean_bps=statistics.mean(e['net_bps'] for e in sample) if sample else None,ci95=[boot[24],boot[974]] if boot else None))
 return result

def main():
 captures=json.loads((OLD/'snapshot-times.json').read_text());events=[];hashes={}
 for n,p in enumerate(sorted(OLD.glob('*USDT-*.json'))):
  symbol,tf=p.stem.rsplit('-',1)
  if tf not in ('1h','4h','1d'):continue
  hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest();events+=study(json.loads(p.read_text()),symbol,tf,captures[p.name])
  if n%60==0:print('processed',n,'events',len(events),flush=True)
 cuts=json.loads((OLD/'results.json').read_text())['cuts']
 for e in events:e['split']='test' if e['time']>=cuts[e['tf']] else 'train' if e['exit_time']<cuts[e['tf']] else 'purged'
 (OUT/'results.json').write_text(json.dumps(aggregate(events),indent=2));(OUT/'input-sha256.json').write_text(json.dumps(hashes,indent=2))
 with (OUT/'events.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(events[0]));w.writeheader();w.writerows(events)
 recent=[]
 for entry in json.loads((OUT/'fetch-log.json').read_text()):
  if 'error' in entry:continue
  p=OUT/f"{entry['symbol']}-1h-{entry['source']}.json"
  for e in study(json.loads(p.read_text()),entry['symbol'],'1h',entry['captured_ms']):e['source']=entry['source'];recent.append(e)
 (OUT/'examples.json').write_text(json.dumps(recent,indent=2));print('done',len(events),'examples',len(recent))
if __name__=='__main__':main()
