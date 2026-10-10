'use client';
import {useEffect,useState} from 'react';
type Check={liquidity_bounds?:number[]|null;status:string;checked_at:number;spread_bps:number|null;liquidity_percentile:number|null;reasons:string[];missing:string[]};
const snapshots=new Map<string,{until:number;promise:Promise<Record<string,Check>>}>();
function checks(api:string){let entry=snapshots.get(api);if(!entry||entry.until<Date.now()){
 const promise=fetch(api+'/api/execution-checks',{signal:AbortSignal.timeout(10000)}).then(async r=>{if(!r.ok)throw Error('当前盘口核验暂不可用');return (await r.json() as {items:Record<string,Check>}).items;});
 entry={until:Date.now()+15000,promise};snapshots.set(api,entry);
}return entry.promise;}
export type Actionability={liquidity_bounds?:number[];score:number|null;label:string;execution_status?:string;execution_reasons?:string[];blockers?:string[];missing?:string[];explanation?:string};
export function ActionabilityCell({value,api,symbol}:{value?:Actionability|null;api?:string|null;symbol?:string}){
 const [current,setCurrent]=useState<Check|null>(null),[checkError,setCheckError]=useState('');
 useEffect(()=>{if(!api||!symbol)return;let live=true;setCurrent(null);setCheckError('');const load=()=>void checks(api).then(rows=>{if(live){setCurrent(rows[symbol]||null);setCheckError(rows[symbol]?'':'当前行情尚未收到，等待实时数据');}}).catch(()=>{if(live){setCurrent(null);setCheckError('当前盘口核验暂不可用，自动重试中');}});load();const timer=setInterval(load,30000);return()=>{live=false;clearInterval(timer);};},[api,symbol]);
 value=value||{score:null,label:'缺少历史K线',missing:['信号当时连续K线']};
 const executionLabel=value.execution_status==='待验证'?(value.liquidity_bounds&&value.liquidity_bounds[0]<.4&&value.liquidity_bounds[1]>=.4?'流动性范围跨门槛':'历史证据不足'):value.execution_status||'历史证据不足';
 const reasons=value.execution_reasons||value.blockers?.filter(r=>/价差|流动性/.test(r))||[];
 return <details className="score-explanation"><summary><b>{value.score==null?'—':value.score+'%'}</b><small> {value.label.replace(' · 盘口待验证','')} · 盘口{executionLabel}</small>{current&&<small style={{display:'block'}} title={'当前核验，不覆盖历史评分 · '+new Date(current.checked_at).toLocaleTimeString()}>当前盘口：{current.status==='待验证'?(current.missing?.[0]||'流动性范围跨门槛'):current.status}</small>}{api&&symbol&&!current&&<small style={{display:'block'}}>当前盘口：{checkError||'正在核验…'}</small>}</summary><div>{current&&<p>当前核验（{new Date(current.checked_at).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})} 北京时间）：价差 {current.spread_bps==null?'缺失':(current.spread_bps/100).toFixed(4)+'%'} · 流动性 {current.liquidity_percentile==null?(current.liquidity_bounds?current.liquidity_bounds.map(v=>Math.round(v*100)+'%').join('～')+'（范围）':'缺失'):Math.round(current.liquidity_percentile*100)+'%'}。{[...current.reasons,...current.missing].join('；')}（此刻数据，不补写旧信号）</p>}{value.liquidity_bounds&&<p>收盘时保存的流动性范围：{value.liquidity_bounds.map(v=>(v*100).toFixed(1)+'%').join('～')}；按范围整体判断40%门槛。</p>}{reasons.map(r=><p key={r}>{r}</p>)}{value.missing?.map(r=><p key={r}>{r.includes('无法确定')?r:<>缺少{r}，{/价差|流动性|盘口|持仓/.test(r)?'无法事后还原；当前情况见实时核验':'可通过所选周期历史补数后重新计算'}</>}</p>)}{value.blockers?.filter(r=>!reasons.includes(r)).map(r=><p key={r}>{r}</p>)}<p>盘口门槛：价差≤0.08%，流动性分位≥40%。历史快照未留存时无法回补，单独展示当前核验；不通过不是封IP提示。</p><p>{value.explanation||'可做度为结构评分，不是胜率。'}</p></div></details>;
}
export function ActivityCell({score,max}:{score?:number|null;max?:number|null}){return <span title="原分 / 当时可用满分；缺失盘口和持仓量不补造">{score==null?'— 数据不足':<><b>{score}</b><small>{max?` / ${max} · ${Math.round(score/max*100)}%`:' · 满分未记录'}</small></>}</span>;}
