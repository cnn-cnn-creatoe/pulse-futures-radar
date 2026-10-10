"""Offline threshold study using frozen snapshots; zero network calls."""
import sqlite3,json,statistics,bisect
from pathlib import Path
from collections import Counter,defaultdict
from monitor.actionability import score_projection,refresh_saved

root=Path(__file__).parent
db=sqlite3.connect((root/'data/pulse.sqlite3').resolve().as_uri()+'?mode=ro',uri=True)
fields=['id','symbol','timeframe','close_time','pattern','activity_score','score_available_max','spread_bps','liquidity_percentile','trade_plan','position_assessment']
projection='json_object('+','.join("'"+f+"',json_extract(payload,'$."+f+"')" for f in fields)+')'
sql='select '+score_projection(projection)+' from alerts order by close_time'
rows=[]
for (raw,) in db.execute(sql):
    row=refresh_saved(json.loads(raw)); row['day']=__import__('datetime').datetime.fromtimestamp((row['close_time']+28800000)/1000,__import__('datetime').timezone.utc).strftime('%Y-%m-%d');rows.append(row)
print('Read',len(rows),'snapshots',flush=True)
def pct(s): return s['activity_score']/max(1,s.get('score_available_max') or 100)*100
thresholds=[0,40,60,65,70,80,90]
days=sorted({s['day'] for s in rows})
counts=[]
for day in days:
    sample=[s for s in rows if s['day']==day]
    counts.append(dict(day=day,total=len(sample),activity={str(t):sum(pct(s)>=t for s in sample) for t in thresholds},action={str(t):sum(s['actionability']['score'] is not None and s['actionability']['score']>=t for s in sample) for t in thresholds},missing=sum(s['actionability']['score'] is None for s in sample),execution=Counter(s['actionability']['execution_status'] for s in sample)))
universe={'BTCUSDT','ETHUSDT','SOLUSDT','BNBUSDT','XRPUSDT','ADAUSDT','DOGEUSDT','AVAXUSDT','LINKUSDT','SUIUSDT','LTCUSDT','DOTUSDT'}
sample=[s for s in rows if s['symbol'] in universe and s['timeframe'] in ('5m','15m')]
by_pair=defaultdict(list)
for s in sample:by_pair[s['symbol'],s['timeframe']].append(s)
outcomes=[]
for (symbol,tf),signals in by_pair.items():
    data=db.execute('select open_time,close_time,open,high,low,close from candles where symbol=? and timeframe=? and open_time>=? and open_time<=? and closed=1 order by open_time',(symbol,tf,min(s['close_time'] for s in signals)-900000,max(s['close_time'] for s in signals)+86400000)).fetchall()
    times=[r[0] for r in data]; step=300000 if tf=='5m' else 900000
    for s in signals:
        i=bisect.bisect_left(times,s['close_time']+1)
        for horizon in (5,12):
            seq=data[i:i+horizon]
            if len(seq)!=horizon or seq[0][0]!=s['close_time']+1 or any(b[0]!=a[1]+1 for a,b in zip(seq,seq[1:])):continue
            side=1 if s['pattern'] in ('阳阳','阴阳') else -1
            entry=seq[0][2]*(1+side*.0003);exit=seq[-1][5]*(1-side*.0003)
            net=side*(exit-entry)/entry*100-.1
            stressed_entry=seq[0][2]*(1+side*.0006);stressed_exit=seq[-1][5]*(1-side*.0006)
            stress=side*(stressed_exit-stressed_entry)/stressed_entry*100-.2
            outcomes.append(dict(day=s['day'],horizon=horizon,activity=pct(s),action=s['actionability']['score'],net=net,stress=stress))
summary=[]
for horizon in (5,12):
 for metric in ('activity','action'):
  for threshold in (0,60,70,80,90):
   for phase in ('all','early','late'):
    part=[x for x in outcomes if x['horizon']==horizon and (threshold==0 or x[metric] is not None and x[metric]>=threshold) and (phase=='all' or (x['day']<days[-2] if phase=='early' else x['day']>=days[-2]))]
    if part: summary.append(dict(horizon=horizon,metric=metric,threshold=threshold,phase=phase,n=len(part),win_pct=round(sum(x['net']>0 for x in part)/len(part)*100,2),mean_net_pct=round(statistics.mean(x['net'] for x in part),4),mean_stress_pct=round(statistics.mean(x['stress'] for x in part),4)))
result=dict(version=2,records=len(rows),days=counts,blockers=Counter(b for s in rows for b in s['actionability']['blockers']),test_universe=sorted(universe),outcomes=len(outcomes),summary=summary,limitations=['只包含当时已保存的异常候选，不是完整市场样本','首尾日期可能不完整','结构分由当时快照按新公式重算，未补造盘口','独立信号可能重叠；不是账户收益','下一根开盘，5或12根后收盘退出；单边手续费5bps、滑点3bps，压力测试翻倍','未计资金费率、精度和盘口成交约束；缺连续K线的结果排除','早晚分段仅做稳定性检查，不是充分样本外验证'])
(root/'data/score-study-2026-09-07.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False),flush=True)
