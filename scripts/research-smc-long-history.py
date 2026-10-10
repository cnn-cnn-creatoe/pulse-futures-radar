import importlib.util,json,csv,hashlib
from pathlib import Path
root=Path.cwd();data=root/'research/smc-context-2026-10-08';out=root/'research/smc-long-history-2026-10-09'
sp=importlib.util.spec_from_file_location('study',root/'scripts/research-smc-entry-exit.py');m=importlib.util.module_from_spec(sp);sp.loader.exec_module(m)
caps=json.loads((data/'snapshot-times.json').read_text());cuts=json.loads((data/'results.json').read_text())['cuts'];selected=[]
for p in sorted(data.glob('*USDT-1d.json')):
 rows=[r for r in json.loads(p.read_text()) if r[6]<caps[p.name]];n=1 if rows else 0
 for a,b in zip(reversed(rows[:-1]),reversed(rows[1:])):
  if b[0]!=a[6]+1:break
  n+=1
 if n>=240:selected.append(dict(symbol=p.stem[:-3],continuous_days=n))
(out/'selection.json').write_text(json.dumps({'criterion':'at least 240 trailing contiguous closed daily bars; before examining returns','items':selected},indent=2))
trades=[];coverage=[]
for c in selected:
 for tf in cuts:
  p=data/f"{c['symbol']}-{tf}.json";raw=json.loads(p.read_text());t,_=m.run_series(raw,c['symbol'],tf,caps[p.name]);trades+=t
  closed=[r for r in raw if r[6]<caps[p.name]];coverage.append(dict(symbol=c['symbol'],tf=tf,closed=len(closed),start=closed[0][0],end=closed[-1][6],sha256=hashlib.sha256(p.read_bytes()).hexdigest()))
for t in trades:t['split']='test' if t['time']>=cuts[t['tf']] else 'train' if t['exit_time']<cuts[t['tf']] else 'purged'
results=[]
for symbol in ['ALL']+[c['symbol'] for c in selected]:
 for tf in cuts:
  for mode in ('touch','confirm','retest'):
   for ex in ('2R','opposing'):
    for filt in ('none','supertrend'):
     for split in ('train','test'):
      ts=[t for t in trades if (symbol=='ALL' or t['symbol']==symbol) and all(t[k]==v for k,v in [('tf',tf),('mode',mode),('exit',ex),('filter',filt),('split',split)])]
      results.append(dict(symbol=symbol,tf=tf,mode=mode,exit=ex,filter=filt,split=split,**m.stats(ts)))
(out/'results.json').write_text(json.dumps(results,indent=2));(out/'coverage.json').write_text(json.dumps(coverage,indent=2))
with (out/'trades.csv').open('w',newline='',encoding='utf-8-sig') as f:
 w=csv.DictWriter(f,fieldnames=list(trades[0]));w.writeheader();w.writerows(trades)
for r in results:
 if r['symbol']=='ALL' and r['split']=='test' and r['exit']=='2R' and r['filter']=='none':print({k:r.get(k) for k in ['tf','mode','n','win','mean_bps','mean_r']})
print('scenario rows',len(trades))
