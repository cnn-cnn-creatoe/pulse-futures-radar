'use client';
import {useEffect,useState} from 'react';
export function SmcFields({api,symbols,onChange}:{api:string|null;symbols:string[];onChange:(v:string[])=>void}){
 const [items,setItems]=useState<{symbol:string;name:string}[]>([]),[search,setSearch]=useState(''),[error,setError]=useState('');
 useEffect(()=>{if(!api)return;const abort=new AbortController();fetch(api+'/api/smc/contracts',{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error('美股清单读取失败');return await r.json() as {items:{symbol:string;name:string}[]};}).then(r=>setItems(r.items)).catch(e=>{if(!abort.signal.aborted)setError(e.message);});return()=>abort.abort();},[api]);
 const matches=items.filter(i=>(i.symbol+' '+i.name).includes(search.trim()));
 return <div><p>内部结构 5 / 波段结构 50：BOS、CHoCH，以及订单块触区、区域反应和失效提醒。实时行情到达就计算，不等收盘；此处状态每 5 秒刷新。</p><p>只接收币安股票 / ETF 分类合约。支持全选；空名单暂停监测。批量启用会逐步加载历史，状态就绪后开始提醒。不要求两根异常、形态或评分门槛。</p>
 <input aria-label="搜索美股合约" placeholder="搜索美股代码" value={search} onChange={e=>setSearch(e.target.value.toUpperCase())}/>
 <div className="rule-delivery-actions" style={{marginTop:10,marginBottom:10}}>
 <button type="button" disabled={!items.length} onClick={()=>onChange([...new Set([...symbols,...items.map(i=>i.symbol)])])}>全选美股（{items.length}）</button>
 <button type="button" disabled={!matches.length} onClick={()=>onChange([...new Set([...symbols,...matches.map(i=>i.symbol)])])}>全选搜索结果（{matches.length}）</button>
 <button type="button" disabled={!symbols.length} onClick={()=>onChange([])}>清空</button>
 <span role="status">已选 {symbols.length} 个 / 可选 {items.length} 个，保存后生效</span>
 </div>
 <details><summary>查看已选品种（{symbols.length}）</summary><p style={{maxHeight:100,overflowY:'auto',overflowWrap:'anywhere'}}>{symbols.length?symbols.join('、'):'未选择'}</p></details>
 {symbols.filter(s=>!items.some(i=>i.symbol===s)).map(s=><button type="button" key={s} onClick={()=>onChange(symbols.filter(x=>x!==s))}>{s}（未核验，点击移除）</button>)}
 <div style={{maxHeight:180,overflowY:'auto',display:'flex',flexWrap:'wrap',gap:12}}>{matches.map(i=><label key={i.symbol}><input type="checkbox" checked={symbols.includes(i.symbol)} onChange={()=>onChange(symbols.includes(i.symbol)?symbols.filter(s=>s!==i.symbol):[...symbols,i.symbol])}/>{i.symbol}</label>)}</div>
 {!items.length&&<p>等待币安合约清单；不会用普通加密币代替美股。</p>}{error&&<p role="alert">{error}</p>}
 <small>结构计算改编自 © LuxAlgo，CC BY-NC-SA 4.0。结构信号按原脚本严格突破触发；订单块边界相等视为触区，不视为失效；盘中回撤不会撤销已发提醒。</small></div>;
}
