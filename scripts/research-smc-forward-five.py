"""Entry-only, causal five-native-bar event study. No production strategy changes."""
import json, importlib.util, bisect, statistics, hashlib, random, sys
from pathlib import Path
from collections import defaultdict
from datetime import datetime,timezone,timedelta
root=Path(__file__).resolve().parents[1]
out=root/'research/smc-forward-five-2026-10-09';out.mkdir(exist_ok=True)
data=root/'research/smc-context-2026-10-08'
five=root/'research/smc-5m-review-2026-10-09'
annual='--annual' in sys.argv
if annual:
 out=root/'research/smc-year-2026-10-08';data=out/'data';five=data
spec=importlib.util.spec_from_file_location('entry',root/'scripts/research-smc-entry-exit.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
caps=json.loads((data/'snapshot-times.json').read_text())
rules={
 'baseline':('原组合',lambda r:True),
 'streak':('避开连续3根反向K线',lambda r:not r['counter_streak']),
 'volume':('前根相对成交额≥0.8倍',lambda r:r['volume_ratio'] is not None and r['volume_ratio']>=.8),
 'range':('前根振幅≤2 ATR',lambda r:r['range_atr']<=2),
 'room':('反向结构空间≥1 ATR',lambda r:r['room_atr'] is not None and r['room_atr']>=1),
}
(out/'protocol.json').write_text(json.dumps(dict(primary='4h touch aligned Supertrend',horizon='next 1..5 COMPLETE native candles, excluding entry candle',reference='entry price; 5m touch execution',selection='chronological first 70% wall-clock development, last30% validation; purge horizon5 crossing cutoff; primary horizon3; only 4 fixed single filters, never combine after seeing validation',cost_percent=.2,minimum_train=12,minimum_validation=8,warning='Reused previously viewed sample; not fresh independent validation'),indent=2),encoding='utf-8')
if annual:
 (out/'protocol.json').write_text(json.dumps(dict(primary='4h touch aligned Supertrend; fixed volume >=0.8',start='2026-01-01 Asia/Shanghai',end='2026-10-08 23:59:59 Asia/Shanghai',development='before 2026-07-01 Asia/Shanghai',validation='2026-07-01 through 2026-10-08; horizon5 boundary purge',horizon='next 1..5 complete native candles excluding entry candle; all returns cumulative relative to entry',cost_percent=.2,warning='Historical exploration; July-October contains previously seen data; no claim of fresh independent validation; current surviving 10-contract universe',minimum_train=50,minimum_validation=30),indent=2),encoding='utf-8')
events=[];hashes={};skips=defaultdict(int)
for fp in sorted(five.glob('*USDT-5m.json')):
 symbol=fp.stem[:-3];f=json.loads(fp.read_text());starts=[int(b[0]) for b in f]
 hashes[fp.name]=hashlib.sha256(fp.read_bytes()).hexdigest()
 for tf in ('1h','4h','1d'):
  p=data/f'{symbol}-{tf}.json';hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
  raw=[r for r in json.loads(p.read_text()) if int(r[6])<caps[p.name]]
  segments=[];seg=[]
  for r in raw:
   b=m.Candle(symbol=symbol,timeframe=tf,open_time=int(r[0]),close_time=int(r[6]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5]),quote_volume=float(r[7]),taker_buy_quote=float(r[10]),trades=int(r[8]),closed=True)
   if seg and b.open_time!=seg[-1].close_time+1:segments.append(seg);seg=[]
   seg.append(b)
  if seg:segments.append(seg)
  seen=set()
  for bars in segments:
   atr=m._atr(bars,14,'sma')
   for c in m.candidates(bars):
    i=c['i']
    if i+5>=len(bars):skips['future_missing']+=1;continue
    a=bisect.bisect_left(starts,bars[i].open_time);z=bisect.bisect_right(starts,bars[i+5].close_time)
    sub=f[a:z]
    if not sub or sub[0][0]!=bars[i].open_time or sub[-1][6]!=bars[i+5].close_time or any(x[6]+1!=y[0] for x,y in zip(sub,sub[1:])):
     skips['five_minute_coverage_missing']+=1;continue
    filled=bars[i].open_time
    if c['touch']:
     hits=[b for b in sub if b[0]<=bars[i].close_time and float(b[3])<=c['entry']<=float(b[2])]
     if not hits:skips['no_touch_fill']+=1;continue
     hit=hits[0]
     if (float(hit[1])<=c['stop'] if c['side']=='long' else float(hit[1])>=c['stop']):skips['invalid_gap']+=1;continue
     filled=hit[0]
    key=(symbol,tf,c['mode'],c['side'],filled,c['entry'])
    if key in seen:continue
    seen.add(key);prior=bars[i-1];av=atr[i-1]
    if not av:continue
    candle_side=1 if prior.close>prior.open else -1 if prior.close<prior.open else 0
    run=0
    for b in reversed(bars[:i]):
     s=1 if b.close>b.open else -1 if b.close<b.open else 0
     if s!=candle_side or not s:break
     run+=1
    sign=1 if c['side']=='long' else -1
    vols=[b.quote_volume for b in bars[max(0,i-21):i-1]]
    ratio=prior.quote_volume/statistics.mean(vols) if len(vols)==20 and statistics.mean(vols)>0 else None
    room=sign*(c['target']-c['entry'])/av if c['target'] is not None else None
    # All outcome prices come from the newly downloaded execution feed.
    closes={int(b[6]):float(b[4]) for b in sub}
    future=[closes[bars[i+h].close_time] for h in range(1,6)]
    price=[(v/c['entry']-1)*100 for v in future]
    events.append(dict(id=len(events),symbol=symbol,tf=tf,mode=c['mode'],side=c['side'],time=filled,entry=c['entry'],native_time=bars[i].open_time,end_time=bars[i+5].close_time,future_times=[bars[i+h].close_time for h in range(1,6)],future_prices=future,price=price,directional=[sign*v for v in price],net=[sign*v-.2 for v in price],aligned=c['aligned'],volume_ratio=ratio,range_atr=(prior.high-prior.low)/av,atr_pct=av/c['entry']*100,streak=run,streak_side=candle_side,counter_streak=run>=3 and candle_side==-sign,room_atr=room,context_time=prior.close_time))
 print(symbol,len(events),flush=True)
# Shared time cutoff per native timeframe, fixed by coverage span rather than returns.
cuts={}
for tf in ('1h','4h','1d'):
 v=[r for r in events if r['tf']==tf]
 if v:cuts[tf]=1782835200000 if annual else int(min(r['time'] for r in v)+.7*(max(r['time'] for r in v)-min(r['time'] for r in v)))
for r in events:r['split']='test' if r['time']>=cuts[r['tf']] else 'train' if r['end_time']<cuts[r['tf']] else 'purged'
def stats(v):
 if not v:return dict(n=0)
 return dict(n=len(v),symbols=len({r['symbol'] for r in v}),days=len({r['time']//86400000 for r in v}),win=[100*sum(r['net'][h]>0 for r in v)/len(v) for h in range(5)],mean=[statistics.mean(r['net'][h] for r in v) for h in range(5)],median=[statistics.median(r['net'][h] for r in v) for h in range(5)])
base=[r for r in events if r['tf']=='4h' and r['mode']=='touch' and r['aligned']]
summary=[]
for key,(label,pred) in rules.items():
 summary.append(dict(key=key,label=label,train=stats([r for r in base if r['split']=='train' and pred(r)]),test=stats([r for r in base if r['split']=='test' and pred(r)]),all=stats([r for r in base if pred(r)])))
eligible=[s for s in summary[1:] if s['train']['n']>=(50 if annual else 12)]
chosen=max(eligible,key=lambda s:s['train']['mean'][2]) if eligible else None
if chosen and (summary[0]['train']['n']==0 or chosen['train']['mean'][2]<=summary[0]['train']['mean'][2]):chosen=None
reasons=[]
for key,(label,pred) in list(rules.items())[1:]:
 for split in ('train','test'):
  v=[r for r in base if r['split']==split]
  reasons.append(dict(key=key,label=label,split=split,pass_group=stats([r for r in v if pred(r)]),fail_group=stats([r for r in v if not pred(r)])))
profitloss=[]
for split in ('train','test','all'):
 for label,fn in [('盈利',lambda r:r['net'][2]>0),('亏损或持平',lambda r:r['net'][2]<=0)]:
  v=[r for r in base if (split=='all' or r['split']==split) and fn(r)]
  def avg(k):
   a=[r[k] for r in v if r[k] is not None];return statistics.mean(a) if a else None
  profitloss.append(dict(split=split,label=label,**stats(v),volume_ratio=avg('volume_ratio'),range_atr=avg('range_atr'),room_atr=avg('room_atr'),counter_streak_pct=100*sum(r['counter_streak'] for r in v)/len(v) if v else None))
results=dict(total=len(events),cuts=cuts,primary_count=len(base),summary=summary,chosen=chosen['key'] if chosen else None,reasons=reasons,profitloss=profitloss,skips=dict(skips),hashes=hashes)
if annual:
 comparisons=[];monthly=[];by_symbol=[]
 for tf in ('1h','4h','1d'):
  for mode in ('touch','confirm','retest'):
   for fil in ('none','trend','trend_volume'):
    group=[r for r in events if r['tf']==tf and r['mode']==mode and (fil=='none' or r['aligned']) and (fil!='trend_volume' or rules['volume'][1](r))]
    for split in ('train','test','all'):
     comparisons.append(dict(tf=tf,mode=mode,filter=fil,split=split,**stats([r for r in group if split=='all' or r['split']==split])))
 for r in events:r['month']=datetime.fromtimestamp(r['time']/1000,timezone(timedelta(hours=8))).strftime('%Y-%m')
 for fil in ('baseline','volume'):
  group=[r for r in base if rules[fil][1](r)]
  for month in sorted({r['month'] for r in base}):monthly.append(dict(month=month,filter=fil,**stats([r for r in group if r['month']==month])))
  for symbol in sorted({r['symbol'] for r in base}):by_symbol.append(dict(symbol=symbol,filter=fil,**stats([r for r in group if r['symbol']==symbol and r['split']=='test'])))
 diagnostics=[]
 for fil in ('baseline','volume'):
  group=[r for r in base if rules[fil][1](r) and r['split']=='test']
  if not group:continue
  weeks=defaultdict(list)
  for r in group:weeks[(r['time']+28800000)//604800000].append(r['net'][2])
  blocks=[(sum(v),len(v)) for v in weeks.values()];rng=random.Random(42);boot=[]
  for _ in range(2000):
   sample=rng.choices(blocks,k=len(blocks));boot.append(sum(v[0] for v in sample)/sum(v[1] for v in sample))
  boot.sort()
  loo=[]
  for symbol in {r['symbol'] for r in group}:
   v=[r['net'][2] for r in group if r['symbol']!=symbol]
   if v:loo.append(dict(excluded=symbol,mean=statistics.mean(v)))
  # Fixed spacing sensitivity: retain first event, then suppress events through horizon 5.
  allowed={};spaced=[]
  for r in sorted(group,key=lambda r:(r['time'],r['id'])):
   if r['time']<=allowed.get(r['symbol'],-1):continue
   spaced.append(r);allowed[r['symbol']]=r['end_time']
  diagnostics.append(dict(filter=fil,weeks=len(blocks),weekly_cluster_ci_h3=[boot[49],boot[1949]],without_best_h3=statistics.mean(sorted(r['net'][2] for r in group)[:-1]) if len(group)>1 else None,leave_one_symbol_out=loo,spaced=stats(spaced),long=stats([r for r in group if r['side']=='long']),short=stats([r for r in group if r['side']=='short'])))
 results.update(comparisons=comparisons,monthly=monthly,by_symbol=by_symbol,diagnostics=diagnostics)
(out/'events.json').write_text(json.dumps(events,ensure_ascii=False),encoding='utf-8')
(out/'results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
assert all(r['context_time']<r['time']<r['future_times'][0] and len(r['net'])==5 and r['future_times']==sorted(r['future_times']) for r in events)
assert all(abs(r['net'][h]-(1 if r['side']=='long' else -1)*(r['future_prices'][h]/r['entry']-1)*100+.2)<1e-8 for r in events for h in range(5))
print(json.dumps({k:results[k] for k in ('total','primary_count','summary','chosen','profitloss')},ensure_ascii=False,indent=2))
