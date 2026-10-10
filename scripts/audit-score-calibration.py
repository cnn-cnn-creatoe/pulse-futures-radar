"""Fixed hourly sampling over a verified year; no parameter fitting or book fabrication."""
import argparse, json, sys, time
from pathlib import Path
from collections import defaultdict
from statistics import fmean
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))
from monitor.research import Research
from monitor.policy import Settings
from monitor.engine import analyze, LOOKBACK, TIMEFRAME_MS
from monitor.indicators import indicator_context, _trade_plan, _directional_position
from monitor.actionability import assess
p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);args=p.parse_args()
data,meta=Research(ROOT/'backend/data/backtest').load(args.dataset)
import urllib.request
snapshot=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/settings',timeout=10))
settings=Settings(**{k:v for k,v in snapshot.items() if k in Settings.model_fields})
out=ROOT/'backend/data/score-validation';out.mkdir(exist_ok=True)
interval=TIMEFRAME_MS[meta['timeframe']];step=max(1,3600000//interval)
window=max(2020,LOOKBACK[meta['timeframe']]['days']*86400000//interval+3)
fee=settings.quality.fee_bps_per_side/10000;slip=settings.quality.slippage_bps_per_side/10000
rows=[];started=time.monotonic()
indices=list(range(window-1,len(data)-13,step))
for n,i in enumerate(indices):
 prefix=data[i-window+1:i+1]
 raw=analyze(prefix,settings.engine(),{},score_only=True).get('signal') or {}
 ctx=indicator_context(prefix[-2020:],settings.indicator_config())
 future=data[i+1:i+13];entry=future[0].open
 partition='train' if i<len(data)*.75 else 'holdout'
 activity=raw.get('activity_score')
 for direction,sign in [('up',1),('down',-1)]:
  plan=_trade_plan(ctx,direction,data[i].close,2*(fee+slip)*10000) if ctx.get('status')=='ready' else None
  pos=_directional_position(ctx.get('smc',{}).get('position_assessment'),direction)
  a=assess({'close_time':data[i].close_time},prefix,ctx,plan,pos)
  buy=entry*(1+sign*slip);exit=future[4].close*(1-sign*slip)
  net=(sign*(exit-buy)/buy-fee*(buy+exit)/buy)*100
  rows.append(dict(time=data[i].close_time,partition=partition,direction=direction,activity=activity,activity_max=raw.get('score_available_max'),actionability=a['score'],execution_status=a['execution_status'],net_5bars_pct=net,range_1h_pct=(max(c.high for c in future)-min(c.low for c in future))/entry*100,absolute_1h_pct=abs(future[-1].close/entry-1)*100))
 if n%250==0:
  progress=dict(processed=n,total=len(indices),elapsed_seconds=round(time.monotonic()-started,1));(out/'progress.json').write_text(json.dumps(progress),'utf8');print(json.dumps(progress),flush=True)
groups=defaultdict(list)
for r in rows:
 for metric in ['activity','actionability']:
  if metric=='activity' and r['direction']=='down':continue
  v=r[metric];bucket='missing' if v is None else ('80–100' if v>=80 else str(int(v//20)*20)+'–'+str(int(v//20)*20+19))
  groups[(r['partition'],metric,bucket)].append(r)
summary=[]
for (partition,metric,bucket),values in sorted(groups.items()):
 summary.append(dict(partition=partition,metric=metric,bucket=bucket,count=len(values),positive_net_fraction=sum(v['net_5bars_pct']>0 for v in values)/len(values) if metric=='actionability' else None,mean_net_pct=fmean(v['net_5bars_pct'] for v in values) if metric=='actionability' else None,mean_range_1h_pct=fmean(v['range_1h_pct'] for v in values),mean_absolute_1h_pct=fmean(v['absolute_1h_pct'] for v in values)))
report=dict(dataset=meta,policy_hash=settings.fingerprint(),policy_snapshot=settings.model_dump(),sample_count=len(indices),protocol='每小时固定抽样，评分只看已收盘历史；前75%训练、后25%留出，不调阈值。活跃度检查未来1小时波动；结构分检查双方向下一根开盘至第5根收盘收益，计双边费用及滑点。固定观察期不是原策略退出规则。',fee_bps_per_side=fee*10000,slippage_bps_per_side=slip*10000,limitations=['单一BTC合约不能代表全部币种或周期','历史盘口与横截面流动性缺失，不能验证当时成交可行性','结构评分双方向样本相关；桶内正收益比例不是经校准的预测胜率','固定观察收益不能代替按结构止盈止损的策略回测'],summary=summary)
(out/f'{args.dataset}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8')
(out/f'{args.dataset}-samples.json').write_text(json.dumps(rows,separators=(',',':')),'utf8')
text=['# 一年期评分检查',report['protocol'],'',f"合约 {meta['symbol']} / {meta['timeframe']}；{meta['bars']:,} 根，数据缺口 {meta['gaps']}，固定抽样 {len(indices):,} 次。",'', '|分段|评分|分数段|样本|平均扣费收益|未来1小时平均振幅|','|---|---|---|---:|---:|---:|']
for r in summary:text.append('|'+ '|'.join([r['partition'],r['metric'],r['bucket'],str(r['count']),'—' if r['mean_net_pct'] is None else f"{r['mean_net_pct']:.4f}%",f"{r['mean_range_1h_pct']:.4f}%"] )+'|')
text+=['','限制：']+['- '+x for x in report['limitations']]
(out/'一年评分检查.md').write_text('\n'.join(text),'utf8');print('COMPLETED',args.dataset,flush=True)
