'use client';
import {usePreservedScroll} from './use-preserved-scroll';
import {MarketChartButton} from './market-chart-button';
import { useEffect, useState, type ReactNode } from 'react';
import { ArrowUpRight, Bell, Globe2, Layers3, ChartNoAxesCombined } from 'lucide-react';
type MarketRow={symbol:string;turnover:number;change_pct:number;last_price:number};
type FullRow={symbol:string;turnover:number|null;change_pct:number|null;last_price:number|null;complete:boolean};
type Snapshot={items:FullRow[];date:string;as_of:number;total:number;covered:number;partial:number;missing:number;up:number;down:number;flat:number;turnover:number;positive:MarketRow[];negative:MarketRow[];all:MarketRow[];leaders:MarketRow[];laggards:MarketRow[];listings:{symbol:string;onboard:number;upcoming:boolean}[]};
const money=(n:number)=>new Intl.NumberFormat('zh-CN',{notation:'compact',maximumFractionDigits:2}).format(n);
const date=(n:number)=>new Date(n).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
export function MarketDashboard({api,online,records,onResearch,onQuant,active=true}:{active?:boolean;api:string|null;online:boolean;records:ReactNode;onResearch:()=>void;onQuant:()=>void}) {
 const rootRef=usePreservedScroll<HTMLDivElement>(active);
 const [data,setData]=useState<Snapshot|null>(null),[error,setError]=useState(''),[tab,setTab]=useState('market'),[search,setSearch]=useState(''),[sort,setSort]=useState<{key:'turnover'|'change_pct';desc:boolean}>({key:'turnover',desc:true}),[page,setPage]=useState(0);
 useEffect(() => {
  if (!api || !active) return;
  let alive = true;
  let timer: ReturnType<typeof setTimeout>;
  const controller = new AbortController();
  const load = async () => {
   let succeeded = false;
   try {
    const response = await fetch(api + '/api/market-overview', {
     signal: AbortSignal.any([controller.signal, AbortSignal.timeout(20000)]),
     cache: 'no-store',
    });
    if (!response.ok) throw Error(`市场统计读取失败（${response.status}），正在自动重试`);
    const snapshot = await response.json() as Snapshot;
    if (alive) { setData(snapshot); setError(''); succeeded = true; }
   } catch (error) {
    if (alive) setError(error instanceof Error && error.message.startsWith('市场统计')
     ? error.message : '行情服务连接中断或超时，正在自动重试');
   } finally {
    if (alive) timer = setTimeout(load, succeeded ? 30000 : 5000);
   }
  };
  void load();
  return () => { alive = false; controller.abort(); clearTimeout(timer); };
 }, [api,active]);
 const sorted=(data?.items||[]).filter(r=>r.symbol.toUpperCase().includes(search.trim().toUpperCase())).sort((a,b)=>{const x=a[sort.key],y=b[sort.key];if(x==null)return y==null?a.symbol.localeCompare(b.symbol):1;if(y==null)return -1;return (sort.desc?y-x:x-y)||a.symbol.localeCompare(b.symbol);});
 const currentPage=Math.min(page,Math.max(0,Math.ceil(sorted.length/50)-1));
 const sortBy=(key:'turnover'|'change_pct')=>{setSort(old=>({key,desc:old.key===key?!old.desc:true}));setPage(0);};
 const ranking=<section className="market-panel ranking"><div className="market-panel-head"><h3>全部合约 <span>{sorted.length}</span></h3><input aria-label="搜索市场合约" placeholder="搜索合约" value={search} onChange={e=>{setSearch(e.target.value);setPage(0);}}/></div><div className="market-scroll"><table><thead><tr><th>#</th><th>合约</th><th>最新收盘价</th><th aria-sort={sort.key==='change_pct'?(sort.desc?'descending':'ascending'):'none'}><button onClick={()=>sortBy('change_pct')}>今日涨跌幅 {sort.key==='change_pct'?(sort.desc?'↓':'↑'):'↕'}</button></th><th aria-sort={sort.key==='turnover'?(sort.desc?'descending':'ascending'):'none'}><button onClick={()=>sortBy('turnover')}>今日成交额（USDT） {sort.key==='turnover'?(sort.desc?'↓':'↑'):'↕'}</button></th><th>数据状态</th><th>图表</th></tr></thead><tbody>{sorted.slice(currentPage*50,(currentPage+1)*50).map((r,i)=><tr key={r.symbol}><td className="rank-number">{currentPage*50+i+1}</td><td><a href={'https://www.binance.com/zh-CN/futures/'+encodeURIComponent(r.symbol)} target="_blank" rel="noreferrer">{r.symbol}</a></td><td>{r.last_price==null?'—':r.last_price.toLocaleString('en-US',{maximumSignificantDigits:8})}</td><td className={r.change_pct==null?'':r.change_pct>=0?'market-up':'market-down'}>{r.change_pct==null?'—':(r.change_pct>=0?'+':'')+r.change_pct.toFixed(2)+'%'}</td><td>{r.turnover==null?'—':money(r.turnover)}</td><td className="market-row-status">{r.complete?'完整':'等待完整行情'}</td><td><MarketChartButton api={api} symbol={r.symbol}/></td></tr>)}</tbody></table>{!sorted.length&&<div className="terminal-empty">{data?'没有匹配的合约':'正在读取市场数据'}</div>}</div><div className="market-pagination"><span>共 {sorted.length} 个 · 每页 50 个</span><button disabled={!currentPage} onClick={()=>setPage(currentPage-1)}>上一页</button><span>{currentPage+1} / {Math.max(1,Math.ceil(sorted.length/50))}</span><button disabled={(currentPage+1)*50>=sorted.length} onClick={()=>setPage(currentPage+1)}>下一页</button></div></section>;
 return <div ref={rootRef} hidden={!active} className="market-dashboard"><div className="terminal-tabs"><button aria-pressed={tab==='market'} onClick={()=>setTab('market')}>市场概览</button><button aria-pressed={tab==='signals'} onClick={()=>setTab('signals')}>当日异常记录</button><span>{data?.date} · 北京时间</span></div>
 {tab==='market'&&error&&<p role="status" className="market-error">{error}{data?' · 当前显示上次成功统计':''}</p>}
 {tab==='signals'?<div className="market-records">{records}</div>:<><div className="market-kpis">{[[Globe2,'监测合约',data?.total.toLocaleString()||'—','USDT 合约'],[ChartNoAxesCombined,'今日成交额',data?money(data.turnover):'—','完整覆盖合约合计'],[ArrowUpRight,'上涨 / 下跌',data?`${data.up} / ${data.down}`:'—',`平盘 ${data?.flat??'—'}`],[Layers3,'数据覆盖',data?`${data.covered} / ${data.total}`:'—',`缺口 ${data?.partial??'—'} · 未载入 ${data?.missing??'—'}`]].map(([Icon,label,value,note])=>{const I=Icon as typeof Globe2;return <div key={String(label)}><span><I size={15}/>{String(label)}</span><b>{String(value)}</b><small>{String(note)}</small></div>;})}</div>
 <div className="market-body"><div className="market-main"><div className="market-filter"><div><b>合约行情</b><small>北京时间今日零点起 · 点击涨跌幅或成交额切换高低排序 · 缺数据排在最后</small></div></div><div className="market-rankings single">{ranking}</div></div>
 <aside className="market-aside"><section className="market-panel"><div className="market-panel-head"><h3><Bell/>新合约上线</h3><span>近 7 天 / 即将上线</span></div><div className="market-scroll listing-list">{data?.listings.map(r=><div key={r.symbol}><b>{r.symbol}</b><span className={r.upcoming?'listing-soon':'listing-live'}>{r.upcoming?'即将上线':'已上线'}</span><small>{date(r.onboard)}</small></div>)}{!data?.listings.length&&<p className="terminal-empty">暂无新上线合约</p>}</div></section><section className="market-panel market-breadth"><div className="market-panel-head"><h3>市场分布</h3></div><div className="breadth-bar"><i style={{flex:data?.up||0}}/><i style={{flex:data?.flat||0}}/><i style={{flex:data?.down||0}}/></div><p><span className="market-up">上涨 {data?.up??'—'}</span><span className="market-down">下跌 {data?.down??'—'}</span></p><dl><dt>前 15 名成交额占比</dt><dd>{data&&data.turnover>0?((data.all.reduce((sum,r)=>sum+r.turnover,0)/data.turnover)*100).toFixed(1)+'%':'—'}</dd><dt>今日领涨</dt><dd>{data?.leaders[0]?.symbol||'—'}</dd><dt>今日领跌</dt><dd>{data?.laggards[0]?.symbol||'—'}</dd></dl><div className="screening-grid"><button onClick={onResearch}>指标策略</button></div></section></aside></div></>}
 <div className="terminal-status">{data?`统计截至 ${date(data.as_of)} · 每 30 秒更新`:'等待本地行情服务'}<span>5 分钟已收盘 K 线 · 成交额统一以 USDT 比较</span></div></div>;
}
