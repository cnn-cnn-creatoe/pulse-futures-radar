'use client';
import {useEffect,useState} from 'react';
type Review={available:boolean;dataset?:{symbol:string;timeframe:string;bars:number;gaps:number};sample_count?:number;protocol?:string;findings?:string[];summary?:{partition:string;metric:string;bucket:string;count:number;mean_range_1h_pct:number}[];structure_plan_check?:{holdout?:{metrics:{trades:number;win_rate:number|null;expectancy_net_pct:number|null}}};limitations?:string[]};
export function ScoreValidation({api}:{api:string|null}){
 const [data,setData]=useState<Review|null>(null),[error,setError]=useState('');
 useEffect(()=>{if(!api)return;const abort=new AbortController();fetch(api+'/api/workbench/score-validation',{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error('评分检查报告暂不可用');return await r.json() as Review;}).then(setData).catch(e=>{if(!abort.signal.aborted)setError(e.message);});return()=>abort.abort();},[api]);
 if(error)return <p>{error}</p>;
 if(!data?.available)return null;
 const held=data.structure_plan_check?.holdout?.metrics;
 return <section><h3>一年期评分检查 · {data.dataset?.symbol} / {data.dataset?.timeframe}</h3><p>{data.dataset?.bars.toLocaleString()} 根已校验 K 线，缺口 {data.dataset?.gaps}；固定抽样 {data.sample_count?.toLocaleString()} 次。活跃度仅验证 K 线可得部分，不含缺失的历史盘口和持仓量。</p>{data.findings?.map(t=><p key={t}>{t}</p>)}<div className="wb-table-wrap"><table><thead><tr><th>活跃原分（留出段）</th><th>样本</th><th>未来1小时平均振幅</th></tr></thead><tbody>{data.summary?.filter(r=>r.partition==='holdout'&&r.metric==='activity').map(r=><tr key={r.bucket}><td>{r.bucket}</td><td>{r.count}</td><td>{r.mean_range_1h_pct.toFixed(3)}%</td></tr>)}</tbody></table></div>{held&&<p>结构分 ≥60 的留出方案回测：{held.trades} 笔，正收益比例 {held.win_rate==null?'—':(held.win_rate*100).toFixed(1)+'%'}，平均扣费收益 {held.expectancy_net_pct?.toFixed(3)}%。样本不足，尚未通过盈利验证。</p>}<details><summary>检查方法与限制</summary><p>{data.protocol}</p>{data.limitations?.map(t=><p key={t}>{t}</p>)}</details></section>;
}
