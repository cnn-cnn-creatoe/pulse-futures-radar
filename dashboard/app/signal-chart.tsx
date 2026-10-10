'use client';
import {useEffect,useMemo,useRef,useState,type ReactNode} from 'react';
import {createPortal} from 'react-dom';
import type {IChartApi,IPriceLine,IPrimitivePaneView,ISeriesPrimitive,UTCTimestamp} from 'lightweight-charts';
import {beijingTime} from './daily-history';
import './signal-chart.css';
import {attachRuler,type RulerReadout} from './chart-ruler';
import {SelectedSignalScores} from './selected-signal-scores';
import {PatternPicker} from './pattern-picker';
import {useChartScoreFilter} from './chart-score-filter';
import {markerBarIndex,markerMatchesPattern,arrowAnchor} from './chart-marker-layout';

type Bar={open_time:number;close_time:number;open:number;high:number;low:number;close:number;volume?:number};
type Marker={time:number;price:number;action:string;label:string;selected:boolean;score_time?:number;pattern?:string};
type Data={overlays?:{name:string;color:string;points:{time:number;value:number|null;color?:string}[]}[];tick_size?:number;row:{symbol:string;timeframe:string;signal_time:number};candles:Bar[];note:string;markers:Marker[];start:number;end:number;live:boolean};
const colors:Record<string,string>={candidate_long:'#36d69a',candidate_short:'#f05c6e',buy:'#36d69a',sell:'#f05c6e',close_long:'#579bff',close_short:'#a6adb9',record:'#f4c56a'};
const labels:Record<string,string>={candidate_long:'多向候选',candidate_short:'空向候选',buy:'买入',sell:'卖出',close_long:'平多',close_short:'平空',record:'记录'};
const seconds=(ms:number)=>Math.floor(ms/1000) as UTCTimestamp;
const decimals=(value:number)=>{const [n,e='0']=value.toString().split('e');return Math.max(0,(n.split('.')[1]?.length||0)-Number(e));};
export function SignalChart({data:initialData,onClose,ruleControls,api,runId,refreshUrl,showPatterns=true}:{data:Data;onClose:()=>void;ruleControls?:ReactNode;api?:string|null;runId?:string;refreshUrl?:string;showPatterns?:boolean}) {
 const [snapshot,setSnapshot]=useState<{seed:Data;data:Data}|null>(null);
 const data=snapshot?.seed===initialData?snapshot.data:initialData;
 const scoreFilter=useChartScoreFilter(api,data.row.symbol,data.row.timeframe,data.markers||[],runId);
 const [refreshError,setRefreshError]=useState('');
 const dialog=useRef<HTMLDialogElement>(null),container=useRef<HTMLDivElement>(null),chartRef=useRef<IChartApi|null>(null);
 const controls=useRef<{focus:()=>void;refresh:()=>void;move:(delta:number)=>void}|null>(null);
 const rulerRef=useRef<ReturnType<typeof attachRuler>|null>(null);
 const [measuring,setMeasuring]=useState(false),[selectedMarker,setSelectedMarker]=useState<Marker|null>(null),[patterns,setPatterns]=useState<string[]>([]);
 const [rulerReadout,setRulerReadout]=useState<RulerReadout|null>(null);
 const interacting=useRef(false);interacting.current=measuring||!!rulerReadout;
 const dragging=useRef(false);
 const viewState=useRef<{context:string;range:{from:number;to:number}|null;priceRange:{from:number;to:number}|null;selected?:string;count:number}|null>(null);
 const context=refreshUrl||`${data.row.symbol}:${data.row.timeframe}:${data.row.signal_time}`;
 useEffect(()=>{
  if(!refreshUrl||!initialData.live)return;
  let disposed=false,pending=false;const controller=new AbortController();
  const load=async()=>{if(pending||document.hidden||interacting.current||dragging.current)return;pending=true;try{
   const response=await fetch(refreshUrl,{signal:AbortSignal.any([controller.signal,AbortSignal.timeout(20000)])});
   const next=await response.json() as Data&{detail?:string};if(!response.ok)throw Error(next.detail||'更新失败');
   if(!disposed&&!interacting.current&&!dragging.current){setRefreshError('');setSnapshot(previous=>{
    const old=previous?.seed===initialData?previous.data:initialData;
    return JSON.stringify([old.candles,old.markers,old.overlays])===JSON.stringify([next.candles,next.markers,next.overlays])?previous:{seed:initialData,data:next};
   });}
  }catch(e){if(!disposed)setRefreshError('图表更新失败，保留上次数据：'+(e as Error).message);}finally{pending=false;}};
  const timer=setInterval(()=>void load(),15000);
  return()=>{disposed=true;controller.abort();clearInterval(timer);};
 },[refreshUrl,initialData]);
 const [navigation,setNavigation]=useState({index:0,total:0});
 const [hover,setHover]=useState<Bar|null>(null),[detail,setDetail]=useState(''),[error,setError]=useState(''),[visible,setVisible]=useState(0);
 const bars=useMemo(()=>[...data.candles].filter(c=>[c.open,c.high,c.low,c.close,c.open_time,c.close_time].every(Number.isFinite)).sort((a,b)=>a.open_time-b.open_time).filter((c,i,a)=>!i||c.open_time!==a[i-1].open_time),[data.candles]);
 const precision=useMemo(()=>Math.min(12,(data.markers||[]).reduce((p,m)=>Math.max(p,decimals(m.price)),bars.slice(0,500).reduce((p,c)=>Math.max(p,decimals(c.open),decimals(c.close)),0))),[bars,data.markers]);
 useEffect(()=>{const previous=document.activeElement as HTMLElement|null;const d=dialog.current;d?.showModal();return()=>{d?.close();previous?.focus();};},[]);
 useEffect(()=>{
  const escape=(event:KeyboardEvent)=>{
   if(event.key!=='Escape'||!dialog.current?.open)return;
   // The native chart is above the history dialog: dismiss only this layer.
   event.preventDefault();event.stopImmediatePropagation();const picker=dialog.current.querySelector('details[open]');if(picker){picker.removeAttribute('open');return;}onClose();
  };
  window.addEventListener('keydown',escape,true);
  return()=>window.removeEventListener('keydown',escape,true);
 },[onClose]);
 useEffect(()=>{
  let cancelled=false,cleanup:(()=>void)|undefined;setError('');setHover(null);setDetail('记录时间：'+beijingTime(data.row.signal_time)+'；时间轴显示 K 线开盘时间');setSelectedMarker(null);setMeasuring(false);setRulerReadout(null);setNavigation({index:0,total:0});
  if(!bars.length)return;
  import('lightweight-charts').then(({createChart,CandlestickSeries,LineSeries,ColorType,LineStyle,CrosshairMode})=>{
   if(cancelled||!container.current)return;
   const chart=createChart(container.current,{
    autoSize:true,layout:{background:{type:ColorType.Solid,color:'#101720'},textColor:'#9fb2ca',fontSize:12,attributionLogo:true},
    grid:{vertLines:{color:'#1c2939'},horzLines:{color:'#1c2939'}},
    rightPriceScale:{borderColor:'#334155',scaleMargins:{top:.15,bottom:.15},minimumWidth:100},
    timeScale:{borderColor:'#334155',timeVisible:true,secondsVisible:data.row.timeframe==='1s',rightOffset:4,minBarSpacing:.05,
     tickMarkFormatter:(t:unknown)=>typeof t==='number'?beijingTime(t*1000).replace(/^\d{4}\//,'').slice(0,data.row.timeframe==='1s'?undefined:-3):''},
    localization:{locale:'zh-CN',timeFormatter:(t:number)=>beijingTime(t*1000)},
    crosshair:{mode:CrosshairMode.Normal,vertLine:{color:'#7187a2',labelBackgroundColor:'#334155'},horzLine:{visible:false,labelVisible:false}},
    handleScroll:{mouseWheel:true,pressedMouseMove:true,horzTouchDrag:true,vertTouchDrag:true},
    handleScale:{mouseWheel:true,pinch:true,axisPressedMouseMove:{time:true,price:true},axisDoubleClickReset:{time:true,price:true}},
   });
   chartRef.current=chart;
   const series=chart.addSeries(CandlestickSeries,{upColor:'#42c6aa',downColor:'#ed7489',borderVisible:false,wickUpColor:'#42c6aa',wickDownColor:'#ed7489',priceLineVisible:false,lastValueVisible:false,priceFormat:{type:'price',precision,minMove:10**-precision}});
   series.setData(bars.map(c=>({time:seconds(c.open_time),open:c.open,high:c.high,low:c.low,close:c.close})));
   for(const overlay of data.overlays||[]){
    const line=chart.addSeries(LineSeries,{color:overlay.color,lineWidth:1,title:overlay.name,priceLineVisible:false,lastValueVisible:false,crosshairMarkerVisible:false});
    const visibleTimes=new Set(bars.map(c=>c.open_time));
    line.setData(overlay.points.filter(p=>visibleTimes.has(p.time)).map(p=>p.value!=null&&Number.isFinite(p.value)?{time:seconds(p.time),value:p.value,...(p.color?{color:p.color}:{})}:{time:seconds(p.time)}));
   }
   const ruler=attachRuler(container.current,chart,series,bars,precision,data.tick_size,()=>setMeasuring(false),setRulerReadout);rulerRef.current=ruler;
   const host=container.current,dragStart=()=>{dragging.current=true;},dragEnd=()=>{dragging.current=false;};
   host.addEventListener('pointerdown',dragStart,true);window.addEventListener('pointerup',dragEnd,true);window.addEventListener('pointercancel',dragEnd,true);window.addEventListener('blur',dragEnd);
   const byTime=new Map(bars.map(b=>[seconds(b.open_time),b]));
   const barIndex=(time:number)=>markerBarIndex(bars,time);
   const marks=(data.markers||[]).filter(scoreFilter.passes).map((m,i)=>({...m,index:barIndex(m.time),id:'signal-'+i})).filter(m=>m.index>=0&&Number.isFinite(m.price)&&!!colors[m.action]).filter(m=>markerMatchesPattern(m.action,m.pattern,m.index,bars,patterns)).sort((a,b)=>a.index-b.index||a.time-b.time);
   const markerKey=(m:Marker)=>`${m.time}:${m.action}:${m.price}`;
   const saved=viewState.current?.context===context?viewState.current:null;
   let selected=marks.find(m=>markerKey(m)===saved?.selected)?.id||marks.find(m=>m.time===data.row.signal_time&&m.action!=='record')?.id||marks.find(m=>m.selected)?.id;
   let lines:IPriceLine[]=[],lineKey='';
   const formatPrice=(v:number)=>v.toFixed(precision);
   let repaint=()=>{};
   const coordinates=()=>{
    const range=chart.timeScale().getVisibleLogicalRange(),width=container.current?.clientWidth||1000;
    const spacing=width/Math.max(1,(range?.to||bars.length)-(range?.from||0));
    const radius=Math.min(20,Math.max(1,Math.ceil(9/spacing)));
    const placed:{x:number;ay:number;above:boolean}[]=[];
    return marks.filter(m=>m.action!=='record').flatMap(m=>{
     const x=chart.timeScale().timeToCoordinate(seconds(bars[m.index].open_time)),y=series.priceToCoordinate(m.price);
     if(x===null||y===null||x< -12||x>width+12)return [];
     const above=m.action==='sell'||m.action==='close_long'||m.action==='candidate_short';
     const neighbors=bars.slice(Math.max(0,m.index-radius),Math.min(bars.length,m.index+radius+1));
     const edge=series.priceToCoordinate(above?Math.max(...neighbors.map(b=>b.high)):Math.min(...neighbors.map(b=>b.low)));
     if(edge===null)return [];
     const ay=arrowAnchor(edge,above,chart.panes()[0].getHeight(),x,placed);
     placed.push({x,ay,above});return [{m,x,y,ay,above}];
    });
   };
   const view:IPrimitivePaneView={zOrder:()=> 'top',renderer:()=>({draw(target){target.useMediaCoordinateSpace(({context:ctx,mediaSize})=>{
    const points=coordinates();ctx.save();ctx.beginPath();ctx.rect(0,0,mediaSize.width,mediaSize.height);ctx.clip();
    for(const {m,x,y,ay,above} of points){
     if(x<0||x>mediaSize.width)continue;
     const color=colors[m.action];
     if(m.id===selected){ctx.beginPath();ctx.strokeStyle=color;ctx.lineWidth=m.id===selected?1.6:1;ctx.setLineDash([5,4]);ctx.moveTo(x,y);ctx.lineTo(mediaSize.width,y);ctx.stroke();ctx.beginPath();ctx.setLineDash([]);ctx.arc(x,y,2.5,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();}
     if(m.action==='record'){ctx.beginPath();ctx.moveTo(x,ay-5);ctx.lineTo(x+5,ay);ctx.lineTo(x,ay+5);ctx.lineTo(x-5,ay);ctx.closePath();ctx.fillStyle=color;ctx.fill();continue;}
     ctx.save();ctx.translate(x,ay);ctx.rotate(above?Math.PI:0);
     ctx.setLineDash([]);ctx.beginPath();ctx.moveTo(0,0);ctx.lineTo(-4,7);ctx.lineTo(-1.5,7);ctx.lineTo(-1.5,12);ctx.lineTo(1.5,12);ctx.lineTo(1.5,7);ctx.lineTo(4,7);ctx.closePath();ctx.fillStyle=color;ctx.fill();ctx.strokeStyle=m.id===selected?'#fff':'#101720';ctx.lineWidth=m.id===selected?1.3:.6;ctx.stroke();ctx.restore();
    }
    ctx.restore();
   });}})};
   const primitive:ISeriesPrimitive={paneViews:()=>[view],attached:p=>{repaint=p.requestUpdate;},hitTest:(x,y)=>{const hit=coordinates().find(p=>Math.abs(x-p.x)<=7&&Math.abs(y-(p.ay+(p.above?-6:6)))<=9);return hit?{externalId:hit.m.id,zOrder:'top',cursorStyle:'pointer'}:null;}};
   series.attachPrimitive(primitive);
   const drawMarkers=()=>repaint();
   const refresh=()=>{
    const range=chart.timeScale().getVisibleLogicalRange();
    const from=Math.max(0,Math.ceil(range?.from??0)),to=Math.min(bars.length-1,Math.floor(range?.to??bars.length-1));
    setVisible(Math.max(0,to-from+1));
    const targets=marks.filter(m=>m.id===selected&&m.action!=='record');
    const unique=[...new Map(targets.map(m=>[m.action+':'+m.price,m])).values()];
    const key=unique.map(m=>m.id).join(',')+'|'+selected;
    repaint();if(key===lineKey)return;lineKey=key;
    lines.forEach(line=>series.removePriceLine(line));
    lines=unique.map(m=>series.createPriceLine({price:m.price,color:colors[m.action],lineWidth:m.id===selected?2:1,lineStyle:LineStyle.Dashed,lineVisible:false,axisLabelVisible:true,title:labels[m.action],axisLabelTextColor:'#101720'}));
   };
   const focus=()=>{const m=marks.find(m=>m.id===selected);const i=m?.index??Math.max(0,barIndex(data.row.signal_time));const from=Math.max(-1,Math.min(i-50,bars.length-100));chart.timeScale().setVisibleLogicalRange({from,to:Math.max(from+100,i+15)});series.priceScale().applyOptions({autoScale:true});};
   const describe=(m:typeof marks[number])=>`${labels[m.action]} · ${formatPrice(m.price)} · ${beijingTime(m.time)}${m.label?' · '+m.label:''}`;
   const choose=(id:string)=>{selected=id;const m=marks.find(m=>m.id===id);if(m){setDetail(describe(m));setSelectedMarker(m);setHover(bars[m.index]);}setNavigation({index:marks.findIndex(m=>m.id===id),total:marks.length});drawMarkers();refresh();};
   const move=(delta:number)=>{const i=marks.findIndex(m=>m.id===selected),next=marks[Math.max(0,Math.min(marks.length-1,i+delta))];if(next){choose(next.id);focus();}};
   if(selected)choose(selected);
   chart.subscribeClick(p=>{const m=marks.find(m=>m.id===p.hoveredObjectId);if(!m)return;choose(m.id);});
   chart.subscribeCrosshairMove(p=>{setHover(typeof p.time==='number'?byTime.get(p.time as UTCTimestamp)||null:null);});
   chart.timeScale().subscribeVisibleLogicalRangeChange(refresh);
   controls.current={focus,refresh,move};drawMarkers();
   if(saved?.range){const delta=saved.range.to>=saved.count-1?bars.length-saved.count:0;chart.timeScale().setVisibleLogicalRange({from:saved.range.from+delta,to:saved.range.to+delta});}
   else if(selected)focus();else chart.timeScale().fitContent();refresh();
   if(saved?.priceRange)series.priceScale().setVisibleRange(saved.priceRange);
   cleanup=()=>{const selectedMark=marks.find(m=>m.id===selected);viewState.current={context,range:chart.timeScale().getVisibleLogicalRange(),priceRange:series.priceScale().options().autoScale?null:series.priceScale().getVisibleRange(),selected:selectedMark?markerKey(selectedMark):undefined,count:bars.length};host.removeEventListener('pointerdown',dragStart,true);window.removeEventListener('pointerup',dragEnd,true);window.removeEventListener('pointercancel',dragEnd,true);window.removeEventListener('blur',dragEnd);dragging.current=false;ruler.dispose();rulerRef.current=null;controls.current=null;chartRef.current=null;chart.remove();};
  }).catch(e=>{if(!cancelled)setError('图表加载失败：'+e.message);});
  return()=>{cancelled=true;cleanup?.();};
 },[bars,data.markers,data.overlays,data.row.signal_time,data.row.timeframe,precision,data.tick_size,patterns,context,scoreFilter.scores,scoreFilter.minimum]);
 return createPortal(<dialog ref={dialog} className="signal-dialog" onCancel={e=>{e.preventDefault();onClose();}} aria-label="信号 K 线图表">
 <header><strong>{data.row.symbol} · {data.row.timeframe}</strong><span>可见 {visible} / {bars.length} 根 · 北京时间</span>{navigation.total>0&&<><button aria-label="上一个信号" title="定位上一个信号" disabled={navigation.index<=0} onClick={()=>controls.current?.move(-1)}>←</button><small>{navigation.total?navigation.index+1:0} / {navigation.total}</small><button aria-label="下一个信号" title="定位下一个信号" disabled={!navigation.total||navigation.index>=navigation.total-1} onClick={()=>controls.current?.move(1)}>→</button><button onClick={()=>controls.current?.focus()}>定位信号</button></>}<button onClick={()=>{chartRef.current?.timeScale().fitContent();chartRef.current?.priceScale('right').applyOptions({autoScale:true});}}>完整范围</button><button autoFocus onClick={onClose}>关闭 · Esc</button></header>
 <div className="signal-chart-info">{hover?`光标 K 线开盘 ${beijingTime(hover.open_time)} · 收盘确认 ${beijingTime(hover.close_time)}　开 ${hover.open}　高 ${hover.high}　低 ${hover.low}　收 ${hover.close}`:`${beijingTime(data.start)} 至 ${beijingTime(data.end)}`}</div>
 <div className="signal-legend">{Object.entries(labels).filter(([k])=>k!=='record'&&(data.markers||[]).some(m=>m.action===k)).map(([k,label])=><span key={k} style={{color:colors[k]}}>{k==='record'?'◆':'▲'} {label}</span>)}<span>拖动平移 · 滚轮缩放 · 拖动右侧价格轴缩放价格{navigation.total>0?' · 点击箭头切换唯一价格线和明细；价格点在虚线起点':''}</span></div>
 <div className="signal-fixed-toolbar">{ruleControls}{scoreFilter.controls}{selectedMarker&&<div className="signal-tool-row">已选{labels[selectedMarker.action]} · 收盘确认 {beijingTime(selectedMarker.time)} · 参考价 {selectedMarker.price}</div>}<div className="signal-tool-row">{showPatterns&&<PatternPicker inline value={patterns} onChange={setPatterns}/>}<button aria-pressed={measuring} onClick={()=>{setMeasuring(!measuring);rulerRef.current?.setEnabled(!measuring);}}>📏 测量</button><span>点击起点 → 移动鼠标 → 点击终点 · 完成后点击别处清除</span><button onClick={()=>rulerRef.current?.clear()}>清除测量</button></div><output className="signal-ruler-accessible" aria-label="测量结果">{rulerReadout?rulerReadout.rows.map((row,i)=><span key={i}>{row}</span>):<span></span>}</output><SelectedSignalScores key={String(selectedMarker?.score_time??selectedMarker?.time)+selectedMarker?.action} api={api} runId={runId} symbol={data.row.symbol} timeframe={data.row.timeframe} time={selectedMarker?.score_time??selectedMarker?.time} direction={(selectedMarker?.action==='record'||selectedMarker?.action?.startsWith('candidate_'))?'record':selectedMarker?.action==='buy'||selectedMarker?.action==='close_long'?'long':'short'}/></div>
 <div className="signal-chart-canvas" ref={container} aria-label="可拖动缩放的 K 线与信号价格图"/>
 {(error||!bars.length)&&<div className="signal-chart-empty">{error||'该范围尚无本地 K 线，请先补充历史数据'}</div>}
 <div className="signal-chart-detail">{detail}</div><footer><span>本地最新 K 线：{bars.length?beijingTime(bars[bars.length-1].close_time):'暂无数据'}。 </span>{refreshUrl&&data.live&&<span>{refreshError||'每 15 秒检查本地已收盘数据；测量期间暂停图表更新。'} </span>}{data.note}</footer></dialog>,document.body);
}
