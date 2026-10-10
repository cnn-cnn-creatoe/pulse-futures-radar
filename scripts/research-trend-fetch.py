import json,time,urllib.request,urllib.error
from pathlib import Path
out=Path('research/smc-trend-2026-10-09');log=[]
# Fixed representative panel before viewing results: semiconductor, mega-cap tech, ETF.
for s in ['NVDAUSDT','MUUSDT','AMDUSDT','AAPLUSDT','MSFTUSDT','TSLAUSDT','IWMUSDT','SOXSUSDT']:
 for tf in ['1h','4h','1d']:
  p=out/f'{s}-{tf}.json'
  try:
   with urllib.request.urlopen(f'https://fapi.binance.com/fapi/v1/klines?symbol={s}&interval={tf}&limit=1000',timeout=18) as r:rows=json.load(r)
   p.write_text(json.dumps(rows));log.append(dict(file=p.name,count=len(rows),captured_ms=int(time.time()*1000)))
  except Exception as e:
   log.append(dict(file=p.name,error=str(e)))
   if isinstance(e,urllib.error.HTTPError) and e.code in (418,429,451):break
  time.sleep(.55)
 else:continue
 break
(out/'fetch-log.json').write_text(json.dumps(log,indent=2));print(json.dumps(log))
