"""Fetch a reproducible research snapshot; never changes production candles."""
import json,time,urllib.request,urllib.error
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
from threading import Lock
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'research/smc-context-2026-10-08';OUT.mkdir(parents=True,exist_ok=True)
contracts=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/smc/contracts'))['items']
(OUT/'contracts.json').write_text(json.dumps(contracts),encoding='utf-8')
lock=Lock();last=[0.0];stop=[False]
def get(pair):
 s,tf=pair;p=OUT/f'{s}-{tf}.json'
 if p.exists():return 'cached'
 for attempt in range(3):
  with lock:
   if stop[0]:return 'stopped'
   time.sleep(max(0,.55-(time.monotonic()-last[0])));last[0]=time.monotonic()
  try:
   url=f'https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval={tf}&limit=499'
   with urllib.request.urlopen(url,timeout=25) as r:rows=json.load(r)
   if not isinstance(rows,list):raise ValueError(str(rows)[:100])
   p.write_text(json.dumps(rows),encoding='utf-8');return 'ok'
  except urllib.error.HTTPError as e:
   if e.code in (418,429,451):stop[0]=True
   err=str(e)
  except Exception as e:err=str(e)
  time.sleep(2+attempt)
 return s+' '+tf+' '+err
pairs=[(c['symbol'],tf) for c in contracts for tf in ('1h','4h','1d')]
results=[]
with ThreadPoolExecutor(max_workers=3) as pool:
 for i,f in enumerate(as_completed([pool.submit(get,p) for p in pairs]),1):
  result=f.result();results.append(result)
  if i%30==0 or result not in ('ok','cached'):print(i,len(pairs),result,flush=True)
(OUT/'download-summary.json').write_text(json.dumps(results),encoding='utf-8')
print('completed',len(results),'successful',sum(r in ('ok','cached') for r in results),flush=True)
