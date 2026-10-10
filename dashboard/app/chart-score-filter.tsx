'use client';
import {useEffect,useRef,useState} from 'react';
type Mark={time:number;action:string;score_time?:number};
type Score={activity_score?:number|null;actionability?:{score?:number|null}|null};
export function useChartScoreFilter(api:string|null|undefined,symbol:string,timeframe:string,marks:Mark[],runId?:string){
 const [minimum,setMinimum]=useState({action:0,activity:0}),[ready,setReady]=useState(false),[scores,setScores]=useState<Record<string,Score>>({}),[status,setStatus]=useState('');
 const cache=useRef<Record<string,Score>>({});
 const key=(m:Mark)=>`${symbol}:${timeframe}:${m.score_time??m.time}:${m.action}:${runId||''}`;
 useEffect(()=>{try{const d=JSON.parse(localStorage.getItem('pulse-chart-score-filter')||'{}');setMinimum({action:[0,40,60,80,90].includes(d.action)?d.action:0,activity:[0,35,50,65,80,90].includes(d.activity)?d.activity:0});}catch{}setReady(true);},[]);
 useEffect(()=>{if(ready)localStorage.setItem('pulse-chart-score-filter',JSON.stringify(minimum));},[minimum,ready]);
 const list=JSON.stringify(marks.map(m=>[m.time,m.score_time,m.action]));
 useEffect(()=>{if(!api||(!minimum.action&&!minimum.activity)){setStatus('');return;}const abort=new AbortController();let at=0,done=0,failed=0;
  const pending=marks.filter(m=>!m.action.startsWith('close_')&&m.action!=='record'&&!cache.current[key(m)]);
  const worker=async()=>{while(at<pending.length&&!abort.signal.aborted){const m=pending[at++];try{const direction=m.action.startsWith('candidate_')?'record':m.action==='buy'?'long':'short';const params=new URLSearchParams({symbol,timeframe,signal_time:String(m.score_time??m.time),direction,run_id:runId||''});const r=await fetch(api+'/api/workbench/signal-score?'+params,{signal:abort.signal});if(!r.ok)throw Error();cache.current[key(m)]=await r.json();}catch{if(!abort.signal.aborted)failed++;}if(!abort.signal.aborted){done++;setScores({...cache.current});setStatus(`评分核对 ${done}/${pending.length}${failed?'；'+failed+'项读取失败':''}`);}}};
  setScores({...cache.current});setStatus(pending.length?'正在核对信号当时评分…':'评分已核对');void Promise.all([worker(),worker()]);return()=>abort.abort();
 },[api,symbol,timeframe,list,runId,minimum.action>0||minimum.activity>0]);
 const passes=(m:Mark)=>{if(m.action.startsWith('close_'))return true;if(!minimum.action&&!minimum.activity)return true;const s=scores[key(m)];return !!s&&(!minimum.action||typeof s.actionability?.score==='number'&&s.actionability.score>=minimum.action)&&(!minimum.activity||typeof s.activity_score==='number'&&s.activity_score>=minimum.activity);};
 const controls=<div className="signal-tool-row"><label>可做度 <select aria-label="最低可做度" value={minimum.action} onChange={e=>setMinimum(v=>({...v,action:Number(e.target.value)}))}>{[0,40,60,80,90].map(n=><option key={n} value={n}>{n?`≥ ${n} / 100`:'不限'}</option>)}</select></label><label>活跃度 <select aria-label="最低活跃原分" value={minimum.activity} onChange={e=>setMinimum(v=>({...v,activity:Number(e.target.value)}))}>{[0,35,50,65,80,90].map(n=><option key={n} value={n}>{n?`原分 ≥ ${n}`:'不限'}</option>)}</select></label><small>筛选同时满足；缺失评分不通过。原策略平仓保留，不修改交易规则。{status}</small></div>;
 return {passes,controls,scores,minimum};
}
