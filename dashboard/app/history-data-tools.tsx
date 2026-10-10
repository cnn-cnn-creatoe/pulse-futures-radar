'use client';
import { useEffect, useState } from 'react';
type Job = {id: string; state: string; message?: string};

export function HistoryDataTools({ api, online }: { api: string | null; online: boolean }) {
  const [symbol,setSymbol] = useState('BTCUSDT'), [tf,setTf] = useState('5m'), [first,setFirst] = useState(''), [last,setLast] = useState('');
  const [job,setJob] = useState<Job|null>(null), [message,setMessage] = useState(''), [busy,setBusy] = useState(false);
  useEffect(() => {
    if(job?.state !== 'running') return;
    let active = true;
    const timer = setInterval(async () => { try {
      const r=await fetch(api+'/api/jobs/'+job.id,{signal:AbortSignal.timeout(15000)}); if(!r.ok) throw Error('读取下载进度失败');
      const j=await r.json() as Job; if(active) {setJob(j);setMessage(j.state==='completed'?'历史数据已保存，回测会自动读取。':j.message||j.state);}
    } catch(e) {if(active) setMessage((e as Error).message);} },2000);
    return ()=>{active=false;clearInterval(timer);};
  },[api,job?.id,job?.state]);
  async function download() {
    setBusy(true); setMessage('');
    try {
      const months:string[]=[]; let current=first;
      while(current && current<=last && months.length<13) { months.push(current); const [y,m]=current.split('-').map(Number); current=`${m===12?y+1:y}-${String(m===12?1:m+1).padStart(2,'0')}`; }
      if(!months.length || months.length>12) throw Error('请选择 1 至 12 个已结束的月份');
      const r=await fetch(api+'/api/history/download',{method:'POST',headers:{'Content-Type':'application/json','X-Pulse-Client':'dashboard'},body:JSON.stringify({symbol,timeframe:tf,months})});
      const data=await r.json() as Job & {detail?:string}; if(!r.ok) throw Error(data.detail||'下载失败');
      setJob(data); setMessage('下载任务已开始');
    } catch(e) { setMessage((e as Error).message); } finally {setBusy(false);}
  }
  async function upload(file:File) {
    setBusy(true); setMessage('');
    try {
      if(file.size>20000000) throw Error('文件不能超过 20 MB');
      const r=await fetch(`${api}/api/history/import?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}&format=${file.name.endsWith('.json')?'json':'csv'}`,{method:'POST',headers:{'Content-Type':'text/plain','X-Pulse-Client':'dashboard'},body:await file.text()});
      const data=await r.json() as {detail?:string}; if(!r.ok) throw Error(data.detail||'导入失败'); setMessage('导入完成，回测会自动读取该币种的历史数据。');
    }catch(e){setMessage((e as Error).message);}finally{setBusy(false);}
  }
  return <details className="wb-card"><summary>补充历史行情 · 下载 / 导入</summary><p className="wb-note">本地 K 线不足时，下载更早的完整月份，或导入 OHLCV 文件。每日信号 CSV 是记录文件，不能当作连续 K 线导入。</p><fieldset disabled={!online||busy||job?.state==='running'}><div className="wb-fields wb-import"><label>币种<input value={symbol} onChange={e=>setSymbol(e.target.value.toUpperCase())}/></label><label>周期<select value={tf} onChange={e=>setTf(e.target.value)}><option>5m</option><option>15m</option></select></label><label>开始月份<input type="month" value={first} onChange={e=>setFirst(e.target.value)}/></label><label>结束月份<input type="month" value={last} onChange={e=>setLast(e.target.value)}/></label><label>导入 CSV / JSON<input type="file" accept=".csv,.json" onChange={e=>{const f=e.target.files?.[0];if(f) void upload(f);e.target.value='';}}/></label></div><button onClick={()=>void download()}>下载历史 K 线</button></fieldset>{message&&<output>{message}</output>}</details>;
}
