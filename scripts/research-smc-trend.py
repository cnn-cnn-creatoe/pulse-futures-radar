"""Fixed-parameter SMC trend overlay event study. Read-only production imports."""
import json,csv,sys,statistics,random,hashlib
from pathlib import Path
from collections import defaultdict
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.engine import Candle
from monitor.community_indicators import ema,sma
from monitor.indicators import _atr,_supertrend,IndicatorConfig
from monitor.smc_structure import Structure
DATA=ROOT/'research/smc-context-2026-10-08';OUT=ROOT/'research/smc-trend-2026-10-09'
PANEL=['NVDAUSDT','MUUSDT','AMDUSDT','AAPLUSDT','MSFTUSDT','TSLAUSDT','IWMUSDT','SOXSUSDT']

def features(bars):
 c=[b.close for b in bars];hi=[b.high for b in bars];lo=[b.low for b in bars];vol=[b.volume for b in bars]
 macd=[a-b for a,b in zip(ema(c,12),ema(c,26))];signal=sma(macd,9);hist=[a-b if b is not None else None for a,b in zip(macd,signal)]
 ma=sma(c,20);res=[None]*len(c);sq=[None]*len(c);released=[False]*len(c)
 tr=[None]+[max(b.high-b.low,abs(b.high-a.close),abs(b.low-a.close)) for a,b in zip(bars,bars[1:])];rng=sma(tr,20)
 for i in range(19,len(c)):
  res[i]=c[i]-((max(hi[i-19:i+1])+min(lo[i-19:i+1]))/2+ma[i])/2
  if rng[i] is not None:released[i]=1.5*statistics.pstdev(c[i-19:i+1])>1.5*rng[i] # supplied BB uses multKC
  if i>=38:
   ys=res[i-19:i+1];avg=statistics.mean(ys);slope=sum((x-9.5)*(y-avg) for x,y in enumerate(ys))/665;sq[i]=avg+slope*9.5
 _,st=_supertrend(bars,IndicatorConfig(),_atr(bars,10),full=True);stmap={r['time']:r['direction'] for r in st}
 v5=ema(vol,5);v10=ema(vol,10);highpivot=lowpivot=None;previous_high=previous_low=None;out=[]
 for i,b in enumerate(bars):
  # pivothigh(15,15)[1]: pivot at i-16 is available only now. Never backdate offset.
  if i>=31:
   j=i-16;left=bars[j-15:j];right=bars[j+1:j+16]
   if all(bars[j].high>=x.high for x in left) and all(bars[j].high>x.high for x in right):highpivot=bars[j].high
   if all(bars[j].low<=x.low for x in left) and all(bars[j].low<x.low for x in right):lowpivot=bars[j].low
  osc=100*(v5[i]-v10[i])/v10[i] if v10[i] else 0
  up=i>0 and highpivot is not None and previous_high is not None and c[i]>highpivot and c[i-1]<=previous_high and osc>20
  down=i>0 and lowpivot is not None and previous_low is not None and c[i]<lowpivot and c[i-1]>=previous_low and osc>20
  out.append(dict(st=stmap.get(b.open_time),macd=macd[i],hist=hist[i],hist_prev=hist[i-1] if i else None,sq=sq[i],sq_prev=sq[i-1] if i else None,sq_off=released[i],pivot_up=bool(up),pivot_down=bool(down)))
  previous_high,previous_low=highpivot,lowpivot
 return out

def rules(f,side):
 s=1 if side=='long' else -1
 st=f['st']==('up' if s==1 else 'down');macd=f['macd']*s>0 and f['hist']*s>0
 sq=f['sq']*s>0 and (f['sq']-f['sq_prev'])*s>0
 return dict(baseline=True,supertrend=st,macd=macd,squeeze=sq,squeeze_release=sq and f['sq_off'],st_macd=st and macd,st_squeeze=st and sq,pivot_volume=f['pivot_up'] if s==1 else f['pivot_down'])

def study(raw,symbol,tf,cutoff):
 bars=[Candle(symbol=symbol,timeframe=tf,open_time=int(r[0]),close_time=int(r[6]),open=float(r[1]),high=float(r[2]),low=float(r[3]),close=float(r[4]),volume=float(r[5]),quote_volume=float(r[7]),taker_buy_quote=float(r[10]),trades=int(r[8]),closed=True) for r in raw if int(r[6])<cutoff]
 segments=[];seg=[]
 for b in bars:
  if seg and b.open_time!=seg[-1].close_time+1:segments.append(seg);seg=[]
  seg.append(b)
 if seg:segments.append(seg)
 out=[]
 for bars in segments:
  fs=features(bars);machine=Structure();next_allowed={};previous_reactions=set()
  for i,b in enumerate(bars):
   prior=machine.zone_snapshot()['items'] if machine.bars else [];ready=machine.atr200 is not None;signals=machine.advance(b);candidates=[]
   dirs={x['direction'] for x in signals}
   if len(dirs)==1:candidates.append(('structure',next(iter(dirs))))
   reactions=set()
   if ready:
    for z in prior:
     if z['phase']=='invalidated':continue
     if z['side']=='supply' and b.high<=z['upper'] and b.high>=z['lower'] and b.close<z['lower'] and b.close<b.open:reactions.add((z['id'],'short'))
     if z['side']=='demand' and b.low>=z['lower'] and b.low<=z['upper'] and b.close>z['upper'] and b.close>b.open:reactions.add((z['id'],'long'))
   fresh=reactions-previous_reactions;previous_reactions=reactions;dirs={x[1] for x in fresh}
   if len(dirs)==1:candidates.append(('block_reaction',next(iter(dirs))))
   if i<70 or i+6>=len(bars) or any(fs[i][k] is None for k in ('hist','hist_prev','sq','sq_prev')):continue
   for family,side in candidates:
    if i<next_allowed.get(family,0):continue
    entry=bars[i+1].open
    if entry<=0:continue
    row=dict(symbol=symbol,tf=tf,family=family,time=b.close_time,exit_time=bars[i+6].close_time,side=side,entry=entry,**rules(fs[i],side));sign=1 if side=='long' else -1
    for h in (1,3,6):row[f'net{h}']=sign*(bars[i+h].close/entry-1)*10000-20
    out.append(row);next_allowed[family]=i+7
 return out

def stats(sample,h):
 if not sample:return dict(n=0,win=None,mean=None,ci=None,days=0)
 vals=[e[f'net{h}'] for e in sample];days=defaultdict(list)
 for e in sample:days[(e['time']+28800000)//86400000].append(e[f'net{h}'])
 blocks=[(sum(v),len(v)) for v in days.values()];rng=random.Random(42);boot=[]
 for _ in range(500):
  b=rng.choices(blocks,k=len(blocks));boot.append(sum(x[0] for x in b)/sum(x[1] for x in b))
 boot.sort();return dict(n=len(vals),win=sum(v>0 for v in vals)/len(vals)*100,mean=statistics.mean(vals),ci=[boot[12],boot[487]],days=len(days))

def main():
 captures=json.loads((DATA/'snapshot-times.json').read_text());cuts=json.loads((DATA/'results.json').read_text())['cuts'];events=[];coverage=[]
 for symbol in PANEL:
  for tf in ('1h','4h','1d'):
   p=DATA/f'{symbol}-{tf}.json'
   if not p.exists():continue
   raw=json.loads(p.read_text());coverage.append(dict(symbol=symbol,tf=tf,count=len(raw),start=raw[0][0],end=min(raw[-1][6],captures[p.name]),sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
   events+=study(raw,symbol,tf,captures[p.name])
 for e in events:e['split']='test' if e['time']>=cuts[e['tf']] else 'train' if e['exit_time']<cuts[e['tf']] else 'purged'
 names=['baseline','supertrend','macd','squeeze','squeeze_release','st_macd','st_squeeze','pivot_volume'];result=[]
 for symbol in ['ALL']+PANEL:
  for tf in ('1h','4h','1d'):
   for family in ('structure','block_reaction'):
    for split in ('train','test'):
     group=[e for e in events if (symbol=='ALL' or e['symbol']==symbol) and e['tf']==tf and e['family']==family and e['split']==split]
     for rule in names:
      for h in (1,3,6):result.append(dict(symbol=symbol,tf=tf,family=family,split=split,rule=rule,horizon=h,**stats([e for e in group if e[rule]],h)))
 (OUT/'results.json').write_text(json.dumps(result,indent=2));(OUT/'coverage.json').write_text(json.dumps(coverage,indent=2))
 with (OUT/'events.csv').open('w',newline='',encoding='utf-8-sig') as f:
  w=csv.DictWriter(f,fieldnames=list(events[0]));w.writeheader();w.writerows(events)
 print('events',len(events))
 for r in result:
  if r['symbol'] in ('ALL','NVDAUSDT','MUUSDT') and r['split']=='test' and r['horizon']==3 and r['tf']=='1h':print(r)
if __name__=='__main__':main()
