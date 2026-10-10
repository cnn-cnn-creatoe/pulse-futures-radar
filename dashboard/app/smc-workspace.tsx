'use client';
import {useEffect,useLayoutEffect,useRef,useCallback,useState} from 'react';
import {usePreservedScroll} from './use-preserved-scroll';
import {Popover,PopoverTrigger,PopoverContent} from '@/components/ui/popover';
import {Star,ChevronDown} from 'lucide-react';
import {SmcContractSearch} from './smc-contract-search';
import {selectedProfiles,type ProfilePreferences} from './independent-rule-fields';
import './smc-workspace.css';
type Zone={id:string;structure:string;side:string;lower:number;upper:number;origin_at:number;formed_at:number;phase:string;confirmed:boolean};
type Zones={items:Zone[];atr_ready:boolean;history_bars:number;note:string};
type Assessment={ready:boolean;note:string;direction?:string;streak?:number;streak_side?:string;volume_ratio?:number|null;atr_pct?:number|null;range_ratio?:number|null;extension_atr?:number|null;warnings?:string[];asof?:number};
type Level={support:number|null;resistance:number|null;support_broken:boolean;resistance_broken:boolean};
type Row={symbol:string;timeframe:string;note:string;state:string;price?:number;price_time?:number;zones?:Zones;assessment?:Assessment;levels?:Record<string,Level>};
type Event={id:string;symbol:string;timeframe:string;signal_kind:string;entry_direction:string;structure_level:number;signal_price:number;observed_at:number;assessment?:Assessment;event_type?:string;zone?:Zone;zone_context?:Zones;zone_conflict?:string;intrabar?:boolean};
function MultiFilter({title,options,value,onChange}:{title:string;options:{value:string;label:string}[];value:string[];onChange:(v:string[])=>void}){
 return <Popover><PopoverTrigger render={<button type="button" className="smc-assist-trigger">{title}：{value.length?options.filter(o=>value.includes(o.value)).map(o=>o.label).join(' / '):'全部'}<ChevronDown size={15}/></button>}/><PopoverContent align="start" className="smc-assist-menu"><small>{title}可多选；不勾选表示全部</small><button type="button" onClick={()=>onChange([])}>显示全部</button>{options.map(o=><label key={o.value} style={{display:'block'}}><input type="checkbox" checked={value.includes(o.value)} onChange={e=>onChange(e.target.checked?[...value,o.value]:value.filter(v=>v!==o.value))}/>{o.label}</label>)}</PopoverContent></Popover>;
}
const price=(n?:number|null)=>n==null?'尚未形成':n.toLocaleString('en-US',{maximumFractionDigits:8});
const zonePhase:Record<string,string>={formed:'本根新形成',inside:'进入区域，等待反应',rejection:'压力区受阻回落 · 偏空候选',bounce:'支撑区止跌反弹 · 偏多候选',invalidated:'越界失效',waiting:'等待触区'};
function ZoneView({value}:{value?:Zones}){
 if(!value)return <span>未保存区域快照</span>;
 return <div style={{minWidth:240,maxWidth:380,whiteSpace:'normal'}}>{!value.atr_ready&&<small>ATR200过滤预热未完成，区域仅供初步观察</small>}{!value.items.length&&<small>当前历史内暂无订单块</small>}{value.items.map(z=><details key={z.id}><summary style={{color:z.side==='supply'?'#f6b875':'#55d1b4'}}>{z.structure==='internal'?'内部':'波段'}{z.side==='supply'?'供给压力区':'需求支撑区'} {price(z.lower)} — {price(z.upper)}</summary><small>{zonePhase[z.phase]||z.phase} · {z.confirmed?'本根已收盘':'盘中未确认'}</small><small>来源K线：{time(z.origin_at)}<br/>形成于：{time(z.formed_at)}</small></details>)}<small>{value.items.filter(z=>['inside','rejection','bounce','invalidated'].includes(z.phase)).map(z=>`${z.structure==='internal'?'内部':'波段'}：${zonePhase[z.phase]}`).join('；')}</small></div>;
}
function eventDirection(r:Event){
 const wait=(reason:string)=>({label:'等待确认',reason,side:'wait'});
 if(r.zone_conflict)return wait('结构与区域反应冲突');
 if(r.event_type==='zone_invalidated')return wait('原区域已失效');
 if(r.event_type==='zone_inside')return wait(r.zone?.side==='supply'?'触及压力区，等待反应':r.zone?.side==='demand'?'触及支撑区，等待反应':'触及区域，等待反应');
 if(r.event_type?.startsWith('zone_')&&!r.zone_context?.atr_ready)return wait('区域历史尚未预热完成');
 if(r.event_type==='zone_rejection')return {label:'偏空观察',reason:'压力区受阻回落',side:'short'};
 if(r.event_type==='zone_bounce')return {label:'偏多观察',reason:'支撑区止跌反弹',side:'long'};
 if(r.entry_direction==='long')return {label:'向上突破',reason:'突破压力位，留意站稳',side:'long'};
 if(r.entry_direction==='short')return {label:'向下跌破',reason:'跌破支撑位，留意反抽',side:'short'};
 return wait('方向数据不足');
}
function EventTag({event:r}:{event:Event}){
 const label=r.event_type==='zone_inside'?(r.zone?.side==='supply'?'触及压力':r.zone?.side==='demand'?'触及支撑':'触及区域'):r.event_type==='zone_rejection'?'压力受阻':r.event_type==='zone_bounce'?'支撑反弹':r.event_type==='zone_invalidated'?'区域失效':r.entry_direction==='long'?'突破压力':r.entry_direction==='short'?'跌破支撑':'结构信号';
 return <span className="smc-event-tag">{label}</span>;
}
function DirectionView({event}:{event:Event}){
 const d=eventDirection(event);
 return <><strong style={{color:d.side==='long'?'#55d1b4':d.side==='short'?'#f6b875':'#a9b8ca'}}>{d.label}</strong>{event.intrabar&&<small>盘中观察</small>}</>;
}
function BriefEvidence({event:r,show}:{event:Event;show:{candles:boolean;volume:boolean;volatility:boolean}}){
 const a=r.assessment,parts:string[]=[];
 if(a?.ready){
  if(show.volume&&a.volume_ratio!=null)parts.push(`前根成交额 ${a.volume_ratio.toFixed(2)}倍`);
  if(show.candles&&a.streak)parts.push(a.streak===1?`前根${a.streak_side}线`:`连续${a.streak}根${a.streak_side}线`);
  if(show.volatility&&a.range_ratio!=null&&a.range_ratio>2)parts.push(`前根波幅 ${a.range_ratio.toFixed(1)} ATR`);
  if(show.volatility&&a.extension_atr!=null&&a.extension_atr>1)parts.push(`距结构位 ${a.extension_atr.toFixed(1)} ATR`);
 }else parts.push(a?.note||'未保存量价背景');
 const eventText=r.event_type==='zone_invalidated'?(r.zone?.side==='supply'?'压力区上破失效':r.zone?.side==='demand'?'支撑区下破失效':'区域越界失效'):eventDirection(r).reason;
 const descending=eventDirection(r).side==='short';
 return <div className="smc-brief"><EventTag event={r}/><span>{eventText}{r.zone?` · 区域 ${price(descending?r.zone.upper:r.zone.lower)}—${price(descending?r.zone.lower:r.zone.upper)}`:r.structure_level!=null?` · ${price(r.structure_level)}`:''}</span><small>{parts.join(' · ')}</small></div>;
}
function RecordPager({page,pages,total,size,onPage,onSize}:{page:number;pages:number;total:number;size:number;onPage:(p:number)=>void;onSize:(n:number)=>void}){
 return <nav className="smc-toolbar smc-pagination" aria-label="记录分页"><select aria-label="每页记录数" value={size} onChange={e=>onSize(Number(e.target.value))}>{[20,50,100].map(n=><option key={n} value={n}>每页 {n} 条</option>)}</select><button disabled={!page} onClick={()=>onPage(0)}>首页</button><button disabled={!page} onClick={()=>onPage(page-1)}>上一页</button>{Array.from({length:pages},(_,i)=>i).filter(i=>Math.abs(i-page)<=2).map(i=><button key={i} aria-label={`第${i+1}页`} aria-current={i===page?'page':undefined} onClick={()=>onPage(i)}>{i+1}</button>)}<button disabled={page+1>=pages} onClick={()=>onPage(page+1)}>下一页</button><button disabled={page+1>=pages} onClick={()=>onPage(pages-1)}>末页</button><span aria-live="polite">共 {total} 条 · 第 {page+1}/{pages} 页</span></nav>;
}
function ContextView({value,show}:{value?:Assessment;show:{candles:boolean;volume:boolean;volatility:boolean}}){
 if(!value)return <span>未保存量价快照</span>;
 if(!value.ready)return <span>{value.note}</span>;
 const n=(v?:number|null,suffix='')=>v==null?'不足':v.toFixed(2)+suffix;
 return <div style={{minWidth:240,maxWidth:380,whiteSpace:'normal'}}>
 {show.candles&&<small>前序已收盘：{value.streak?`连续 ${value.streak} 根${value.streak_side}线`:'平盘'}</small>}
 {show.volume&&<small>前一根成交额 / 前20根均值：{n(value.volume_ratio,' 倍')}</small>}
 {show.volatility&&<small>ATR14 / 价格：{n(value.atr_pct,'%')} · 前一根波幅：{n(value.range_ratio,' ATR')}{value.extension_atr!=null?` · 离结构位 ${n(value.extension_atr,' ATR')}`:''}</small>}
 {value.warnings?.map(w=><small key={w} style={{color:'#efbd7b'}}>{w}</small>)}
 {!value.warnings?.length&&<small>未触发所列风险条件，仍需确认</small>}
 <small>背景截至 {time(value.asof)}</small></div>;
}
function CopyContract({symbol,favorite,onFavorite}:{symbol:string;favorite:boolean;onFavorite:()=>void}){
 const [message,setMessage]=useState('');
 useEffect(()=>{if(!message)return;const timer=setTimeout(()=>setMessage(''),2000);return()=>clearTimeout(timer);},[message]);
 const copy=async()=>{try{await navigator.clipboard.writeText(symbol);setMessage('已复制');}catch{setMessage('复制失败，请手动复制');}};
 return <><button type="button" className="smc-favorite" aria-label={`${favorite?'取消收藏':'收藏'} ${symbol}`} aria-pressed={favorite} title={favorite?'取消收藏':'收藏置顶'} onClick={onFavorite}><Star size={17} fill={favorite?'currentColor':'none'}/></button><button type="button" className="smc-copy-contract" title="点击复制合约代码" aria-label={`复制 ${symbol}`} onClick={()=>void copy()}>{symbol}</button><small role="status">{message}</small></>;
}
const time=(n?:number)=>n?new Date(n).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'}):'—';
type FrozenRecords={key:string;events:Event[]};
type RecordReturn={frozen:FrozenRecords|null;search:string;period:string[];direction:string[];eventType:string;tab:string;historyDay:string;size:number;sort:string;key:string;page:number;scroll:{node:HTMLElement;top:number;left:number}[];x:number;y:number;focus:HTMLElement|null};
export function SmcWorkspace({api,onWaveTrend,active=true,standalone=false}:{api:string|null;onWaveTrend?:()=>void;active?:boolean;standalone?:boolean}){
 const [frozenRecords,setFrozenRecords]=useState<FrozenRecords|null>(null);
 const [viewPrefsReady,setViewPrefsReady]=useState(false);
 const workspaceRef=usePreservedScroll<HTMLElement>(active);
 const [lastSync,setLastSync]=useState<number|null>(null);
 const [returnStack,setReturnStack]=useState<RecordReturn[]>([]);
 const pendingRestore=useRef<RecordReturn|null>(null);
 useLayoutEffect(()=>{const saved=pendingRestore.current;if(!saved)return;pendingRestore.current=null;saved.focus?.isConnected&&saved.focus.focus({preventScroll:true});for(const p of saved.scroll){p.node.scrollTop=p.top;p.node.scrollLeft=p.left;}window.scrollTo(saved.x,saved.y);});
 const [data,setData]=useState<{items:Row[];recent:Event[];date?:string;days?:string[]}>({items:[],recent:[]}),[prefs,setPrefs]=useState<ProfilePreferences&{signals_enabled?:boolean}>({}),[error,setError]=useState(''),[search,setSearch]=useState(''),[recordSearch,setRecordSearch]=useState(''),[favorites,setFavorites]=useState<string[]>([]),[favoriteError,setFavoriteError]=useState(''),[period,setPeriod]=useState<string[]>([]),[tab,setTab]=useState('events'),[loaded,setLoaded]=useState(false),[historyDay,setHistoryDay]=useState(''),[direction,setDirection]=useState<string[]>([]),[showContext,setShowContext]=useState(false),[showZones,setShowZones]=useState(false),[eventType,setEventType]=useState('all'),[monitorPage,setMonitorPage]=useState(0),[recordPaging,setRecordPaging]=useState({key:"",page:0}),[recordSize,setRecordSize]=useState(20),[recordSort,setRecordSort]=useState('favorites'),[contextFields,setContextFields]=useState({candles:true,volume:true,volatility:true});
 useEffect(()=>{let active=true;queueMicrotask(()=>{if(!active)return;try{const v=JSON.parse(localStorage.getItem('pulse-smc-view-v1')||'{}');if([20,50,100].includes(v.size))setRecordSize(v.size);if(['latest','favorites'].includes(v.sort))setRecordSort(v.sort);if(Array.isArray(v.period))setPeriod(v.period.filter((p:unknown):p is string=>typeof p==='string'&&['1m','5m','15m','30m','1h','4h','1d','1w','1M'].includes(p)));}catch{setFavoriteError('浏览偏好读取失败，本次可正常使用。');}setViewPrefsReady(true);});return()=>{active=false;};},[]);
 useEffect(()=>{if(!viewPrefsReady)return;try{localStorage.setItem('pulse-smc-view-v1',JSON.stringify({size:recordSize,sort:recordSort,period}));}catch{setFavoriteError('浏览偏好暂不能保存，刷新后可能恢复默认。');}},[viewPrefsReady,recordSize,recordSort,period]);
 useEffect(()=>{if(!api||!active)return;const abort=new AbortController();let busy=false;const load=async()=>{if(busy)return;busy=true;try{const responses=await Promise.all(['/api/smc/status'+(tab==='history'&&historyDay?'?day='+historyDay:''),'/api/telegram/settings'].map(p=>fetch(api+p,{signal:abort.signal})));if(responses.some(r=>!r.ok))throw Error('读取美股策略失败，请检查服务连接');const [a,b]=await Promise.all(responses.map(r=>r.json()));setData(a as typeof data);setPrefs((b as {preferences:typeof prefs}).preferences);setLoaded(true);setLastSync(Date.now());setError('');}catch(e){if(!abort.signal.aborted)setError((e as Error).message);}finally{busy=false;}};void load();const timer=setInterval(()=>void load(),5000);return()=>{abort.abort();clearInterval(timer);};},[api,active,tab,historyDay]);
 useEffect(()=>{try{const saved=JSON.parse(localStorage.getItem('pulse-smc-favorites')||'[]');if(Array.isArray(saved))setFavorites(saved.filter((s):s is string=>typeof s==='string'));}catch{setFavoriteError('收藏读取失败，可重新选择。');}
 const sync=(e:StorageEvent)=>{if(e.key==='pulse-smc-favorites'){try{const saved=JSON.parse(e.newValue||'[]');if(Array.isArray(saved))setFavorites(saved.filter((s):s is string=>typeof s==='string'));}catch{}}};window.addEventListener('storage',sync);return()=>window.removeEventListener('storage',sync);},[]);
 const toggleFavorite=(symbol:string)=>{const next=favorites.includes(symbol)?favorites.filter(s=>s!==symbol):[...favorites,symbol];setFavorites(next);try{localStorage.setItem('pulse-smc-favorites',JSON.stringify(next));window.dispatchEvent(new StorageEvent('storage',{key:'pulse-smc-favorites',newValue:JSON.stringify(next)}));setFavoriteError('');}catch{setFavoriteError('收藏仅本次有效，浏览器未允许保存。');}setMonitorPage(0);setRecordPage(0);};
 const favoriteFirst=(a:{symbol:string},b:{symbol:string})=>Number(favorites.includes(b.symbol))-Number(favorites.includes(a.symbol));
 const profile=selectedProfiles(prefs).find(p=>p.rule_id==='smc_equity_structure');
 const symbols=profile?.symbols||[],periods=profile?.timeframes||[];
 const visible=(s:string,t:string)=>s.includes(search.trim().toUpperCase())&&(!period.length||period.includes(t));
 const rows=symbols.flatMap(symbol=>periods.map(timeframe=>data.items.find(r=>r.symbol===symbol&&r.timeframe===timeframe)||{symbol,timeframe,state:'waiting',note:prefs.signals_enabled?'等待启动或核验合约':'信号推送已关闭'})).filter(r=>visible(r.symbol,r.timeframe)).sort(favoriteFirst);
 useEffect(()=>setMonitorPage(0),[search,period,symbols.length]);
 const pageCount=Math.max(1,Math.ceil(rows.length/50));
 const page=Math.min(monitorPage,pageCount-1);
 const visibleRows=rows.slice(page*50,page*50+50);
 const today=new Date().toLocaleDateString('en-CA',{timeZone:'Asia/Shanghai'});
 const requestedDay=tab==='history'?historyDay:today;
 const dayEvents=data.date===requestedDay?data.recent:[];
 const liveEvents=dayEvents.filter(r=>r.symbol.toUpperCase().includes(recordSearch.trim().toUpperCase())&&(!period.length||period.includes(r.timeframe))&&(!direction.length||direction.includes(eventDirection(r).side))&&(eventType==='all'||(eventType==='structure'?!r.event_type||r.event_type==='structure':r.event_type?.startsWith('zone_')))).sort((a,b)=>(recordSort==='favorites'?favoriteFirst(a,b):0)||b.observed_at-a.observed_at||a.id.localeCompare(b.id));
 const changeRecordSearch=(value:string)=>{if(recordSearch.trim()&&!value.trim())setRecordSort('latest');setRecordSearch(value);};
 const recordKey=JSON.stringify([recordSearch,period,direction,eventType,tab,requestedDay,recordSize,recordSort,favorites]);
 const events=frozenRecords?.key===recordKey?frozenRecords.events:liveEvents;
 const frozenIds=new Set(events.map(r=>r.id));
 const pendingRecords=frozenRecords?.key===recordKey?liveEvents.filter(r=>!frozenIds.has(r.id)).length:0;
 const recordPage=recordPaging.key===recordKey?recordPaging.page:0;
 const setRecordPage=(page:number)=>{setFrozenRecords(page>0?{key:recordKey,events}:null);setRecordPaging({key:recordKey,page});};
 const recordPages=Math.max(1,Math.ceil(events.length/recordSize));
 const currentRecordPage=Math.min(recordPage,recordPages-1);
 const visibleEvents=events.slice(currentRecordPage*recordSize,(currentRecordPage+1)*recordSize);
 const dailyCounts=dayEvents.reduce<Record<string,number>>((counts,r)=>{counts[r.symbol]=(counts[r.symbol]||0)+1;return counts;},{});
 const returnToList=useCallback(()=>{const saved=returnStack.at(-1);if(!saved)return;setReturnStack(v=>v.slice(0,-1));setFrozenRecords(saved.frozen);setRecordSearch(saved.search);setPeriod(saved.period);setDirection(saved.direction);setEventType(saved.eventType);setTab(saved.tab);setHistoryDay(saved.historyDay);setRecordSize(saved.size);setRecordSort(saved.sort);setRecordPaging({key:saved.key,page:saved.page});pendingRestore.current=saved;},[returnStack]);
 useEffect(()=>{if(!active||!returnStack.length)return;const back=(e:KeyboardEvent)=>{if(e.key!=='Escape'||e.defaultPrevented||document.querySelector('[role="dialog"]'))return;e.preventDefault();returnToList();};window.addEventListener('keydown',back);return()=>window.removeEventListener('keydown',back);},[active,returnStack.length,returnToList]);
 const showSymbolEvents=(symbol:string)=>{if(recordSearch===symbol&&!period.length&&!direction.length&&eventType==='all')return;const scroll:RecordReturn['scroll']=[];let node:HTMLElement|null=workspaceRef.current;while(node){scroll.push({node,top:node.scrollTop,left:node.scrollLeft});node=node.parentElement;}setReturnStack(v=>[...v,{frozen:frozenRecords,search:recordSearch,period:[...period],direction:[...direction],eventType,tab,historyDay,size:recordSize,sort:recordSort,key:recordKey,page:currentRecordPage,scroll,x:window.scrollX,y:window.scrollY,focus:document.activeElement instanceof HTMLElement?document.activeElement:null}]);setRecordSearch(symbol);setPeriod([]);setDirection([]);setEventType('all');setFrozenRecords(null);setRecordPaging({key:'',page:0});};
 const pager=<RecordPager page={currentRecordPage} pages={recordPages} total={events.length} size={recordSize} onPage={setRecordPage} onSize={setRecordSize}/>;
 const periodOptions=['1m','5m','15m','30m','1h','4h','1d','1w','1M'].filter(p=>periods.includes(p)||data.recent.some(r=>r.timeframe===p)||period.includes(p));
 const openHistory=()=>{setHistoryDay(data.days?.find(d=>d<today)||today);setTab('history');};
 return <section ref={workspaceRef} hidden={!active} className={"smc-workspace"+(standalone?" smc-standalone":"")}><header><div><h1>美股 SMC · 支撑与压力</h1><small role="status">{error?`刷新失败：${error}`:lastSync?`最近同步 ${time(lastSync)} · 每5秒自动刷新`:'正在连接行情…'}</small><p>内部结构 5 · 波段结构 50 · BOS 结构延续 / CHoCH 结构转向</p></div></header>
 <details className="smc-strategy-picker"><summary>✓ 指标策略：美股 SMC · 盘中结构提醒</summary><button aria-pressed="true" onClick={()=>setTab('events')}>✓ 美股 SMC · 当日异常记录</button>{onWaveTrend&&<button onClick={onWaveTrend}>WaveTrend · 超买超卖交叉</button>}</details>
 <div className="smc-summary">已保存 {symbols.length} 个品种 · 周期 {periods.join(' / ')||'未选择'} · 页面每 5 秒更新，提醒随实时行情触发</div>
 {!prefs.signals_enabled&&loaded&&<p role="status">信号推送已关闭，请在“系统与推送”开启。已保存品种仍列在下方。</p>}
 {error&&<p role="alert">{error}；下方如有数据，为上次读取结果。</p>}
 <div className="smc-toolbar">
 {returnStack.length>0&&<button onClick={returnToList}>← 返回上一列表（Esc）</button>}
 <button aria-pressed={tab==='events'} onClick={()=>setTab('events')}>当日异常记录{tab==='events'?`（${events.length}）`:''}</button>
 <button aria-pressed={tab==='monitor'} onClick={()=>setTab('monitor')}>监测列表（{rows.length}）</button>

 <button aria-pressed={tab==='history'} onClick={openHistory}>历史记录</button>
 {tab==='history'&&<><input type="date" aria-label="历史记录日期" max={today} value={historyDay} onChange={e=>{if(e.target.value)setHistoryDay(e.target.value);}}/><select aria-label="已有记录的日期" value={historyDay} onChange={e=>setHistoryDay(e.target.value)}>{!(data.days||[]).includes(historyDay)&&<option value={historyDay}>{historyDay}</option>}{data.days?.map(d=><option key={d} value={d}>{d}</option>)}</select></>}
 <MultiFilter title="周期" options={periodOptions.map(p=>({value:p,label:p}))} value={period} onChange={setPeriod}/>
 {tab!=='monitor'&&<MultiFilter title="方向" options={[{value:'long',label:'向上 / 偏多'},{value:'short',label:'向下 / 偏空'},{value:'wait',label:'等待确认'}]} value={direction} onChange={setDirection}/>}
 {tab!=='monitor'&&<select aria-label="SMC 事件类型" value={eventType} onChange={e=>setEventType(e.target.value)}><option value="all">事件：全部</option><option value="structure">BOS / CHoCH 结构</option><option value="zone">供需区 / 区域反应</option></select>}
 <Popover><PopoverTrigger render={<button type="button" className="smc-assist-trigger">辅助判断<ChevronDown size={15}/></button>}/><PopoverContent align="start" className="smc-assist-menu"><label style={{display:'block'}}><input type="checkbox" checked={showZones} onChange={e=>setShowZones(e.target.checked)}/>显示订单块 / 区域状态</label><label><input type="checkbox" checked={showContext} onChange={e=>setShowContext(e.target.checked)}/>显示量价背景</label>{(['candles','volume','volatility'] as const).map((key,i)=><label key={key} style={{display:'block'}}><input type="checkbox" checked={contextFields[key]} onChange={e=>setContextFields(v=>({...v,[key]:e.target.checked}))}/>{['连续阴阳线','相对成交量','波动率 / 追价距离'][i]}</label>)}<small>只控制数据显示，不改变提醒或自动下单。风险条件为观察提示，未确认盈利优势。</small></PopoverContent></Popover>
 </div>
 <details><summary>回测依据：辅助条件未证实提高胜率</summary><p>178 个品种、174,197 根已收盘 K 线。按时间前70% / 后30%分段，下一根开盘进入，持有3根，假设往返成本0.20%。后段原始信号单笔均值：1h −0.109%（1,073笔），4h −0.053%（1,017笔），1d −0.070%（100笔）。本轮量能、连续阴阳线与波幅条件未显示稳定优势，因此只提供风险提示。补充验证：风险条件汇总在结构信号样本中，1h、4h筛选后均值更差；叠加WT亦未出现跨周期稳定改善。偏多、偏空仅表示观察方向，不代表更高胜率。</p><p>这是已收盘信号代理回测，未还原盘中首次触发，未计资金费；品种相关且样本期较短。支撑反弹的连续阴线过滤在前后样本效果相反，不作为禁止做多的硬条件。</p></details>
 <details><summary>判断方法与限制（展开查看）</summary><p>方向提示只描述结构突破或区域反应，不代表胜率；量价背景不改变方向，也不屏蔽提醒。量价背景仅用触发前已收盘 K 线；ATR 使用 14 根真实波幅均值。支撑反弹与压力反转的补充回测尚未确认稳定优势，方向参考不等于开仓指令。</p>
 <p>结构高低点与供需区分别计算。订单块采用原脚本 ATR200 / 高低价失效规则，内部、波段各显示最近5个有效区域；触区提醒仅观察。已观察到价格进入区域后，回到供给区下方且本根走阴才列受阻候选；回到需求区上方且本根走阳才列反弹候选。盘中反应会变化，不能视为确定买卖信号。</p></details>
 {!loaded&&!error&&<p>正在读取已保存的策略…</p>}
 {loaded&&!symbols.length&&<p>尚未保存监测品种，可在“监测规则”中选择品种和周期。</p>}
 {tab==='monitor'&&loaded&&!rows.length&&<p>没有符合搜索和周期条件的监测项目。</p>}
 {tab!=='monitor'&&data.date!==requestedDay&&<p>正在读取 {requestedDay} 的记录…</p>}
 {tab==='monitor'&&<div className="smc-monitor-search"><SmcContractSearch api={api} monitor value={search} onChange={setSearch} favorites={favorites} onFavorite={toggleFavorite} counts={Object.fromEntries(symbols.map(s=>[s,periods.length]))}/></div>}
 {tab==='monitor'&&<div className="smc-toolbar"><span>共 {rows.length} 项 · 每页50项 · {page+1} / {pageCount}</span><button disabled={page===0} onClick={()=>setMonitorPage(page-1)}>上一页</button><button disabled={page+1>=pageCount} onClick={()=>setMonitorPage(page+1)}>下一页</button></div>}
 {tab!=='monitor'&&<div className="smc-monitor-search"><SmcContractSearch api={api} value={recordSearch} onChange={changeRecordSearch} favorites={favorites} onFavorite={toggleFavorite} counts={dayEvents.reduce<Record<string,number>>((counts,r)=>{counts[r.symbol]=(counts[r.symbol]||0)+1;return counts;},{})}/><select aria-label="记录排序" value={recordSort} onChange={e=>setRecordSort(e.target.value)}><option value="latest">最新优先</option><option value="favorites">★ 收藏优先</option></select><span>收藏与未收藏均可搜索 · 清空搜索恢复最新排序</span></div>}
 {favoriteError&&<p role="status">{favoriteError}</p>}
 {tab!=='monitor'&&pendingRecords>0&&<button className="smc-new-records" onClick={()=>setRecordPage(0)}>新增 {pendingRecords} 条 · 回到最新记录</button>}
 {tab!=='monitor'&&pager}
 <div className="smc-table-wrap">{tab==='monitor'?<table><thead><tr><th>美股合约</th><th>周期</th><th>最近价格 / 时间</th><th>内部支撑位</th><th>内部压力位</th><th>波段支撑位</th><th>波段压力位</th>{showZones&&<th>供需区 / 当前反应</th>}<th>监测状态</th>{showContext&&<th>量价背景 / 风险提示</th>}</tr></thead><tbody>{visibleRows.map(r=><tr key={r.symbol+r.timeframe}><td><CopyContract symbol={r.symbol} favorite={favorites.includes(r.symbol)} onFavorite={()=>toggleFavorite(r.symbol)}/></td><td>{r.timeframe}</td><td>{r.price==null?'—':price(r.price)}<small>{time(r.price_time)}</small><small>{r.state==='live'?'实时行情':'最近已知价格'}</small></td>{['internal','swing'].flatMap(g=>['support','resistance'].map(side=>{const level=r.levels?.[g],broken=side==='support'?level?.support_broken:level?.resistance_broken;return <td key={g+side} className={side==='support'?'smc-support':'smc-resistance'}>{level?price(level[side as 'support'|'resistance']):'等待数据'}<small>{broken?(side==='support'?'已跌破支撑':'已突破压力'):level?.[side as 'support'|'resistance']!=null?'等待突破':level?'等待结构形成':'历史加载 / 未就绪'}</small></td>;}))}{showZones&&<td><ZoneView value={r.zones}/></td>}<td>{r.note}</td>{showContext&&<td><ContextView value={r.assessment} show={contextFields}/></td>}</tr>)}</tbody></table>:<><p>{requestedDay} · 北京时间 · 共 {dayEvents.length} 条已保存记录，{frozenRecords?.key===recordKey?'当前浏览已固定':'筛选后'} {events.length} 条，本页显示 {visibleEvents.length} 条。零点自动切换新一天，往日记录保留在历史中；记录表示已进入推送队列，Telegram 送达结果见“系统与推送”。</p><table><thead><tr><th>触发时间（北京时间）</th><th>美股合约</th><th>周期</th><th>方向判断</th><th>简短说明</th><th>触发价格</th>{showZones&&<th>触发时供需区</th>}{showContext&&<th>触发时背景 / 风险提示</th>}</tr></thead><tbody>{visibleEvents.map(r=><tr key={r.id}><td>{time(r.observed_at)}</td><td><CopyContract symbol={r.symbol} favorite={favorites.includes(r.symbol)} onFavorite={()=>toggleFavorite(r.symbol)}/><button className="smc-daily-count" title="查看此合约所选日期的全部周期和事件" onClick={()=>showSymbolEvents(r.symbol)}>{requestedDay===today?'今日':'当日'} {dailyCounts[r.symbol]||0} 次 · 查看全部</button></td><td>{r.timeframe}</td><td><DirectionView event={r}/></td><td><BriefEvidence event={r} show={contextFields}/></td><td>{price(r.signal_price)}</td>{showZones&&<td><ZoneView value={r.zone_context}/></td>}{showContext&&<td><ContextView value={r.assessment} show={contextFields}/></td>}</tr>)}</tbody></table>{data.date===requestedDay&&!events.length&&<p>当前日期、搜索及筛选条件下暂无记录。</p>}</>}</div>{tab!=='monitor'&&events.length>recordSize&&pager}</section>;
}
