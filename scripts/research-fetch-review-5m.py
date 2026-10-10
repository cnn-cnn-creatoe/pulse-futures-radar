import json,time,urllib.request,urllib.error,threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor,as_completed
ROOT=Path(__file__).resolve().parents[1];OLD=ROOT/'research/smc-context-2026-10-08';OUT=ROOT/'research/smc-5m-review-2026-10-09';OUT.mkdir(exist_ok=True)
symbols=['TSLAUSDT','INTCUSDT','HOODUSDT','PLTRUSDT','MSTRUSDT','CRCLUSDT','COINUSDT','AMZNUSDT','NVDAUSDT','MUUSDT'];caps=json.loads((OLD/'snapshot-times.json').read_text());lock=threading.Lock();last=[0.];stop=threading.Event()
def fetch(s):
 name=s+'-1h.json';coarse=[r for r in json.loads((OLD/name).read_text()) if r[6]<caps[name]];start=coarse[0][0];end=coarse[-1][6];cursor=start;rows=[];error=None
 while cursor<=end and not stop.is_set():
  with lock:
   time.sleep(max(0,.55-(time.monotonic()-last[0])));last[0]=time.monotonic()
  url=f'https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval=5m&startTime={cursor}&endTime={end}&limit=1000'
  try:
   with urllib.request.urlopen(url,timeout=20) as r:part=json.load(r)
   if not isinstance(part,list) or not part:break
   rows.extend(part);nextcursor=part[-1][6]+1
   if nextcursor<=cursor:raise ValueError('non advancing response')
   cursor=nextcursor
  except Exception as e:
   error=str(e)
   if isinstance(e,urllib.error.HTTPError) and e.code in (418,429,451):stop.set()
   break
 unique=sorted({r[0]:r for r in rows if start<=r[0] and r[6]<=end}.values(),key=lambda r:r[0]);(OUT/f'{s}-5m.json').write_text(json.dumps(unique))
 complete=bool(unique and unique[0][0]==start and unique[-1][6]==end and all(b[0]==a[6]+1 for a,b in zip(unique,unique[1:])))
 return dict(symbol=s,count=len(unique),start=start,end=end,complete=complete,error=error,stopped=stop.is_set())
results=[]
with ThreadPoolExecutor(max_workers=3) as pool:
 for f in as_completed([pool.submit(fetch,s) for s in symbols]):
  result=f.result();results.append(result);print(json.dumps(result),flush=True);(OUT/'download-summary.json').write_text(json.dumps(results,indent=2))
