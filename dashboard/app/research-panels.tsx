'use client';
import {useEffect,useRef,useState} from 'react';
import {beijingTime} from './daily-history';
export type Portfolio = {initial_capital:number;final_equity:number;balance:number;unrealized:number;net_profit:number;return_pct:number;max_drawdown_pct:number;closed:number;open:number;skipped:number;liquidations:number;fees:number;win_rate:number|null;profit_factor:number|null;model:string;limitations:string[];curve:{time:number;equity:number;balance:number;drawdown_pct:number;unrealized:number}[]};
export function Splitter({value,onChange,min,max,vertical=false,label,collapsed=false,onToggle}:{value:number;onChange:(v:number)=>void;min:number;max:number;vertical?:boolean;label:string;collapsed?:boolean;onToggle?:()=>void}) {
  const drag=useRef<{position:number;value:number}|null>(null);
  const bound=(v:number)=>Math.max(min,Math.min(max,v));
  return <div className={'wb-splitter '+(vertical?'horizontal':'vertical')}><div className="wb-splitter-drag" role="separator" tabIndex={0} aria-label={label} aria-orientation={vertical?'horizontal':'vertical'} aria-valuemin={min} aria-valuemax={max} aria-valuenow={Math.round(value)}
    onPointerDown={e=>{e.currentTarget.focus();drag.current={position:vertical?e.clientY:e.clientX,value};e.currentTarget.setPointerCapture(e.pointerId);e.preventDefault();}}
    onPointerMove={e=>{if(drag.current)onChange(bound(drag.current.value+(vertical?e.clientY:e.clientX)-drag.current.position));}}
    onPointerUp={e=>{drag.current=null;if(e.currentTarget.hasPointerCapture(e.pointerId))e.currentTarget.releasePointerCapture(e.pointerId);}}
    onPointerCancel={()=>{drag.current=null;}}
    onKeyDown={e=>{if(['ArrowLeft','ArrowRight','ArrowUp','ArrowDown','Home','End'].includes(e.key)){e.preventDefault();onChange(e.key==='Home'?min:e.key==='End'?max:bound(value+(['ArrowRight','ArrowDown'].includes(e.key)?10:-10)));}}} title="拖动调整大小；也可用方向键"/>{onToggle&&<button className="wb-collapse-arrow" aria-label={(collapsed?'展开':'收起')+label.replace('调整','')} aria-expanded={!collapsed} onPointerDown={e=>e.stopPropagation()} onKeyDown={e=>e.stopPropagation()} onClick={e=>{e.stopPropagation();onToggle();}}>{vertical?(collapsed?'▴':'▾'):(collapsed?'▸':'◂')}</button>}</div>;
}
const num=(v:number)=>v.toLocaleString('zh-CN',{maximumFractionDigits:2});
export function EquityPanel({data}:{data?:Portfolio}) {
  const [hover,setHover]=useState<number|null>(null);
  const svg=useRef<SVGSVGElement>(null);
  const [size,setSize]=useState({width:640,height:240});
  useEffect(()=>{const element=svg.current;if(!element)return;const observer=new ResizeObserver(([entry])=>{if(entry.contentRect.width>0&&entry.contentRect.height>0)setSize({width:entry.contentRect.width,height:entry.contentRect.height});});observer.observe(element);return()=>observer.disconnect();},[]);
  const points=data?.curve||[];
  const low=points.reduce((m,p)=>Math.min(m,p.equity),data?.initial_capital||0),high=points.reduce((m,p)=>Math.max(m,p.equity),data?.initial_capital||1);
  const spread=Math.max(high-low,(data?.initial_capital||1)*.002);
  const right=Math.max(100,size.width-15),bottom=Math.max(55,size.height-25);
  const y=(v:number)=>15+(high+spread*.1-v)/(spread*1.2)*(bottom-15);
  const from=points[0]?.time||0,to=points[points.length-1]?.time||1;
  const x=(t:number)=>65+Math.max(0,(t-from)/Math.max(1,to-from))*(right-65);
  const point=hover==null?points[points.length-1]:points[hover];
  return <section className="wb-equity wb-card"><header><b>资金曲线 · USDT</b><span>{point?`${beijingTime(point.time)}　权益 ${num(point.equity)}　浮盈亏 ${num(point.unrealized)}`:'设置资金、杠杆后开始回测'}</span></header>
    <svg ref={svg} viewBox={`0 0 ${size.width} ${size.height}`} aria-label="含未平仓盈亏的账户权益曲线" onMouseLeave={()=>setHover(null)} onMouseMove={e=>{if(!points.length)return;const r=e.currentTarget.getBoundingClientRect();const target=from+Math.max(0,Math.min(1,((e.clientX-r.left)-65)/(right-65)))*(to-from);let best=0;for(let i=1;i<points.length;i++)if(Math.abs(points[i].time-target)<Math.abs(points[best].time-target))best=i;setHover(best);}}>
      {points.length>0&&[0,1,2,3].map(i=>{const v=high+spread*.1-spread*1.2*i/3;return <g key={i}><line x1="65" x2={right} y1={y(v)} y2={y(v)} stroke="#2a3545"/><text x="5" y={y(v)+3} fill="#8192a9" fontSize="10">{num(v)}</text></g>;})}
      {!points.length&&<text x={size.width/2} y={size.height/2} textAnchor="middle" fill="#8192a9" fontSize="12">暂无回测结果</text>}
      {!!data&&points.length>0&&<line x1="65" x2={right} y1={y(data.initial_capital)} y2={y(data.initial_capital)} stroke="#64748b" strokeDasharray="4 4"/>}
      {points.length>0&&<polyline fill="none" stroke="#7aabff" strokeWidth="2" vectorEffect="non-scaling-stroke" points={points.map(p=>`${x(p.time)},${y(p.equity)}`).join(' ')}/>}
      {hover!=null&&point&&<circle cx={x(point.time)} cy={y(point.equity)} r="3" fill="#cae0ff"/>}
      <text x="65" y={size.height-5} fill="#8192a9" fontSize="10">{points.length?beijingTime(from):''}</text><text x={right} y={size.height-5} textAnchor="end" fill="#8192a9" fontSize="10">{points.length?beijingTime(to):''}</text>
    </svg>
    <small>{data?'蓝线：账户权益（含浮盈亏）　虚线：初始资金　风险强平为 OHLC 近似，未计资金费率。':'暂无账户回测结果；旧研究记录可重新运行生成资金曲线。'}</small>
  </section>;
}
