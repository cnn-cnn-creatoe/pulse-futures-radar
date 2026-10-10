'use client';
import {useEffect,useState} from 'react';
import {ActionabilityCell,ActivityCell,type Actionability} from './score-cells';
export function SelectedSignalScores({api,symbol,timeframe,time,direction,runId}:{api?:string|null;symbol:string;timeframe:string;time?:number;direction:string;runId?:string}){
 const [result,setResult]=useState<{actionability?:Actionability;activity_score?:number;score_available_max?:number;score_note?:string}|null>(null),[error,setError]=useState('');
 useEffect(()=>{setResult(null);setError('');if(!api||time==null)return;const abort=new AbortController();
  const params=new URLSearchParams({symbol,timeframe,signal_time:String(time),direction,run_id:runId||''});
  fetch(api+'/api/workbench/signal-score?'+params,{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error('评分暂不可用');return r.json() as Promise<NonNullable<typeof result>>;}).then(r=>{if(!abort.signal.aborted)setResult(r);}).catch(e=>{if(!abort.signal.aborted)setError(e.message);});
  return()=>abort.abort();
 },[api,symbol,timeframe,time,direction,runId]);
 return <div className="selected-signal-scores" aria-live="polite">{time==null?'点击箭头查看该信号评分':error||(!result?'读取所选信号评分…':<><div>可做度 <ActionabilityCell value={result.actionability} api={api} symbol={symbol}/></div><div>活跃度 <ActivityCell score={result.activity_score} max={result.score_available_max}/></div><small>{result.score_note}</small></>)}</div>;
}
