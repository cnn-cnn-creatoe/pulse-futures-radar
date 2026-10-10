"""Complete saved score diagnostics without rerunning or refitting the sample."""
import sys,json,argparse,urllib.request
from pathlib import Path
from collections import defaultdict
from statistics import fmean
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'backend'))
from monitor.research import Research
from monitor.policy import Settings
from monitor.indicators import indicator_context,_trade_plan
from monitor.backtest import _execute,_metrics
p=argparse.ArgumentParser();p.add_argument('--dataset',required=True);args=p.parse_args()
out=ROOT/'backend/data/score-validation';report=json.loads((out/f'{args.dataset}.json').read_text('utf8'));rows=json.loads((out/f'{args.dataset}-samples.json').read_text())
snapshot=report.get('policy_snapshot') or json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/settings',timeout=10))
settings=Settings(**{k:v for k,v in snapshot.items() if k in Settings.model_fields});assert settings.fingerprint()==report['policy_hash'],'Settings changed; original snapshot required'
report['policy_snapshot']=settings.model_dump();groups=defaultdict(list)
for r in rows:
 for metric in ('activity','actionability'):
  if metric=='activity' and r['direction']=='down':continue
  v=r[metric];bucket='missing' if v is None else '80–100' if v>=80 else f'{int(v//20)*20}–{int(v//20)*20+19}'
  groups[(r['partition'],metric,bucket)].append(r)
summary=[]
for (partition,metric,bucket),values in sorted(groups.items()):
 summary.append(dict(partition=partition,metric=metric,bucket=bucket,count=len(values),positive_net_fraction=sum(v['net_5bars_pct']>0 for v in values)/len(values) if metric=='actionability' else None,mean_net_pct=fmean(v['net_5bars_pct'] for v in values) if metric=='actionability' else None,mean_range_1h_pct=fmean(v['range_1h_pct'] for v in values),mean_absolute_1h_pct=fmean(v['absolute_1h_pct'] for v in values)))
report['summary']=summary
bars,meta=Research(ROOT/'backend/data/backtest').load(args.dataset);by_time={c.close_time:i for i,c in enumerate(bars)};trades=[];skipped=defaultdict(int)
for r in rows:
 if r['actionability'] is None or r['actionability']<60:continue
 i=by_time[r['time']];ctx=indicator_context(bars[max(0,i-2019):i+1],settings.indicator_config());plan=_trade_plan(ctx,r['direction'],bars[i].close,2*(report['fee_bps_per_side']+report['slippage_bps_per_side']))
 if not plan:raise ValueError('Saved high score no longer has its original plan')
 trade=_execute(bars,dict(index=i,trade_plan=plan,pattern='fixed_hourly_sample',volatility_ratio=None,activity_score=r['activity']),dict(max_holding_bars=5,fee_bps_per_side=report['fee_bps_per_side'],slippage_bps_per_side=report['slippage_bps_per_side']))
 if trade:trades.append({**trade,'partition':r['partition'],'structure_score':r['actionability']})
 else:skipped[r['partition']]+=1
report['structure_plan_check']={part:dict(metrics=_metrics([t for t in trades if t['partition']==part]),invalid_next_open=skipped[part]) for part in ('train','holdout')}
report['structure_plan_check']['protocol']='固定抽样中结构分≥60的全部候选，按原结构止损和目标、最多5根退出；同根同时触及止损和目标按止损优先，下一根开盘越界则排除；历史盘口缺失，属于价格数据诊断。'
report['findings']=['BTC留出段的活跃度分桶越高，未来1小时平均振幅越大；这一样本支持它作为波动排序，不支持把它当买卖胜率。','结构分≥60的留出候选仅11次；样本不足，不能认定高分高胜率或可直接实盘。','不能用一年K线恢复过去未保存的最优买卖价。']
(out/f'{args.dataset}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),'utf8');(out/f'{args.dataset}-structure-trades.json').write_text(json.dumps(trades,ensure_ascii=False,indent=2),'utf8')
text=['# 一年期评分检查','\n'.join(report['findings']),'',report['protocol'],'',f"合约 {meta['symbol']} / {meta['timeframe']}；{meta['bars']:,} 根，无缺口，固定抽样 {report['sample_count']:,} 次。",'', '|分段|评分|分数段|样本|平均扣费收益|未来1小时平均振幅|','|---|---|---|---:|---:|---:|']
for r in summary:text.append('|'+ '|'.join([r['partition'],r['metric'],r['bucket'],str(r['count']),'—' if r['mean_net_pct'] is None else f"{r['mean_net_pct']:.4f}%",f"{r['mean_range_1h_pct']:.4f}%"] )+'|')
text+=['','## 结构方案补充回测',report['structure_plan_check']['protocol'],'','|分段|成交样本|正收益比例|平均扣费收益|开盘越界排除|','|---|---:|---:|---:|---:|']
for part in ('train','holdout'):
 r=report['structure_plan_check'][part];m=r['metrics'];text.append(f"|{part}|{m['trades']}|{m['win_rate']*100:.2f}%|{m['expectancy_net_pct']:.4f}%|{r['invalid_next_open']}|")
text+=['','限制：']+['- '+x for x in report['limitations']]
(out/'一年评分检查.md').write_text('\n'.join(text),'utf8');print(json.dumps(report['structure_plan_check'],ensure_ascii=True))
