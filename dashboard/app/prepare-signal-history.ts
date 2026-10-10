// A selected chart uses the same cancellable, shared-budget preparation as a scan.
export async function prepareSignalHistory(api:string,options:Record<string,unknown>,signal:AbortSignal,onProgress:(text:string)=>void){
 let id='';
 const request=async(path:string,body?:unknown)=>{
  const response=await fetch(api+path,{signal:body?AbortSignal.timeout(45000):AbortSignal.any([signal,AbortSignal.timeout(20000)]),...(body?{method:'POST',headers:{'Content-Type':'application/json','X-Pulse-Client':'dashboard'},body:JSON.stringify(body)}:{})});
  const data=await response.json() as {id:string;state:string;message?:string;detail?:string;items?:unknown[];preparation?:{current?:string;downloaded?:number;checked?:number;total?:number}};
  if(!response.ok)throw Error(data.detail||'历史数据准备失败');return data;
 };
 const pause=()=>new Promise<void>((resolve,reject)=>{const done=()=>{signal.removeEventListener('abort',abort);resolve();},timer=setTimeout(done,2000),abort=()=>{clearTimeout(timer);signal.removeEventListener('abort',abort);reject(new DOMException('已取消','AbortError'));};signal.addEventListener('abort',abort,{once:true});if(signal.aborted)abort();});
 try{
  onProgress('正在检查所选周期历史…');
  while((await request('/api/workbench/active-tasks')).items?.length){onProgress('等待当前数据任务完成…');await pause();}
  signal.throwIfAborted();
  const job=await request('/api/workbench/runs',{...options,purpose:'signals',auto_download:true,account_mode:false});id=job.id;
  while(true){
   signal.throwIfAborted();const state=await request('/api/workbench/runs/'+id+'?limit=1');
   if(state.state==='completed'){id='';return;}
   if(state.state==='failed'||state.state==='cancelled')throw Error(state.message||'数据准备已停止');
   const p=state.preparation;onProgress(p?`补齐历史 ${p.current||''} · 已保存 ${p.downloaded||0} 根 · ${p.checked||0}/${p.total||0}`:'正在准备历史数据…');await pause();
  }
 }finally{
  if(id)await fetch(api+'/api/workbench/runs/'+id+'/stop',{method:'POST',headers:{'Content-Type':'application/json','X-Pulse-Client':'dashboard'},body:'{}',signal:AbortSignal.timeout(10000)}).catch(()=>{});
 }
}
