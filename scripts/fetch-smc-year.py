import json,time,urllib.request,urllib.error,threading
from pathlib import Path
from datetime import datetime,timezone,timedelta
from concurrent.futures import ThreadPoolExecutor,as_completed
root=Path(__file__).resolve().parents[1];out=root/'research/smc-year-2026-10-08/data';out.mkdir(parents=True,exist_ok=True)
tz=timezone(timedelta(hours=8))
stamp=lambda s:int(datetime.fromisoformat(s).replace(tzinfo=tz).timestamp()*1000)
start=stamp('2026-01-01');end=stamp('2026-10-09')-1
symbols=['TSLAUSDT','INTCUSDT','HOODUSDT','PLTRUSDT','MSTRUSDT','CRCLUSDT','COINUSDT','AMZNUSDT','NVDAUSDT','MUUSDT']
lock=threading.Lock();last=[0.];stop=threading.Event()
def fetch(symbol,tf):
 p=out/f'{symbol}-{tf}.json'
 try:rows=json.loads(p.read_text()) if p.exists() else []
 except json.JSONDecodeError:rows=[]
 begin=start if tf=='5m' else stamp('2025-01-01')
 cursor=rows[-1][6]+1 if rows else begin;error=None;retries=0
 while cursor<=end and not stop.is_set():
  with lock:
   time.sleep(max(0,.75-(time.monotonic()-last[0])));last[0]=time.monotonic()
  url=f'http://127.0.0.1:8765/api/research/klines?symbol={symbol}&interval={tf}&startTime={cursor}&endTime={end}'
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={"X-Pulse-Client":"dashboard"}),timeout=180) as response:part=json.load(response)
   if not isinstance(part,list):raise ValueError(str(part))
   if not part:break
   nxt=part[-1][6]+1
   if nxt<=cursor:raise ValueError('non advancing data')
   rows.extend(r for r in part if r[6]<=end);cursor=nxt;retries=0;error=None
   temp=p.with_suffix('.tmp');temp.write_text(json.dumps(rows));temp.replace(p)
   if len(part)<499:break
  except Exception as e:
   error=str(e)
   if isinstance(e,urllib.error.HTTPError) and e.code in (403,418,429,451,503):stop.set()
   elif retries<2:
    retries+=1;time.sleep(retries);continue
   break
 p.write_text(json.dumps(rows))
 return dict(symbol=symbol,tf=tf,n=len(rows),first=rows[0][0] if rows else None,last=rows[-1][6] if rows else None,gaps=sum(a[6]+1!=b[0] for a,b in zip(rows,rows[1:])),error=error,stopped=stop.is_set())
results=[]
with ThreadPoolExecutor(max_workers=8) as pool:
 jobs=[pool.submit(fetch,s,tf) for tf in ('1d','4h','1h','5m') for s in symbols]
 for future in as_completed(jobs):
  r=future.result();results.append(r);print(json.dumps(r),flush=True)
  (out.parent/'download-summary.json').write_text(json.dumps(results,indent=2))
(out/'snapshot-times.json').write_text(json.dumps({f'{r["symbol"]}-{r["tf"]}.json':end+1 for r in results}))
print('DONE',len(results),flush=True)
