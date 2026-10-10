import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]/'research/smc-year-2026-10-08'
coverage=json.loads((root/'download-summary.json').read_text())
checks=[]
for fp in sorted((root/'data').glob('*-5m.json')):
 symbol=fp.stem[:-3];fine=json.loads(fp.read_text());closes={r[6]:float(r[4]) for r in fine}
 for tf in ('1h','4h','1d'):
  native=json.loads((fp.parent/f'{symbol}-{tf}.json').read_text())
  matched=[r for r in native if r[6] in closes]
  differences=[dict(time=r[6],native=float(r[4]),fine=closes[r[6]]) for r in matched if abs(float(r[4])-closes[r[6]])>max(1e-8,abs(float(r[4]))*1e-9)]
  checks.append(dict(symbol=symbol,tf=tf,matched=len(matched),mismatches=len(differences),examples=differences[:3]))
result=dict(files=len(coverage),errors=[r for r in coverage if r['error'] or r['gaps'] or not r['n']],close_checks=checks,total_close_mismatches=sum(r['mismatches'] for r in checks))
(root/'data-validation.json').write_text(json.dumps(result,indent=2))
print(json.dumps(result,indent=2))
