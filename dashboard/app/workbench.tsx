'use client';
import {IntrabarStatus} from './intrabar-status';
import {ScoreValidation} from './score-validation';
import {ChartChoice} from './chart-choice';
import {ActionabilityCell,ActivityCell,type Actionability} from './score-cells';
import {PatternPicker} from './pattern-picker';
import {RuleSettingsButton,useRuleSettings,type RuleSettings} from './rule-settings';
import {INDICATOR_SHORTLIST} from './indicator-shortlist';
import {TimeframePicker} from './timeframe-picker';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { beijingDay, beijingTime } from './daily-history';
import './workbench.css';
import { HistoryDataTools } from './history-data-tools';
import {PersonalRuleSettings} from './rule-delivery';
import { ContractPicker, type Contract } from './contract-picker';
import {EquityPanel,Splitter,type Portfolio} from './research-panels';
import {ScriptLibrary} from './script-library';
import {ActiveResearch} from './active-research';
import {SignalChart} from './signal-chart';
import {prepareSignalHistory} from './prepare-signal-history';
import {SourcePane} from './source-pane';
import './research-studio.css';

type Indicator = { id: string; name: string; kind: string; description: string; category?: string; author?: string; source?: string; status?: string; long_expression?: string; short_expression?: string };
type Strategy = {id:string; name:string; category:string; description:string; author:string; source:string};
type Options = { capital_mode?:'fixed'|'equity';fill_mode?:'next_open'|'signal_close'; rule_settings?:RuleSettings; auto_download?:boolean; account_mode?:boolean; initial_capital?:number; margin_per_trade?:number; leverage?:number; max_positions?:number; maintenance_margin_pct?:number; start: string; end: string; scope: string; symbols: string[]; timeframes: string[]; engine: string; strategy?: string | null; indicators: string[]; combination: string; pattern: string; patterns?:string[]; holding_bars: number; fee_bps: number; slippage_bps: number; purpose: string };
type Row = {actionability?:Actionability;activity_score?:number|null;score_available_max?:number|null; account_status?:string; quantity?:number; margin?:number; account_pnl?:number|null; account_unrealized?:number; account_entry_price?:number; account_exit_price?:number; account_entry_time?:number|null; account_exit_time?:number|null; id: string; row_index: number; symbol: string; timeframe: string; signal_time: number; signal_price: number; direction: string; signal_kind: string; pattern: string; entry_time: number | null; entry_price: number | null; exit_time: number | null; exit_price: number | null; status: string; net_return_pct: number | null; best_move_pct: number | null; worst_move_pct: number | null; indicator_results: Record<string, boolean | null> };
type Coverage = { symbol: string; timeframe: string; bars: number; expected: number; missing: number; internal_gaps: number; warmup: number; first: number | null; last: number | null };
type Run = { portfolio?:Portfolio; preparation?: { stage: string; current: string; checked: number; total: number; downloaded: number }; id: string; state: string; request: Options; created_at: number; completed_at?: number; processed: number; total: number; current?: string; message?: string; indicator_names: string[]; logs?: string[]; evidence?: string[]; basis?: string; coverage?: Coverage[]; missing_symbols?: string[]; items?: Row[]; total_items?: number; metrics?: { selected: number; completed: number; pending: number; win_rate: number | null; average_return_pct: number | null; profit_factor: number | null; wins: number; losses: number; bars: number; missing_bars: number } };
type Bar = { open_time: number; close_time: number; open: number; high: number; low: number; close: number };
type ChartData = { row: Row; candles: Bar[]; note: string; markers: {time:number;price:number;action:string;label:string;selected:boolean}[]; live:boolean; start:number; end:number };
type Archive = { date: string; records: number; symbols: number; filename: string; ready: boolean };
const number = (v?: number | null, suffix = '') => v == null ? '—' : `${v.toLocaleString('zh-CN', { maximumFractionDigits: 3 })}${suffix}`;
const price = (v?: number | null) => v == null ? '—' : String(Number(v.toPrecision(8)));
const defaults = (signals: boolean): Options => ({ capital_mode:'fixed',fill_mode:'next_open', rule_settings:{wt_extreme_cross:{min_activity:0,min_actionability:0}},account_mode:!signals,initial_capital:10000,margin_per_trade:1000,leverage:1,max_positions:5,maintenance_margin_pct:0.5,start: beijingDay() + 'T00:00', end: beijingDay() + 'T23:59:59', scope: 'single', symbols: ['BTCUSDT'], timeframes: ['5m'], engine: 'indicator', indicators: [INDICATOR_SHORTLIST[0].id], combination: 'all', pattern: 'none', patterns:[], holding_bars: 5, fee_bps: 5, slippage_bps: 3, purpose: signals ? 'signals' : 'backtest' });

async function request<T>(api: string | null, path: string, body?: unknown): Promise<T> {
  if (!api) throw Error('本地服务尚未连接');
  const response = await fetch(api + path, { signal: AbortSignal.timeout(45000), ...(body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pulse-Client': 'dashboard' }, body: JSON.stringify(body) }) });
  const data = await response.json() as { detail?: string | { msg: string }[] };
  if (!response.ok) throw Error(typeof data.detail === 'string' ? data.detail : Array.isArray(data.detail) ? data.detail.map((d: { msg: string }) => d.msg).join('；') : '请求失败，请稍后重试');
  return data as T;
}

export function QuantStrategyPanel({api,online}:{api:string|null;online:boolean}) {
  return <ResearchWorkbench api={api} online={online} signals/>;
}

export function ResearchWorkbench({ api, online, signals = false, quant = false }: { api: string | null; online: boolean; signals?: boolean; quant?: boolean }) {
  const [baseOptions, setOptions] = useState<Options>(() => ({...defaults(signals), engine:"indicator"})), [symbolText, setSymbolText] = useState('BTCUSDT');
  const [ruleSettings,updateRuleSettings]=useRuleSettings();
  const options=useMemo(()=>({...baseOptions,rule_settings:Object.fromEntries(baseOptions.indicators.map(id=>[id,ruleSettings[id]||{min_activity:0,min_actionability:0}]))}),[baseOptions,ruleSettings]);
  const [strategies, setStrategies] = useState<Strategy[]>([]);
  const [catalog, setCatalog] = useState<Indicator[]>([]), [archives, setArchives] = useState<Archive[]>([]), [history, setHistory] = useState<Run[]>([]);
  const [job, setJob] = useState<Run | null>(null), [error, setError] = useState(''), [posting, setPosting] = useState(false), [page, setPage] = useState(0);
  const [tab, setTab] = useState('trades'), [chart, setChart] = useState<ChartData | null>(null), [chartLoading, setChartLoading] = useState(false), [symbolHistory, setSymbolHistory] = useState('');
  const [chartEndpoint,setChartEndpoint]=useState('');
  const chartPreparation=useRef<AbortController|null>(null);const [chartProgress,setChartProgress]=useState('');
  useEffect(()=>()=>chartPreparation.current?.abort(),[]);
  const [chartSelection,setChartSelection]=useState<{runId:string;row:number;rule:string;period:string;engine:string;names:string;originalRule:string}|null>(null);
  const [sortOrder,setSortOrder] = useState('desc');
  const [pausedSignature,setPausedSignature]=useState('');
  const ownedJob=useRef('');
  const [coveragePage, setCoveragePage] = useState(0), [contracts, setContracts] = useState<Contract[]>([]);
  const [submittedSignature, setSubmittedSignature] = useState('');
  const submitted = useRef(''), stopping = useRef('');
  const normalizedSymbols = [...new Set(symbolText.trim().split(/[\s,，;；]+/).filter(Boolean))];
  const signature = JSON.stringify({...options,symbols: options.scope === 'all' ? [] : normalizedSymbols});
  const resultChanged = !signals && job?.state==='completed' && Object.keys(defaults(false)).filter(k=>k!=='purpose').some(k=>{
    const field=k as keyof Options;
    const selected=field==='symbols'?(options.scope==='all'?[]:normalizedSymbols):options[field];
    const used=field==='symbols'&&job.request.scope==='all'?[]:job.request[field];
    return JSON.stringify(selected??defaults(false)[field])!==JSON.stringify(used??defaults(false)[field]);
  });
  const valid = (options.engine === 'indicator' || strategies.some(s => s.id === options.strategy)) && options.indicators.length > 0 && options.timeframes.length > 0 && !!options.start && options.end > options.start && (options.scope === 'all' || ((options.scope === 'single' ? normalizedSymbols.length === 1 : normalizedSymbols.length >= 2) && normalizedSymbols.every(s => contracts.some(c => c.symbol === s) || /USDT(?:_\d{6})?$/.test(s))));
  const mounted = useRef(true), chartSequence = useRef(0), todayRef = useRef(beijingDay());
  const key = signals ? 'pulse-indicator-strategy-v1' : 'pulse-workbench-v3';
  const busy = posting || job?.state === 'running';
  useEffect(()=>{if(!signals||quant||!catalog.length)return;setOptions(old=>{const ids=old.indicators.filter(id=>catalog.some(c=>c.id===id&&c.kind==='event'));return ids.length===old.indicators.length?old:{...old,indicators:ids.length?ids:[INDICATOR_SHORTLIST[0].id]};});},[signals,quant,catalog.length]);
  const change = <K extends keyof Options>(name: K, value: Options[K]) => setOptions(old => ({ ...old, [name]: value }));
  const reload = useCallback(async () => {
    if (!online || !api) return;
    await Promise.all([
      request<{items: Contract[]}>(api,'/api/workbench/contracts').then(u=>{if(mounted.current)setContracts(u.items);}),
      request<{indicators:Indicator[];strategies:Strategy[]}>(api,'/api/workbench/catalog').then(c=>{if(mounted.current){setCatalog(c.indicators);setStrategies(c.strategies||[]);}}),
    ]);
  },[api,online]);
  useEffect(()=>{
    if(tab!=='saved'||!online||!api)return;
    let active=true;
    void Promise.all([
      request<{items:Archive[];archive_error?:string}>(api,'/api/daily-archives').then(a=>{if(active){setArchives(a.items);if(a.archive_error)setError('每日 CSV 更新失败：'+a.archive_error);}}),
      request<{items:Run[]}>(api,'/api/workbench/runs').then(h=>{if(active)setHistory(h.items);}),
    ]).catch((e:Error)=>{if(active)setError(e.message);});
    return()=>{active=false;};
  },[tab,online,api,job?.state]);
  useEffect(() => { mounted.current = true; const timer = setTimeout(() => { reload().catch(e => setError(e.message)); const id = localStorage.getItem(key); if(id && !signals) request<Run>(api,'/api/workbench/runs/'+id).then(j => { if(mounted.current && !signals && j.request.engine==='indicator' && j.request.indicators.every(id=>INDICATOR_SHORTLIST.some(r=>r.id===id))) { setJob(j); setOptions(j.request); setSymbolText(j.request.symbols.join(' ')); } }).catch(() => {}); },0); return () => { mounted.current = false; clearTimeout(timer); }; },[api,key,reload,signals]);
  useEffect(() => {
    if(!job?.id || !api) return;
    let active = true;
    const load = async () => { try { const j = await request<Run>(api,`/api/workbench/runs/${job.id}?offset=${page*50}&limit=50&symbol=${encodeURIComponent(symbolHistory)}&order=${sortOrder}`); if(active) setJob(j); } catch(e) { if(active) setError((e as Error).message); } };
    void load(); const timer = job.state === 'running' ? setInterval(load,2000) : undefined;
    return () => { active = false; clearInterval(timer); };
  },[api,job?.id,job?.state,page,symbolHistory,sortOrder]);
  useEffect(() => { if(job?.state !== 'completed') return; const timer = setTimeout(() => { void reload().catch(e => setError(e.message)); }, 0); return () => clearTimeout(timer); },[job?.id,job?.state,reload]);
  const start = useCallback(async () => {
    if(submitted.current !== signature) {setChart(null); chartSequence.current++;}
    submitted.current = signature; setSubmittedSignature(signature); stopping.current = ''; setPosting(true); setError(''); setPage(0); setSymbolHistory('');
    try {
      const current = { ...defaults(signals), ...options, account_mode:!signals, symbols: symbolText.split(/[\s,，;；]+/).filter(Boolean), purpose: signals ? 'signals' : 'backtest',auto_download:signals };
      const j = await request<Run>(api,'/api/workbench/runs',current);
      if(mounted.current) { ownedJob.current=j.id;setJob(j); localStorage.setItem(key,j.id); }
      else if(signals) { void request(api,`/api/workbench/runs/${j.id}/stop`,{}).catch(()=>{}); }
    } catch(e) { if(mounted.current) { setError((e as Error).message); } } finally { if(mounted.current) setPosting(false); }
  },[api,options,symbolText,signals,key,signature]);
  useEffect(() => {
    if(!signals || !online || !catalog.length || pausedSignature===signature || chartLoading) return;
    if(busy) {
      if(job?.state === 'running' && submitted.current !== signature && stopping.current !== job.id) {
        stopping.current = job.id;
        void request(api,`/api/workbench/runs/${job.id}/stop`,{}).catch(e => setError(e.message));
      }
      return;
    }
    if(!valid) return;
    const changed = submitted.current !== signature;
    if(!changed && (job?.state !== 'completed' || options.end.slice(0,10)!==beijingDay())) return;
    const timer = setTimeout(() => void start(),changed ? 550 : 60000);
    return () => clearTimeout(timer);
  },[signals,online,catalog.length,busy,job?.id,job?.state,signature,valid,api,start,options.end,pausedSignature,error,chartLoading]);
  useEffect(()=>()=>{if(signals&&ownedJob.current)void request(api,`/api/workbench/runs/${ownedJob.current}/stop`,{}).catch(()=>{});},[api,signals]);
  useEffect(()=>{if(signals&&!valid&&!posting){setJob(null);setChart(null);setError('');}},[signals,valid,posting]);
  const stopped=useCallback(()=>{setPausedSignature(signature);setError('');},[signature]);
  const idle=useCallback(()=>{if(error.includes('已有指标研究')||error.includes('正在停止')){submitted.current='';setError('');}},[error]);
  async function viewChart(row: Row) {
    if(!job) return;
    const sequence = ++chartSequence.current; setChartLoading(true); setError('');
    try { const endpoint=`/api/workbench/runs/${job.id}/chart?row=${row.row_index}${signals?'&refresh=true':''}`; const next = await request<ChartData>(api,endpoint); if(sequence === chartSequence.current) {setChart(next);setChartEndpoint(signals&&api?api+endpoint:'');const originalRule=job.request.engine==='quant'?(job.request.strategy||''):job.request.indicators.length===1?job.request.indicators[0]:'combined';setChartSelection({runId:job.id,row:row.row_index,rule:originalRule,originalRule,period:row.timeframe,engine:job.request.engine,names:job.indicator_names.join(' + ')});} }
    catch(e) { setError((e as Error).message); } finally { if(sequence === chartSequence.current) setChartLoading(false); }
  }
  async function switchChart(rule:string,period:string){
    if(!chartSelection||!api||!chart||!job)return;
    chartPreparation.current?.abort();const abort=new AbortController();chartPreparation.current=abort;
    const sequence=++chartSequence.current;setChartLoading(true);setError('');
    const endpoint=`/api/workbench/runs/${chartSelection.runId}/chart?row=${chartSelection.row}&refresh=true&timeframe=${encodeURIComponent(period)}${rule==='combined'?'':'&rule='+encodeURIComponent(rule)}`;
    try{await prepareSignalHistory(api,{...job.request,scope:'single',symbols:[chart.row.symbol],timeframes:[period],engine:chartSelection.engine,...(chartSelection.engine==='quant'?{strategy:rule}:rule==='combined'?{}:{indicators:[rule],combination:'all'})},abort.signal,setChartProgress);const next=await request<ChartData>(api,endpoint);if(sequence===chartSequence.current){setChart(next);setChartEndpoint(api+endpoint);setChartSelection({...chartSelection,rule,period});}}
    catch(e){if(sequence===chartSequence.current)setError((e as Error).message);}finally{if(sequence===chartSequence.current){setChartLoading(false);setChartProgress('');}}
  }
  const chartRuleControls=signals&&chartSelection?<div className="signal-tool-row"><ChartChoice label={chartSelection.engine==='quant'?'策略选择':'信号选择'} disabled={chartLoading} value={chartSelection.rule} onChange={v=>void switchChart(v,chartSelection.period)} options={[...(chartSelection.originalRule==='combined'?[{id:'combined',name:chartSelection.names}]:[]),...(chartSelection.engine==='quant'?strategies:catalog.filter(c=>c.kind==='event'))]}/><ChartChoice label="周期选择" disabled={chartLoading} value={chartSelection.period} onChange={v=>void switchChart(chartSelection.rule,v)} options={['1m','5m','15m','30m','1h','4h','1d','1w','1M'].map(tf=>({id:tf,name:tf}))}/><IntrabarStatus api={api} symbol={chart?.row.symbol||''} timeframe={chartSelection.period} engine={chartSelection.engine} rule={chartSelection.rule}/>{chartLoading&&<span>{chartProgress||'正在读取本地数据…'}</span>}{error&&<span role="alert">{error}</span>}</div>:undefined;
  useEffect(() => {
    if (!signals) return;
    const timer = setInterval(() => {
      const day = beijingDay();
      if (day !== todayRef.current && !busy && options.start.slice(0,10) === todayRef.current && options.end.slice(0,10) === todayRef.current) {
        todayRef.current = day;
        setOptions(o => ({...o,start:day+'T00:00',end:day+'T23:59:59'}));
        if (!busy) { setJob(null); setChart(null); setPage(0); setSymbolHistory(''); localStorage.removeItem(key); }
      }
    },1000);
    return () => clearInterval(timer);
  },[signals,busy,key,options.start,options.end]);
  const metrics = job?.metrics;
  const [libraryHidden,setLibraryHidden]=useState(false),[sourceHidden,setSourceHidden]=useState(false),[resultsHidden,setResultsHidden]=useState(false);
  const [menuSearch,setMenuSearch] = useState('');
  const [paneWidth,setPaneWidth] = useState(280),[editorWidth,setEditorWidth]=useState(460),[equityHeight,setEquityHeight]=useState(510),[draft,setDraft]=useState(false);
  useEffect(()=>{try{const p=JSON.parse(localStorage.getItem('pulse-research-layout-v2')||'{}');if(Number.isFinite(p.width))setPaneWidth(Math.max(260,Math.min(400,p.width)));if(Number.isFinite(p.editor))setEditorWidth(Math.max(260,Math.min(850,p.editor)));if(Number.isFinite(p.height))setEquityHeight(Math.max(340,Math.min(850,p.height)));}catch{}},[]);
  useEffect(()=>{const t=setTimeout(()=>localStorage.setItem('pulse-research-layout-v2',JSON.stringify({width:paneWidth,editor:editorWidth,height:equityHeight})),300);return()=>clearTimeout(t);},[paneWidth,editorWidth,equityHeight]);
  const shownCatalog=catalog.filter(i=>!signals || i.kind==='event').map(i=>({...i,category:INDICATOR_SHORTLIST.find(c=>c.id===i.id)?.family||i.category})).filter(i=>(i.name+' '+(i.author||'')+' '+(i.category||'')).toLowerCase().includes(menuSearch.toLowerCase()));
  const toolbar = (
<div className="wb-toolbar">
      <div className="wb-toolbar-row"><label className="wb-scope">币种范围<select value={options.scope} onChange={e => change('scope',e.target.value)}><option value="single">单币种</option><option value="multiple">多币种</option><option value="all">全部合约</option></select></label>
        {options.scope !== 'all' && <label className="wb-symbols">{options.scope === 'multiple' ? '合约 · 空格分隔' : '合约'}<ContractPicker items={contracts} value={symbolText} multiple={options.scope === 'multiple'} onChange={setSymbolText}/></label>}
        <TimeframePicker compact={!signals} value={options.timeframes} onChange={v=>change('timeframes',v)}/>
        <div className="wb-run-action">{signals ? <output>{busy ? '正在更新信号…' : pausedSignature===signature ? '已停止自动更新' : !valid ? '请选择完整条件' : (options.end.slice(0,10)===beijingDay()?'所选周期自动补缺 · 收盘确认 · 每分钟重查 · 未下单':'选完自动补缺并更新')}</output> : <button className="wb-primary" disabled={!online || busy || !valid} onClick={() => void start()}>{busy ? '正在准备 / 回测…' : '开始回测'}</button>}
        {signals && !busy && <button disabled={!online || !valid} onClick={()=>{setPausedSignature('');void start();}}>{pausedSignature===signature?'恢复信号':'刷新信号'}</button>}{busy && job && <button onClick={() => {stopped();void request(api,`/api/workbench/runs/${job.id}/stop`,{}).catch(e => setError(e.message));}}>停止</button>}</div>
      </div>
      <div className="wb-toolbar-row"><div className="wb-date-range" aria-label="回测起止时间（北京时间）"><label>开始时间<input aria-label="开始时间" type="datetime-local" step="1" value={options.start} onChange={e => change('start',e.target.value)}/></label><span>至</span><label>结束时间<input aria-label="结束时间" type="datetime-local" step="1" value={options.end} onChange={e => change('end',e.target.value)}/></label></div>
        <div className="wb-actions"><button onClick={() => setOptions(o => ({...o,start:beijingDay()+'T00:00',end:beijingDay()+'T23:59:59'}))}>今天</button><button onClick={() => { const end = new Date(Date.now()+7*3600000).toISOString().slice(0,19); setOptions(o => ({...o,start:end.slice(0,10)+'T00:00',end})); }}>截至一小时前</button><button onClick={() => setOptions(o => ({...o,start:new Date(Date.now()+8*3600000-7*86400000).toISOString().slice(0,10)+'T00:00',end:new Date(Date.now()+7*3600000).toISOString().slice(0,19)}))}>近 7 天</button></div><span className="wb-timezone">北京时间 UTC+8</span>
      </div>
      {!signals&&<div className="wb-toolbar-row wb-capital"><label>初始资金 · USDT<input aria-label="初始资金" type="number" min="1" value={options.initial_capital??10000} onChange={e=>setOptions(o=>({...o,account_mode:true,initial_capital:Number(e.target.value)}))}/></label><label>{options.capital_mode==='equity'?'保证金随权益自动计算':'每笔保证金 · USDT'}<input disabled={options.capital_mode==='equity'} aria-label="每笔保证金" type="number" min="1" value={options.margin_per_trade??1000} onChange={e=>setOptions(o=>({...o,account_mode:true,margin_per_trade:Number(e.target.value)}))}/></label><label>杠杆 · 倍<input aria-label="杠杆倍数" type="number" min="1" max="125" step="1" value={options.leverage??1} onChange={e=>setOptions(o=>({...o,account_mode:true,leverage:Number(e.target.value)}))}/></label><label>最多持仓<input type="number" min="1" max="100" value={options.max_positions??5} onChange={e=>change('max_positions',Number(e.target.value))}/></label><span className="wb-note">{options.capital_mode==='equity'?'每笔名义金额：可用权益 × 杠杆（预留手续费）':`每笔名义金额 ${number((options.margin_per_trade??1000)*(options.leverage??1))} USDT`} · 同币不叠加</span></div>}
    </div>
  );
  return <div className={"wb "+(!signals?"wb-studio":"wb-live")+(libraryHidden?" wb-hide-library":"")+(sourceHidden?" wb-hide-source":"")+(resultsHidden?" wb-hide-results":"")}>
    {signals&&<details style={{gridColumn:"1 / -1",padding:16}}><summary>美股 SMC 盘中提醒 · 自选币种与周期 · Telegram</summary><PersonalRuleSettings api={api}/></details>}
    <ActiveResearch api={api} onIdle={idle} onStopped={stopped}/>{error && <p className="wb-error" role="alert">{error}</p>}{options.timeframes.includes("1s")&&<p className="wb-note">1 秒暂不可用：当前合约接口实测拒绝秒线，请选择 1 分钟及以上周期。</p>}
    {signals&&toolbar}
    <div className="wb-layout wb-resizable" style={signals?{gridTemplateColumns:`${libraryHidden?"0px":`minmax(160px, min(${paneWidth}px, 35%))`} 6px minmax(0,1fr)`}:{gridTemplateColumns:`${libraryHidden?"0px":`minmax(160px,min(${paneWidth}px,23%))`} 6px ${sourceHidden?"0px":`minmax(260px,min(${editorWidth}px,46%))`} 6px minmax(220px,1fr)`,gridTemplateRows:resultsHidden?"minmax(0,1fr) 6px 0px":`minmax(340px,min(${equityHeight}px,calc(100% - 146px))) 6px minmax(140px,1fr)`}}><aside className="wb-card wb-config"><div className="wb-section-head"><h3>研究库 · 指标策略</h3><button type="button" onClick={()=>setTab('source')}>源码</button></div>
    <input aria-label="搜索指标策略" placeholder="搜索指标策略" value={menuSearch} onChange={e=>setMenuSearch(e.target.value)}/><fieldset>
    <PatternPicker inline value={options.patterns??[]} onChange={v=>setOptions(o=>({...o,pattern:'none',patterns:v}))}/>
    <div className="wb-indicators">{shownCatalog.map(i=><div key={i.id} className="rule-library-card">
      <label aria-label={i.name} className={options.indicators.includes(i.id)?'selected':''}><input type="checkbox" checked={options.indicators.includes(i.id)} onChange={e=>setOptions(o=>({...o,indicators:e.target.checked?[i.id]:[],...(i.id==='nan_strategy1_oversold'?{capital_mode:'equity' as const,max_positions:1}:{})}))}/><span><b>{i.name}</b><small className="wb-indicator-description">{i.description}</small></span></label>
      <RuleSettingsButton name={i.name} value={ruleSettings[i.id]} onChange={v=>updateRuleSettings(i.id,v)} disabled={!options.indicators.includes(i.id)}/>
    </div>)}</div><p className="wb-note">选择规则后，在该项设置中选择活跃度和可做度。使用信号确认时的评分，两项同时满足才入选；楠策略的原生平仓提示不受入场门槛限制。评分不是胜率。</p>
      {!signals && <details className="wb-assumptions"><summary>模拟成交与成本设置</summary><label>仓位方式<select aria-label="仓位方式" value={options.capital_mode??'fixed'} onChange={e=>change('capital_mode',e.target.value as 'fixed'|'equity')}><option value="fixed">每笔固定保证金</option><option value="equity">全部可用权益复利（预留手续费）</option></select></label><label>成交方式<select aria-label="成交方式" value={options.fill_mode??'next_open'} onChange={e=>change('fill_mode',e.target.value as 'next_open'|'signal_close')} disabled={!options.indicators.includes('nan_strategy1_oversold')}><option value="next_open">信号后下一根开盘</option><option value="signal_close">信号当根收盘（理论参考）</option></select></label><label hidden={options.engine === 'quant'||options.indicators.includes('nan_strategy1_oversold')}>入选后持有几根 K 线<input type="number" min="1" max="100" value={options.holding_bars} onChange={e => change('holding_bars',Number(e.target.value))}/></label><label>单边手续费（bps）<input type="number" min="0" max="100" step="0.1" value={options.fee_bps} onChange={e => change('fee_bps',Number(e.target.value))}/></label><label>单边滑点（bps）<input type="number" min="0" max="100" step="0.1" value={options.slippage_bps} onChange={e => change('slippage_bps',Number(e.target.value))}/></label><p className="wb-note">{options.indicators.includes('nan_strategy1_oversold') ? '按原代码相反转折平仓，不使用固定持有期。' : options.engine === 'quant' ? '下一根开盘模拟进入，反向条件后下一根开盘退出。' : '下一根开盘进入，第 N 根收盘退出；退出点不是指标卖点。'}1 bps = 0.01%。</p></details>}
    </fieldset></aside><Splitter value={paneWidth} onChange={setPaneWidth} min={160} max={400} label="调整研究库宽度" collapsed={libraryHidden} onToggle={()=>setLibraryHidden(v=>!v)}/>
    {!signals&&<><div className="wb-authoring">{toolbar}<SourcePane api={api} rules={options.engine==='quant'?strategies.filter(s=>s.id===options.strategy):catalog.filter(i=>options.indicators.includes(i.id))} draft={draft} onDraft={setDraft}/></div><Splitter value={editorWidth} onChange={setEditorWidth} min={260} max={850} label="调整源码与曲线宽度" collapsed={sourceHidden} onToggle={()=>setSourceHidden(v=>!v)}/></>}
    <div className="wb-results"><div className="wb-analysis">
      <section className="wb-card wb-overview"><div className="wb-section-head"><h3>{signals ? (quant?'策略开平仓信号':'指标入选信号') : '回测概览'}</h3>{job?.state === 'completed' && <a href={`${api}/api/workbench/runs/${job.id}/results.csv`} download>下载本次全部结果 CSV ↓</a>}</div>
        {job?.portfolio&&!signals&&<div className="wb-run-context" title={`${job.indicator_names.join(' + ')} · ${job.request.start} 至 ${job.request.end}`}><span>结果参数：{number(job.portfolio.initial_capital)} USDT · {job.request.leverage} 倍 · {job.indicator_names.join(' + ')} · {job.request.start.slice(0,10)} — {job.request.end.slice(0,10)}</span>{resultChanged&&<strong>参数已更改，开始回测后更新结果</strong>}</div>}
        {job && <p>本次：{job.indicator_names.join(job.request.combination === 'all' ? ' + ' : ' 或 ')} · {job.request.scope === 'all' ? '全部本地币种' : job.request.symbols.join('、')} · {job.request.start.replace('T',' ')} 至 {job.request.end.replace('T',' ')}</p>}
        {job?.portfolio&&!signals ? <div className="wb-metrics wb-account-metrics">{[['账户权益',number(job.portfolio.final_equity)],['净收益',number(job.portfolio.net_profit)+' USDT'],['收益率',number(job.portfolio.return_pct,'%')],['最大回撤',number(job.portfolio.max_drawdown_pct,'%')],['平仓 / 持仓',`${job.portfolio.closed} / ${job.portfolio.open}`],['胜率（已平仓）',number(job.portfolio.win_rate==null?null:job.portfolio.win_rate*100,'%')],['跳过 / 风险强平',`${job.portfolio.skipped} / ${job.portfolio.liquidations}`],['浮动盈亏',number(job.portfolio.unrealized)+' USDT']].map(([title,value])=><div key={title}><span>{title}</span><b>{value}</b></div>)}</div> : metrics && signals ? <p>当前显示 {job?.total_items ?? metrics.selected} 条信号记录。点击币种查看本次日期范围内该币的全部记录；选择条件后自动检查整段历史。</p> : metrics ? <><div className="wb-metrics"><div><span>入选 / 有后续结果</span><b>{metrics.selected} / {metrics.completed}</b></div><div><span>扣费后正收益比例</span><b>{number(metrics.win_rate == null ? null : metrics.win_rate*100,'%')}</b></div><div><span>单条平均净收益</span><b>{number(metrics.average_return_pct,'%')}</b></div><div><span>总正收益 / 总负收益绝对值</span><b>{number(metrics.profit_factor)}</b></div></div><p className="wb-note" title={job?.basis}>{job?.request.engine === 'quant' ? '反手策略（非账户收益）' : '逐条独立观察（非账户收益）'} · 待完成 {metrics.pending} · 盈利 {metrics.wins} · 亏损 {metrics.losses} · {job?.request.engine === 'quant' ? '反向条件退出' : '固定持有期模拟退出'}</p></> : <p className="wb-empty">{job?.state === 'running' ? (signals ? '正在逐根检查，完成后显示全部入选记录。' : '正在逐根检查。完成后这里会显示结果和买卖点。') : job?.message || (signals ? '选择条件后自动更新' : '选择币种、起止时间和指标，点击开始回测')}</p>}
      </section>
      {!signals&&<div className="wb-equity-slot"><EquityPanel data={job?.portfolio}/></div>}</div>{!signals&&<Splitter vertical value={equityHeight} onChange={setEquityHeight} min={340} max={850} label="调整上方工作区与结果表高度" collapsed={resultsHidden} onToggle={()=>setResultsHidden(v=>!v)}/>}
      <section className="wb-card wb-result-panel"><div className="wb-tabs" role="tablist" aria-label="回测结果">{[['trades',signals ? '入选信号记录' : '图表与逐笔记录'],['quality','数据与可信度'],['logs','参数与日志'],['library','源码清单'],...(signals?[['source','查看源码']]:[])].map(([id,label]) => <button role="tab" aria-selected={tab === id} key={id} onClick={() => setTab(id)}>{label}</button>)}</div>
        <div className={'wb-tab-content '+(tab === 'trades' ? 'wb-trades' : '')}>{tab === 'trades' && <>
          {chartLoading && <output>正在读取该记录的 K 线…</output>}{chart && <SignalChart api={api} runId={chartSelection?.runId||job?.id} ruleControls={chartRuleControls} showPatterns={!signals} refreshUrl={chartEndpoint||undefined} data={chart} onClose={() => {chartPreparation.current?.abort();chartSequence.current++;setChart(null);setChartEndpoint('');setChartSelection(null);setChartLoading(false);}}/>} {!chart && !signals && <p className="wb-note">点击“看买卖点”，在 K 线上查看信号确认、模拟入场和退出。点击币种，可筛出本次研究中该币的全部记录。</p>}
          {symbolHistory && <div className="wb-actions"><b>{symbolHistory} · 本次范围内全部入选历史</b><button onClick={() => { setSymbolHistory(''); setPage(0); }}>返回全部币种</button></div>}
          <div className="wb-table-wrap"><table><thead><tr>{(signals ? ['币种 / 周期','确认时间（北京时间）','信号性质 / 方向','可做度','活跃度','图表'] : ['币种 / 周期','信号确认（北京时间）','性质 / 方向','模拟入场 → 退出',job?.portfolio?'账户净盈亏 / 数量':'扣费后收益',...(!job?.portfolio?['最大有利 / 不利']:[]),'结果 / 图表']).map((h,i) => <th key={h} aria-sort={i===1?(sortOrder==='desc'?'descending':'ascending'):undefined}>{i===1?<button className="wb-time-sort" title={sortOrder==='desc'?'最新在前；点击改为最早在前':'最早在前；点击改为最新在前'} onClick={()=>{setSortOrder(o=>o==='desc'?'asc':'desc');setPage(0);}}>{h} <span>{sortOrder==='desc'?'↓':'↑'}</span></button>:h}</th>)}</tr></thead><tbody>{(signals && submittedSignature !== signature ? [] : job?.items)?.map(r => <tr key={r.id}>
            <td><button className="wb-link" onClick={() => { setSymbolHistory(r.symbol); setPage(0); }}>{r.symbol}</button><small>{r.timeframe} · {r.pattern}</small></td><td>{beijingTime(r.signal_time)}<small>信号价 {price(r.signal_price)}</small></td><td>{r.direction === 'long' ? '看多' : '看空'}<small>{r.signal_kind}</small></td>{signals&&<><td><ActionabilityCell value={r.actionability} api={api} symbol={r.symbol}/></td><td><ActivityCell score={r.activity_score} max={r.score_available_max}/></td><td><button onClick={()=>void viewChart(r)}>看信号点</button></td></>}{!signals && <><td>{price(job?.portfolio?r.account_entry_price:r.entry_price)} → {price(job?.portfolio?r.account_exit_price:r.exit_price)}<small>{beijingTime(job?.portfolio?(r.account_entry_time??null):r.entry_time)}<br/>{beijingTime(job?.portfolio?(r.account_exit_time??null):r.exit_time)}</small></td><td className={(job?.portfolio?r.account_pnl:r.net_return_pct) == null ? '' : (job?.portfolio?r.account_pnl!:r.net_return_pct!) >= 0 ? 'wb-positive' : 'wb-negative'}>{job?.portfolio?<>{number(r.account_pnl,' USDT')}<small>数量 {number(r.quantity)} · 保证金 {number(r.margin)}<br/>浮盈亏 {number(r.account_unrealized,' USDT')}</small></>:number(r.net_return_pct,'%')}</td>{!job?.portfolio&&<td>{number(r.best_move_pct,'%')} / {number(r.worst_move_pct,'%')}</td>}<td>{job?.portfolio?r.account_status:r.status}<small><button disabled={!!job?.portfolio&&!r.account_entry_time} onClick={() => void viewChart(r)}>看买卖点</button></small></td></>}
          </tr>)}</tbody></table></div>
          {job?.state === 'completed' && !job.items?.length && <p className="wb-empty">本次没有符合条件的入选记录。请先查看“数据与可信度”，确认历史覆盖和指标预热是否足够。</p>}
          {!!job?.total_items && <div className="wb-pagination"><button disabled={page === 0} onClick={() => setPage(p => p-1)}>上一页</button><span>第 {page+1} / {Math.ceil(job.total_items/50)} 页 · 共 {job.total_items} 条</span><button disabled={(page+1)*50 >= job.total_items} onClick={() => setPage(p => p+1)}>下一页</button></div>}
        </>}
        {tab==='source'&&<SourcePane api={api} rules={options.engine==='quant'?strategies.filter(s=>s.id===options.strategy):catalog.filter(i=>options.indicators.includes(i.id))} draft={draft} onDraft={setDraft}/>}
        {tab === 'quality' && <ScoreValidation api={api}/>}
        {tab === 'quality' && signals && <><h3>信号数据覆盖</h3><p>{job?.basis||'只读取已收盘 K 线；数据缺口后的指标需要重新预热。'}</p><p>信号只用于提示，不代表实际账户持仓或成交。</p><div className="wb-table-wrap"><table><thead><tr><th>合约 / 周期</th><th>已保存 / 应有</th><th>缺少</th><th>预热根数</th></tr></thead><tbody>{job?.coverage?.map(c=><tr key={c.symbol+c.timeframe}><td>{c.symbol} · {c.timeframe}</td><td>{c.bars} / {c.expected}</td><td>{c.missing}</td><td>{c.warmup}</td></tr>)}</tbody></table></div></>}
        {tab === 'quality' && !signals && <><h3>这些结果能说明什么？</h3><p>当前检验“指标选出的币之后怎么走”。完整量化优劣还需要下面的证据，胜率高不能单独证明策略有效。</p><div className="wb-evidence">{[
          ['行情完整性', metrics ? `${number(metrics.bars)} 根已保存 K 线；范围内缺少 ${number(metrics.missing_bars)} 根。缺口后指标重新预热。` : '检查 OHLCV、成交额、时间戳、缺口、上市与退市时间。'],
          ['真实成交成本','已计算双边手续费和固定滑点；资金费率、盘口深度、最小下单量、成交量限制尚未建模。'],
          ['账户收益与风险','已支持初始资金、固定逐仓保证金、杠杆和同时持仓；资金曲线包含浮盈亏。风险强平采用成交价 OHLC 近似，未复现标记价格、档位、资金费率；夏普与基准超额暂不填造。'],
          ['样本外验证','保留未参与调参的日期，冻结规则后再运行；还需跨牛熊行情、滚动验证及参数敏感性测试。'],
          ['模拟与实盘核对','对比信号时间、订单成交、滑点与收益偏差；策略接入前均为未验证。'],
        ].map(([title,body]) => <div key={title}><h4>{title}</h4><p>{body}</p></div>)}</div>
          {!!job?.missing_symbols?.length && <p className="wb-error">没有历史数据的币种：{job.missing_symbols.join('、')}</p>}
          <p className="wb-note">参考：<a href="https://www.quantconnect.com/docs/v2/writing-algorithms/key-concepts/research-guide" target="_blank" rel="noreferrer">QuantConnect 研究指南</a> · <a href="https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data" target="_blank" rel="noreferrer">币安合约历史行情、标记价格与资金费率</a></p><h4>每个币种实际覆盖</h4><div className="wb-table-wrap"><table><thead><tr>{['币种 / 周期','已保存 / 应有','缺少 / 内部缺口','预热 K 线','实际开始 → 结束'].map(h=><th key={h}>{h}</th>)}</tr></thead><tbody>{job?.coverage?.slice(coveragePage*30,(coveragePage+1)*30).map(c => <tr key={c.symbol+c.timeframe}><td>{c.symbol} · {c.timeframe}</td><td>{c.bars} / {c.expected}</td><td>{c.missing} / {c.internal_gaps}</td><td>{c.warmup}</td><td>{beijingTime(c.first)}<br/>{beijingTime(c.last)}</td></tr>)}</tbody></table></div>
          {!!job?.coverage?.length && <div className="wb-pagination"><button disabled={!coveragePage} onClick={() => setCoveragePage(p=>p-1)}>上一页</button><span>{coveragePage+1} / {Math.ceil(job.coverage.length/30)}</span><button disabled={(coveragePage+1)*30 >= job.coverage.length} onClick={() => setCoveragePage(p=>p+1)}>下一页</button></div>}
        </>}
        {tab === 'library' && <ScriptLibrary api={api} onlyKind={quant?"strategy":signals?"indicator":undefined}/>}
        {tab === 'logs' && <><h3>运行日志</h3><p>WaveTrend 逐根收盘计算，读取信号时点及之前的历史。缺口后重新预热；设置门槛依据当时评分筛选。</p><ol>{job?.logs?.map((line,i)=><li key={i}>{line}</li>)}</ol><p>{job?.message}</p>{job && <details><summary>本次参数快照</summary><pre>{JSON.stringify(job.request,null,2)}</pre></details>}</>}
        {tab === 'saved' && <>    <details className="wb-archives" open><summary>每日保存数据 · {archives.length} 个 CSV <span>放在这里，按需展开下载</span></summary><p className="wb-note">每日原始候选、指标快照和独立指标入选证据按北京时间合并归档。同规则版本重复扫描去重；CSV 区分实时异常与历史重算。上方研究记录保存本次全部信号及后续表现。</p><div className="wb-table-wrap"><table><thead><tr><th>日期</th><th>记录 / 币种</th><th>文件</th><th>操作</th></tr></thead><tbody>{archives.map(a=><tr key={a.date}><td>{a.date}</td><td>{a.records.toLocaleString()} / {a.symbols}</td><td><a href={`${api}/api/daily-archives/${a.date}.csv`} download>{a.filename} ↓</a></td><td><button disabled={busy} onClick={()=> { setOptions(o=>({...o,start:a.date+'T00:00',end:a.date+'T23:59:59'})); setTab('trades'); }}>使用这个日期</button></td></tr>)}</tbody></table></div></details>      <details className="wb-saved-list" open><summary>研究记录 · {history.length} 次</summary><div className="wb-table-wrap"><table><thead><tr><th>时间</th><th>指标</th><th>状态</th><th>操作</th></tr></thead><tbody>{history.map(j => <tr key={j.id}><td>{beijingTime(j.created_at)}</td><td>{j.indicator_names.join(' + ')}</td><td>{({completed:'已完成',failed:'失败',cancelled:'已停止',running:'运行中'} as Record<string,string>)[j.state] || j.state}</td><td><button disabled={busy} onClick={() => { setPage(0); setSymbolHistory(''); setCoveragePage(0); setChart(null); chartSequence.current++; request<Run>(api,'/api/workbench/runs/'+j.id).then(r => {setJob(r);setOptions({...r.request,purpose:signals?'signals':'backtest'});setSymbolText(r.request.symbols.join(' '));setTab('trades');localStorage.setItem(key,r.id);}).catch(e=>setError(e.message)); }}>打开结果</button></td></tr>)}</tbody></table></div></details>
</>}
        {tab === 'data' && <HistoryDataTools api={api} online={online}/>}

        </div>
      </section>
    </div></div>
    <div className="wb-statusbar"><span>{job?.preparation && job.state === 'running' ? `${({checking:'检查历史',downloading:'补齐历史',verifying:'复查缺口',ready:'数据已齐'} as Record<string,string>)[job.preparation.stage]} · ${job.preparation.current} · ${job.preparation.checked}/${job.preparation.total} 组 · 已补存 ${job.preparation.downloaded} 根` : job?.state === 'running' ? `${job.current || '读取历史'} · ${job.processed}/${job.total} 组` : job?.message || '就绪'}</span><div className="wb-bottom-tabs"><button aria-pressed={tab === 'saved'} onClick={() => setTab(tab === 'saved' ? 'trades' : 'saved')}>每日 CSV / 研究记录</button><button aria-pressed={tab === 'data'} onClick={() => setTab(tab === 'data' ? 'trades' : 'data')}>补充历史数据</button><span>UTC+8</span></div></div>
  </div>;
}
