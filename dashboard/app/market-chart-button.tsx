'use client';
import {IntrabarStatus} from './intrabar-status';
import {ChartChoice} from './chart-choice';
import {useState,useRef,useEffect,type ComponentProps} from 'react';
import {SignalChart} from './signal-chart';
import {prepareSignalHistory} from './prepare-signal-history';
import {beijingDay} from './daily-history';
type Rule={id:string;name:string;kind?:string};
type Request={api:string|null;symbol:string;timeframe?:string;time?:number;label?:string;initialRule?:`${'anomaly'|'indicator'|'quant'}:${string}`};
export function MarketChartButton(props:Request){return <button disabled={!props.api} onClick={e=>{e.stopPropagation();window.dispatchEvent(new CustomEvent('open-market-chart',{detail:props}));}}>{props.label || '查看图表'}</button>;}
export function MarketChartHost(){
 const [request,setRequest]=useState<(Request&{requestId:number})|null>(null),sequence=useRef(0);
 useEffect(()=>{const open=(e:Event)=>setRequest({...((e as CustomEvent<Request>).detail),requestId:++sequence.current});window.addEventListener('open-market-chart',open);return()=>window.removeEventListener('open-market-chart',open);},[]);
 return request?<MarketChartSession key={request.requestId} {...request} onClose={()=>setRequest(null)}/>:null;
}
function MarketChartSession({api,symbol,timeframe='5m',time,initialRule='anomaly:two_k',onClose}:Request&{onClose:()=>void}){
 const [chart,setChart]=useState<ComponentProps<typeof SignalChart>['data']|null>(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
 const [catalog,setCatalog]=useState<{indicators:Rule[];strategies:Rule[]}>({indicators:[],strategies:[]});
 const [refreshUrl,setRefreshUrl]=useState('');const preparation=useRef<AbortController|null>(null);const [progress,setProgress]=useState('');
 const [choice,setChoice]=useState<string>(initialRule);const sequence=useRef(0);
 const open=async(selected=choice,period=chart?.row.timeframe||timeframe)=>{if(!api)return;preparation.current?.abort();const abort=new AbortController();preparation.current=abort;const seq=++sequence.current;setBusy(true);setError('');try{
  const [engine,rule]=selected.split(':');
  const day=time?new Date(time+8*3600000).toISOString().slice(0,10):beijingDay();
  const startDay=new Date(Date.parse(day+'T00:00:00+08:00')-29*86400000+8*3600000).toISOString().slice(0,10);
  await prepareSignalHistory(api,{scope:'single',symbols:[symbol],timeframes:[period],engine:engine==='anomaly'?'indicator':engine,...(engine==='quant'?{strategy:rule,indicators:['wt_extreme_cross']}:{indicators:[engine==='anomaly'?'wt_extreme_cross':rule]}),start:startDay+'T00:00',end:day+'T23:59:59'},abort.signal,setProgress);
  const url=`${api}/api/workbench/market-chart?symbol=${encodeURIComponent(symbol)}&timeframe=${period}&history_days=30&engine=${engine}&rule=${encodeURIComponent(rule)}${time?'&signal_time='+time:''}`;
  const r=await fetch(url,{signal:AbortSignal.timeout(20000)});
  const d=await r.json() as ComponentProps<typeof SignalChart>['data'] & {detail?:string};if(!r.ok)throw Error(d.detail||'图表读取失败');if(seq===sequence.current){setChart(d);setChoice(selected);setRefreshUrl(url);}
  if(!catalog.indicators.length){const c=await fetch(api+'/api/workbench/catalog');if(c.ok)setCatalog(await c.json());}
 }catch(e){if(seq===sequence.current)setError((e as Error).message);}finally{if(seq===sequence.current){setBusy(false);setProgress('');}}};
 useEffect(()=>{void open();return()=>{preparation.current?.abort();sequence.current++;};},[]);
 const controls=<div className="signal-tool-row"><span>近 30 天 · 向左拖动查看历史</span><ChartChoice label="周期选择" value={chart?.row.timeframe||timeframe} disabled={busy} onChange={v=>void open(choice,v)} options={["1m","5m","15m","30m","1h","4h","1d","1w","1M"].map(t=>({id:t,name:t}))}/><ChartChoice label="图中信号规则" value={choice} disabled={busy} onChange={v=>void open(v)} options={[{id:'anomaly:two_k',name:'列表 · 两 K 多空候选'},...(catalog.indicators.length?catalog.indicators.filter(r=>r.kind==='event'):[{id:'wt_extreme_cross',name:'WaveTrend · 超买超卖交叉'}]).map(r=>({id:'indicator:'+r.id,name:'指标 · '+r.name})),...catalog.strategies.map(r=>({id:'quant:'+r.id,name:'策略 · '+r.name}))]}/><IntrabarStatus api={api} symbol={symbol} timeframe={chart?.row.timeframe||timeframe} engine={choice.split(':')[0]} rule={choice.split(':')[1]}/>{busy&&<span>{progress||'正在计算箭头…'}</span>}{error&&<span role="alert">{error}</span>}</div>;
 return <SignalChart api={api} refreshUrl={refreshUrl||undefined} data={chart||{row:{symbol,timeframe,signal_time:time||0},candles:[],markers:[],live:true,start:time||Date.now(),end:Date.now(),note:busy?'正在读取本地图表…':error}} ruleControls={controls} onClose={onClose}/>;
}
