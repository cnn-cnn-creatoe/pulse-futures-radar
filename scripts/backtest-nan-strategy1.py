"""Reproducible requested run; no order or Telegram API calls."""
import sys,json,time,hashlib,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.workbench import Workbench,WorkbenchRequest
from monitor.policy import Settings,migrate_saved_settings
from monitor.store import Store
from monitor.research import Research
from monitor.custom_indicators import CustomIndicatorRegistry
from monitor.nan_strategy1 import RULE_ID,NAME
from monitor.portfolio import simulate
from copy import deepcopy

out=ROOT/'outputs/nan-strategy1-btc-3y';out.mkdir(parents=True,exist_ok=True)
store=Store(ROOT/'backend/data/pulse.sqlite3')
research=Research(ROOT/'backend/data/backtest')
service=Workbench(store,research,CustomIndicatorRegistry(ROOT/'backend/data/custom-indicators.json'),ROOT/'backend/data/backtest/workbench')
settings=Settings(**migrate_saved_settings(json.loads((ROOT/'backend/data/settings.json').read_text('utf-8'))))
options=WorkbenchRequest(start='2023-09-14T00:00:00+08:00',end='2026-09-13T23:59:59.999+08:00',symbols=['BTCUSDT'],timeframes=['5m'],indicators=[RULE_ID],initial_capital=1000,margin_per_trade=1000,capital_mode='equity',fill_mode='next_open',leverage=3,max_positions=1,fee_bps=5,slippage_bps=3,account_mode=True,rule_settings={RULE_ID:dict(min_activity=0,min_actionability=0)}).model_dump()
ident=uuid.uuid4().hex
job=dict(id=ident,state='running',created_at=int(time.time()*1000),as_of=int(time.time()*1000),processed=0,logs=[],request=options,indicator_names=[NAME],policy_snapshot=settings.model_dump(),custom_indicator_snapshot=[])
job['builtin_code_snapshot']={name:(ROOT/'backend/monitor'/name).read_text('utf-8') for name in ('nan_strategy1.py','community_indicators.py','portfolio.py','workbench.py')}
job['builtin_code_hash']=hashlib.sha256(json.dumps(job['builtin_code_snapshot'],sort_keys=True).encode()).hexdigest()
print('Running 3-year original-rule backtest',flush=True)
service.work(job,settings,[])
research._atomic_write(service.root/f'{ident}.json',json.dumps(job,ensure_ascii=False))
(out/'result.json').write_text(json.dumps(job,ensure_ascii=False),'utf-8')
(out/'trades.csv').write_bytes((service.root/f'{ident}.csv').read_bytes())
(out/'original.pine').write_bytes((ROOT/'backend/sources/tradingview/nan-strategy1.pine').read_bytes())
summary={k:job[k] for k in ('id','request','metrics','coverage','basis')}
summary['portfolio']={k:v for k,v in job['portfolio'].items() if k!='curve'}
(out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),'utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2),flush=True)
# Independent scalar recurrence checks all historical actions, not only fixtures.
from monitor.workbench_data import HistoricalPreparation
from monitor.workbench import timestamp
from monitor.nan_strategy1 import actions,events,outcomes
start=timestamp(options['start']);end=timestamp(options['end'])
bars=HistoricalPreparation(store,research,None).series('BTCUSDT','5m',start-2020*300000,end,research.datasets())
esa=dev=avg=None;wt=[];line=[];pos=0;reference=[]
for i,c in enumerate(bars):
    price=(c.high+c.low+c.close)/3
    esa=price if esa is None else (2/11)*price+(9/11)*esa
    distance=abs(price-esa);dev=distance if dev is None else (2/11)*distance+(9/11)*dev
    ci=(price-esa)/(.015*dev) if dev>0 else None
    if ci is not None:avg=ci if avg is None else (2/22)*ci+(20/22)*avg
    wt.append(avg if ci is not None else None)
    w=wt[-4:];line.append(sum(w)/4 if len(w)==4 and all(v is not None for v in w) else None)
    if i<35 or wt[i-2] is None or line[i-1] is None:continue
    up=wt[i-2]>wt[i-1]<wt[i];down=wt[i-2]<wt[i-1]>wt[i]
    if pos==1 and down:reference.append((i,'close_long'));pos=0
    elif pos==-1 and up:reference.append((i,'close_short'));pos=0
    if not pos:
        if up and wt[i-1]<=-53 and wt[i-1]<line[i-1]:reference.append((i,'long'));pos=1
        elif down and wt[i-1]>=53 and wt[i-1]>line[i-1]:reference.append((i,'short'));pos=-1
actual=actions(bars)
assert actual==reference,'Scalar and vector signal mismatch'
parity=dict(bars=len(bars),events=len(actual),exact_action_match=True,independent_implementation='scalar EMA recurrence and scalar pos state')
(out/'parity.json').write_text(json.dumps(parity,indent=2),'utf-8')
print('PARITY',parity,flush=True)
signals=list(events(bars,start,end));market={('BTCUSDT','5m'):[c for c in bars if c.open_time>=start]}
comparisons=[dict(label='下一根开盘，手续费0.05%+滑点0.03%',portfolio=summary['portfolio'])]
for label,patch in [('下一根开盘，不扣费用',dict(fee_bps=0,slippage_bps=0)),('下一根开盘，仅手续费0.05%',dict(fee_bps=5,slippage_bps=0)),('当根收盘参考价，不扣费用',dict(fee_bps=0,slippage_bps=0,fill_mode='signal_close'))]:
    config={**options,**patch};rows=outcomes(signals,bars,config)
    result=simulate(rows,market,config,start,end)
    comparisons.append(dict(label=label,portfolio={k:v for k,v in result.items() if k!='curve'}))
    print('COMPARISON',label,result['final_equity'],result['closed'],flush=True)
(out/'comparisons.json').write_text(json.dumps(comparisons,ensure_ascii=False,indent=2),'utf-8')
store.close()
