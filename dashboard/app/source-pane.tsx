'use client';
import {useEffect,useState} from 'react';
type Rule={id:string;name:string;description:string;author?:string;source?:string};
type Source={code:string;filename:string;language:string;note:string};
type Result={key:string;source?:Source;error?:string};
export function SourcePane({api,rules}:{api:string|null;rules:Rule[];draft:boolean;onDraft:(v:boolean)=>void}){
  const [result,setResult]=useState<Result|null>(null);
  const rule=rules[0],ruleId=rule?.id;
  const requestKey=`${api||''}:${ruleId||''}`;
  const source=result?.key===requestKey?result.source:undefined;
  const error=result?.key===requestKey?result.error:undefined;
  useEffect(()=>{
    if(!api||!ruleId)return;
    const controller=new AbortController();
    void fetch(api+'/api/workbench/source?rule_id='+encodeURIComponent(ruleId),{signal:controller.signal}).then(async response=>{
      const data=await response.json() as Source & {detail?:string};
      if(!response.ok)throw Error(data.detail||'源码读取失败');
      if(!controller.signal.aborted)setResult({key:requestKey,source:data});
    }).catch((error:Error)=>{if(!controller.signal.aborted)setResult({key:requestKey,error:error.message});});
    return()=>controller.abort();
  },[api,ruleId,requestKey]);
  return <section className="wb-source-pane"><header>指标策略源码 · {rule?.name||'尚未选择'}</header><p>{rule?.description}</p>{error?<p role="alert">{error}</p>:<pre className="wb-source-code" aria-label="当前规则参考源码">{source?.code||(rule?'正在读取源码…':'请在研究库选择 WaveTrend')}</pre>}<footer>{source?.filename} · {source?.note}</footer></section>;
}
