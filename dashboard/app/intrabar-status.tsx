'use client';
import {useEffect,useState} from 'react';
import {beijingTime} from './daily-history';
type Result={available:boolean;observed_at?:number;bar_open_time?:number;note:string;signals?:{action:string;price:number}[]};
type Seen={key:string;action:string;price:number;firstSeen:number;bar:number;active:boolean};
export function IntrabarStatus({api,symbol,timeframe,engine,rule}:{api?:string|null;symbol:string;timeframe:string;engine:string;rule:string}){
 const [result,setResult]=useState<Result|null>(null),[error,setError]=useState('');
 const [seen,setSeen]=useState<Seen[]>([]);
 useEffect(()=>setSeen([]),[api,symbol,timeframe,engine,rule]);
 useEffect(()=>{if(!result?.available||!result.observed_at||result.bar_open_time===undefined)return;
   setSeen(previous=>{const active=new Set((result.signals||[]).map(s=>`${result.bar_open_time}:${s.action}`));
     const next=previous.map(s=>({...s,active:active.has(s.key)}));
     for(const s of result.signals||[]){const key=`${result.bar_open_time}:${s.action}`;
       if(!next.some(p=>p.key===key))next.unshift({key,action:s.action,price:s.price,firstSeen:result.observed_at!,bar:result.bar_open_time!,active:true});}
     return next.slice(0,10);});
 },[result]);
 useEffect(()=>{setResult(null);setError('');if(!api||!symbol||!['indicator','quant'].includes(engine)||rule==='combined')return;let busy=false;const abort=new AbortController();const load=async()=>{if(busy||document.hidden)return;busy=true;try{const params=new URLSearchParams({symbol,timeframe,engine,rule});const r=await fetch(api+'/api/workbench/intrabar?'+params,{signal:abort.signal});const d=await r.json() as Result&{detail?:string};if(!r.ok)throw Error(d.detail||'盘中条件暂不可用');if(!abort.signal.aborted){setResult(d);setError('');}}catch(e){if(!abort.signal.aborted){setError((e as Error).message);setResult(null);}}finally{busy=false;}};void load();const timer=setInterval(()=>void load(),2000);return()=>{abort.abort();clearInterval(timer);};},[api,symbol,timeframe,engine,rule]);
 if(!['indicator','quant'].includes(engine)||rule==='combined')return null;
 return <div><div className="signal-tool-row" role="status"><b>{timeframe} 盘中条件</b><span>{error||(!result?'正在读取实时行情…':!result.available?result.note:result.signals?.length?result.signals.map(s=>`${s.action} · ${s.price}`).join('；'):'当前未触发')}</span>{result?.observed_at&&<small>行情时间 {beijingTime(result.observed_at)} · 每2秒检查；未收盘，条件可能撤回。{engine==='quant'?'原策略收盘委托规则不变。':''}</small>}</div>{seen.length>0&&<details><summary>本图打开期间捕捉到 {seen.length} 条盘中条件</summary>{seen.map(s=><div key={s.key}>{beijingTime(s.firstSeen)} · {s.action} · 首次观察价 {s.price} · {s.active?'当前条件仍成立':s.bar===result?.bar_open_time?'盘中条件已撤回':'上一周期观察，收盘结果见图表'}</div>)}<small>仅保留本次图表会话最近10条；关闭图表或切换规则后清空，不是历史确认信号或TG推送记录。</small></details>}</div>;
}
