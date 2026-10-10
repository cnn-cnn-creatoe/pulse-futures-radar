"""Frozen causal entry/exit comparison. Production code is imported read-only."""
import sys,json,csv,statistics,random,hashlib
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.engine import Candle
from monitor.smc_structure import Structure
from monitor.indicators import _atr,_supertrend,IndicatorConfig
DATA=ROOT/'research/smc-context-2026-10-08';OUT=ROOT/'research/smc-entry-exit-2026-10-09'

def simulate(bars,i,entry,stop,target,side,touch=False,optimistic=False):
 sign=1 if side=='long' else -1
 if i+11>=len(bars):return None
 for j in range(i,i+12):
  b=bars[j]
  if j>i or not touch:
   if sign*(b.open-stop)<=0:return (j,b.open,'gap_stop',False)
   if sign*(b.open-target)>=0:return (j,target,'gap_target',False)
  hitstop=b.low<=stop if sign==1 else b.high>=stop
  hittarget=b.high>=target if sign==1 else b.low<=target
  if hitstop and hittarget:return (j,target if optimistic else stop,'both',True)
  if hitstop:return (j,stop,'stop',False)
  # On touch bar an opposite extreme could precede the fill. Target hits
  # are indeterminate unless close is beyond target; pessimistic waits.
  if hittarget and (not touch or j!=i or sign*(b.close-target)>=0 or optimistic):return (j,target,'target',False)
 return (i+11,bars[i+11].close,'timeout',False)

def opposing(machine,side,entry):
 vals=[]
 for l in machine.levels().values():
  key='resistance' if side=='long' else 'support'
  if l[key] is not None and not l[key+'_broken']:vals.append(l[key])
 for z in machine.zone_snapshot()['items']:
  if z['phase']=='invalidated':continue
  if side=='long' and z['side']=='supply':vals.append(z['lower'])
  if side=='short' and z['side']=='demand':vals.append(z['upper'])
 valid=[v for v in vals if v>entry] if side=='long' else [v for v in vals if v<entry]
 return (min(valid) if side=='long' else max(valid)) if valid else None

def candidates(bars):
 machine=Structure();atr=_atr(bars,14,'sma');_,st=_supertrend(bars,IndicatorConfig(),_atr(bars,10),full=True);trend={p['time']:p['direction'] for p in st};seen=set();pending=[];retests=[];out=[]
 def add(mode,side,i,entry,stop,target,asof,touch=False):
  if i>=len(bars) or entry<=0:return
  risk=(entry-stop) if side=='long' else (stop-entry)
  if risk/entry<.001:return
  out.append(dict(mode=mode,side=side,i=i,entry=entry,stop=stop,risk=risk,target=target,asof=asof,touch=touch,aligned=trend.get(asof)==('up' if side=='long' else 'down')))
 for i,b in enumerate(bars):
  previous=list(machine.bars);ready=machine.atr200 is not None;zones=machine.zone_snapshot()['items'] if previous else [];a=atr[i-1] if i else None
  if ready and a:
   for z in zones:
    if z['phase']=='invalidated' or z['id'] in seen:continue
    if b.high<z['lower'] or b.low>z['upper']:continue
    seen.add(z['id']);side='short' if z['side']=='supply' else 'long';edge=z['lower'] if side=='short' else z['upper'];stop=z['upper']+.1*a if side=='short' else z['lower']-.1*a
    # Only a limit approached from outside the zone: no assumed fill through a gap stop.
    priorclose=previous[-1].close
    approached=priorclose<edge if side=='short' else priorclose>edge
    gapinvalid=b.open>=stop if side=='short' else b.open<=stop
    if approached and not gapinvalid:add('touch',side,i,edge,stop,opposing(machine,side,edge),previous[-1].open_time,True)
    pending.append(dict(z=z,side=side,end=i+3,stop=stop))
  signals=machine.advance(b)
  active={z['id'] for z in machine.zone_snapshot()['items'] if z['phase']!='invalidated'}
  keep=[]
  for p in pending:
   z=p['z'];side=p['side']
   if i>p['end'] or z['id'] not in active:continue
   confirmed=b.close<z['lower'] and b.close<b.open if side=='short' else b.close>z['upper'] and b.close>b.open
   if confirmed and i+1<len(bars):
    entry=bars[i+1].open;add('confirm',side,i+1,entry,p['stop'],opposing(machine,side,entry),b.open_time)
   else:keep.append(p)
  pending=keep;keep=[]
  for p in retests:
   if i>p['end']:continue
   side=p['side'];level=p['level'];valid=b.close>=level if side=='long' else b.close<=level
   if not valid:continue
   touched=b.low<=level if side=='long' else b.high>=level
   confirmed=b.close>b.open if side=='long' else b.close<b.open
   if touched and confirmed and a and i+1<len(bars):
    entry=bars[i+1].open;stop=b.low-.1*a if side=='long' else b.high+.1*a
    add('retest',side,i+1,entry,stop,opposing(machine,side,entry),b.open_time)
   else:keep.append(p)
  retests=keep
  # This study requires mature structure/blocks for all modes.
  if ready:
   for s in signals:retests.append(dict(side=s['direction'],level=s['level'],end=i+3))
 return out

def run_series(raw,symbol,tf,cutoff):
 bars=[Candle(symbol=symbol,timeframe=tf,open_time=int(r[0]),close_time=int(r[6]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5]),quote_volume=float(r[7]),taker_buy_quote=float(r[10]),trades=int(r[8]),closed=True) for r in raw if int(r[6])<cutoff]
 segments=[];seg=[]
 for b in bars:
  if seg and b.open_time!=seg[-1].close_time+1:segments.append(seg);seg=[]
  seg.append(b)
 if seg:segments.append(seg)
 trades=[];audit=defaultdict(int)
 for bars in segments:
  cs=candidates(bars);audit['candidates']+=len(cs)
  for mode in ('touch','confirm','retest'):
   for exitmode in ('2R','opposing'):
    for filt in ('none','supertrend'):
     allowed=0
     for c in sorted([c for c in cs if c['mode']==mode],key=lambda c:c['i']):
      if c['i']<allowed:continue
      if filt=='supertrend' and not c['aligned']:continue
      sign=1 if c['side']=='long' else -1;target=c['entry']+sign*2*c['risk'] if exitmode=='2R' else c['target']
      if target is None:audit['missing_opposing']+=1;continue
      result=simulate(bars,c['i'],c['entry'],c['stop'],target,c['side'],c['touch'])
      if result is None:audit['insufficient_future']+=1;continue
      j,exitprice,reason,ambiguous=result;best=simulate(bars,c['i'],c['entry'],c['stop'],target,c['side'],c['touch'],True)
      gross=sign*(exitprice/c['entry']-1)*10000;riskbps=c['risk']/c['entry']*10000
      trades.append(dict(symbol=symbol,tf=tf,mode=mode,exit=exitmode,filter=filt,side=c['side'],time=bars[c['i']].open_time,exit_time=bars[j].close_time,entry=c['entry'],stop=c['stop'],target=target,exitprice=exitprice,reason=reason,ambiguous=ambiguous,gross_bps=gross,net_bps=gross-20,net_r=(gross-20)/riskbps,optimistic_bps=sign*(best[1]/c['entry']-1)*10000-20,holding=j-c['i']+1))
      allowed=j+1
 return trades,dict(audit)

def stats(ts):
 if not ts:return dict(n=0)
 vals=[t['net_r'] for t in ts];w=[v for v in vals if v>0];l=[v for v in vals if v<=0];path=peak=dd=0
 for t in sorted(ts,key=lambda t:(t['exit_time'],t['symbol'])):
  path+=t['net_r'];peak=max(peak,path);dd=max(dd,peak-path)
 days=defaultdict(list)
 for t in ts:days[(t['time']+28800000)//86400000].append(t['net_r'])
 blocks=[(sum(v),len(v)) for v in days.values()];rng=random.Random(42);boot=[]
 for _ in range(500):
  sel=rng.choices(blocks,k=len(blocks));boot.append(sum(x[0] for x in sel)/sum(x[1] for x in sel))
 boot.sort()
 return dict(n=len(ts),symbols=len({t['symbol'] for t in ts}),days=len(days),win=100*len(w)/len(vals),mean_r=statistics.mean(vals),mean_bps=statistics.mean(t['net_bps'] for t in ts),mean_win_r=statistics.mean(w) if w else None,mean_loss_r=statistics.mean(l) if l else None,profit_factor=sum(w)/-sum(l) if sum(l)<0 else None,max_closed_trade_drawdown_r=dd,ci_r=[boot[12],boot[487]],ambiguous=sum(t['ambiguous'] for t in ts),optimistic_mean_bps=statistics.mean(t['optimistic_bps'] for t in ts))

def main():
 captures=json.loads((DATA/'snapshot-times.json').read_text());cuts=json.loads((DATA/'results.json').read_text())['cuts'];alltrades=[];audit={};hashes={}
 files=sorted(DATA.glob('*USDT-*.json'))
 for n,p in enumerate(files):
  symbol,tf=p.stem.rsplit('-',1)
  if tf not in cuts:continue
  hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest();trades,counts=run_series(json.loads(p.read_text()),symbol,tf,captures[p.name]);alltrades+=trades;audit[p.name]=counts
  if n%90==0:print('processed',n,'trades',len(alltrades),flush=True)
 for t in alltrades:t['split']='test' if t['time']>=cuts[t['tf']] else 'train' if t['exit_time']<cuts[t['tf']] else 'purged'
 results=[]
 for universe in ('ALL','NVDAUSDT','MUUSDT','OTHER'):
  for tf in cuts:
   for mode in ('touch','confirm','retest'):
    for ex in ('2R','opposing'):
     for fil in ('none','supertrend'):
      for split in ('train','test'):
       ts=[t for t in alltrades if (universe=='ALL' or universe=='OTHER' and t['symbol'] not in ('NVDAUSDT','MUUSDT') or t['symbol']==universe) and t['tf']==tf and t['mode']==mode and t['exit']==ex and t['filter']==fil and t['split']==split]
       results.append(dict(universe=universe,tf=tf,mode=mode,exit=ex,filter=fil,split=split,**stats(ts)))
 (OUT/'results.json').write_text(json.dumps(results,indent=2));(OUT/'audit.json').write_text(json.dumps(audit,indent=2));(OUT/'input-sha256.json').write_text(json.dumps(hashes,indent=2))
 with (OUT/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(alltrades[0]));w.writeheader();w.writerows(alltrades)
 print('finished',len(alltrades))
 for r in results:
  if r['universe']=='ALL' and r['split']=='test' and r['tf']=='1h':print(r)
if __name__=='__main__':main()
