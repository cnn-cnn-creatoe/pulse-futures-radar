'use client';
import {useEffect,useId,useRef,useState} from 'react';
import {Star,X} from 'lucide-react';
type Contract={symbol:string;name:string};
export function SmcContractSearch({api,value,onChange,favorites,onFavorite,counts,monitor=false}:{api:string|null;value:string;onChange:(s:string)=>void;favorites:string[];onFavorite:(s:string)=>void;counts:Record<string,number>;monitor?:boolean}){
 const [contracts,setContracts]=useState<Contract[]>([]),[error,setError]=useState(''),[open,setOpen]=useState(false),[scope,setScope]=useState('favorites');
 const root=useRef<HTMLDivElement>(null),id=useId();
 useEffect(()=>{if(!api)return;const abort=new AbortController();const load=async()=>{try{const r=await fetch(api+'/api/smc/contracts',{signal:abort.signal});if(!r.ok)throw Error('合约清单读取失败');const result=await r.json() as {items:Contract[]};setContracts(result.items);setError('');}catch(e){if(!abort.signal.aborted)setError((e as Error).message);}};void load();const timer=setInterval(()=>void load(),30000);return()=>{abort.abort();clearInterval(timer);};},[api]);
 useEffect(()=>{if(!open)return;const close=(e:PointerEvent)=>{if(!root.current?.contains(e.target as Node))setOpen(false);};document.addEventListener('pointerdown',close);return()=>document.removeEventListener('pointerdown',close);},[open]);
 const query=value.trim().toUpperCase();
 const catalog=[...contracts,...favorites.filter(s=>!contracts.some(c=>c.symbol===s)).map(symbol=>({symbol,name:''}))];
 const matches=catalog.filter(c=>query?(c.symbol+' '+c.name).toUpperCase().includes(query):scope==='all'||favorites.includes(c.symbol)).sort((a,b)=>Number(favorites.includes(b.symbol))-Number(favorites.includes(a.symbol))||a.symbol.localeCompare(b.symbol));
 return <div className="smc-contract-search" ref={root} onBlur={e=>{if(!e.currentTarget.contains(e.relatedTarget as Node|null))setOpen(false);}} onKeyDown={e=>{if(e.key==='Escape'&&open){setOpen(false);e.stopPropagation();}}}>
 <input type="search" aria-label={monitor?'搜索监测美股':'搜索美股记录'} aria-expanded={open} aria-controls={id} aria-haspopup="dialog" placeholder="搜索美股 / 查看自选" value={value} onFocus={()=>{setOpen(true);setScope('favorites');}} onClick={()=>setOpen(true)} onChange={e=>{onChange(e.target.value);setOpen(true);}}/>
 {open&&<div id={id} role="dialog" aria-label="美股自选与合约搜索" className="smc-contract-panel"><div className="smc-contract-tabs"><button aria-pressed={scope==='favorites'} onClick={()=>{setScope('favorites');onChange('');}}>自选（{favorites.length}）</button><button aria-pressed={scope==='all'} onClick={()=>{setScope('all');onChange('');}}>全部美股（{contracts.length}）</button><button aria-label="关闭合约搜索" onClick={()=>setOpen(false)}><X size={16}/></button></div>
 <small>{query?'搜索全部美股，包含未收藏及无信号的合约':'监测列表、当日记录共用同一份收藏'}</small>
 <div className="smc-contract-results">{matches.map(c=><div className="smc-contract-result" key={c.symbol}><button className="smc-favorite" aria-label={`${favorites.includes(c.symbol)?'取消收藏':'收藏'} ${c.symbol}`} aria-pressed={favorites.includes(c.symbol)} onClick={()=>onFavorite(c.symbol)}><Star size={18} fill={favorites.includes(c.symbol)?'currentColor':'none'}/></button><button className="smc-contract-choice" onClick={()=>{onChange(c.symbol);setOpen(false);}}><b>{c.symbol}</b><small>{!contracts.some(item=>item.symbol===c.symbol)?'未在当前合约清单中':monitor?(counts[c.symbol]?'已加入监测':'未加入监测，可在设置中选择'):(counts[c.symbol]?`所选日期 ${counts[c.symbol]} 条记录`:favorites.includes(c.symbol)?'所选日期暂无记录 · 已收藏':'所选日期暂无记录，可直接收藏')}</small></button></div>)}</div>
 {!matches.length&&<p>{error||(!contracts.length?'正在加载美股合约清单…':query?'未找到匹配合约':scope==='favorites'?'暂无收藏，输入代码搜索并点星标添加':'暂无可选合约')}</p>}
 <small>收藏仅置顶，不改变提醒订阅。</small></div>}
 </div>;
}
