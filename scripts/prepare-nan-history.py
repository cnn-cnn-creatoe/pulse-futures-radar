"""Prepare verified monthly BTCUSDT 5m archives for the requested 3-year run."""
import sys,json,hashlib,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from monitor.history_data import download_binance_month,parse_candles_csv
from monitor.research import Research
research=Research(ROOT/'backend/data/backtest')
archive=research.root/'archives'
for year in range(2023,2027):
 for month in range(1,13):
  label=f'{year}-{month:02}'
  if not '2023-09'<=label<='2026-08':continue
  name=f'BTCUSDT-5m-{label}.zip';path=archive/name;check=archive/(name+'.CHECKSUM')
  if path.exists() and check.exists() and hashlib.sha256(path.read_bytes()).hexdigest()==check.read_text().split()[0]:
   with zipfile.ZipFile(path) as z:bars=parse_candles_csv(z.read(name[:-4]+'.csv').decode('utf-8-sig'),'BTCUSDT','5m')
   meta=json.loads((archive/(name+'.metadata.json')).read_text('utf-8'))
  else:
   result=download_binance_month('BTCUSDT','5m',label,archive);bars=result['candles'];meta=result['metadata']
  research.register(bars,meta,label='BTCUSDT 5m '+label)
  print(label,len(bars),'verified',flush=True)
print('READY',flush=True)
