"""Replay frozen native-timeframe signals using contiguous five-minute execution bars."""
import importlib.util, json, bisect, hashlib
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=root/'research/smc-5m-review-2026-10-09'
data=root/'research/smc-context-2026-10-08'
spec=importlib.util.spec_from_file_location('study',root/'scripts/research-smc-entry-exit.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
caps=json.loads((data/'snapshot-times.json').read_text())
cuts=json.loads((data/'results.json').read_text())['cuts']
cache={};fine=[];starts=[]

def replay(bars,i,entry,stop,target,side,touch=False,optimistic=False):
    if i+11>=len(bars):return None
    begin,end=bars[i].open_time,bars[i+11].close_time
    key=(begin,entry,stop,target,side,touch,optimistic)
    if key in cache:return cache[key][0]
    lo=bisect.bisect_left(starts,begin);hi=bisect.bisect_right(starts,end)
    bs=fine[lo:hi]
    if not bs or bs[0][0]!=begin or bs[-1][6]!=end or any(a[6]+1!=b[0] for a,b in zip(bs,bs[1:])):
        return None
    sign=1 if side=='long' else -1
    fill=None;result=None
    for b in bs:
        t,o,h,l,c,_,ct=b[:7]
        newly=False
        if fill is None:
            if touch:
                if not l<=entry<=h:continue
                # Never assume a resting entry survives a gap beyond its stop.
                if sign*(o-stop)<=0:return None
            fill=t;newly=True
        if not (touch and newly):
            if sign*(o-stop)<=0:result=(o,'gap_stop',False,t,ct);break
            if sign*(o-target)>=0:result=(target,'gap_target',False,t,ct);break
        hs=l<=stop if sign==1 else h>=stop
        ht=h>=target if sign==1 else l<=target
        if hs and ht:result=(target if optimistic else stop,'both',True,t,ct);break
        if hs:result=(stop,'stop',False,t,ct);break
        if ht and (not touch or not newly or sign*(c-target)>=0 or optimistic):
            result=(target,'target',False,t,ct);break
    if fill is None:return None
    if result is None:result=(bs[-1][4],'timeout',False,bs[-1][0],bs[-1][6])
    price,reason,amb,t,ct=result
    j=next(j for j in range(i,i+12) if bars[j].open_time<=t<=bars[j].close_time)
    value=(j,price,reason,amb)
    cache[key]=(value,dict(entry_5m=fill,exit_5m=t,exit_5m_end=ct))
    return value

m.simulate=replay
trades=[];audit={};hashes={}
for fp in sorted(out.glob('*USDT-5m.json')):
    symbol=fp.stem[:-3]
    raw5=json.loads(fp.read_text());fine=[]
    for r in raw5:
        b=list(r)
        for k in (1,2,3,4,5):b[k]=float(b[k])
        fine.append(b)
    starts=[b[0] for b in fine]
    hashes[fp.name]=hashlib.sha256(fp.read_bytes()).hexdigest()
    for tf in ('1h','4h','1d'):
        cache={};p=data/f'{symbol}-{tf}.json'
        hashes[p.name]=hashlib.sha256(p.read_bytes()).hexdigest()
        ts,counts=m.run_series(json.loads(p.read_text()),symbol,tf,caps[p.name])
        for t in ts:
            key=(t['time'],t['entry'],t['stop'],t['target'],t['side'],t['mode']=='touch',False)
            t.update(cache[key][1]);t['native_entry_time']=t['time']
            t['time']=t['entry_5m'];t['exit_time']=t['exit_5m_end']
            t['split']='test' if t['time']>=cuts[tf] else 'train' if t['exit_time']<cuts[tf] else 'purged'
        trades.extend(ts);audit[f'{symbol}-{tf}']=counts
    print(symbol,len(trades),flush=True)
(out/'replayed-trades.json').write_text(json.dumps(trades,ensure_ascii=False),encoding='utf-8')
(out/'replay-audit.json').write_text(json.dumps(dict(audit=audit,hashes=hashes,rows=len(trades),ambiguous=sum(t['ambiguous'] for t in trades),rule='Frozen native signals; full 12-native-bar 5m coverage; same-native-bar reentry prohibited; 5m dual hits stop first; 20bps cost'),indent=2),encoding='utf-8')
print('Finished',len(trades))
