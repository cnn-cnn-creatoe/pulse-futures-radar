import type {IChartApi,ISeriesApi,Logical} from 'lightweight-charts';
export type RulerBar={open_time:number;volume?:number;high?:number;low?:number};
type Box={x:number;y:number;w:number;h:number};
export function placeReadout(width:number,height:number,w:number,h:number,endpoint:{x:number;y:number},obstacles:Box[]){
 const overlap=(a:Box,b:Box)=>Math.max(0,Math.min(a.x+a.w,b.x+b.w)-Math.max(a.x,b.x))*Math.max(0,Math.min(a.y+a.h,b.y+b.h)-Math.max(a.y,b.y));
 const clamp=(x:number,y:number)=>({x:Math.max(4,Math.min(width-w-4,x)),y:Math.max(4,Math.min(height-h-4,y)),w,h});
 const candidates=[clamp(endpoint.x+24,endpoint.y-h-24),clamp(endpoint.x-w-24,endpoint.y-h-24),clamp(endpoint.x+24,endpoint.y+24),clamp(endpoint.x-w-24,endpoint.y+24)];
 for(let y=4;y<=Math.max(4,height-h-4);y+=Math.max(20,h/2))for(let x=4;x<=Math.max(4,width-w-4);x+=Math.max(40,w/2))candidates.push(clamp(x,y));
 candidates.push(clamp(width-w-4,4),clamp(width-w-4,height-h-4));
 const cursor={x:endpoint.x-22,y:endpoint.y-22,w:44,h:44};
 const score=(b:Box)=>overlap(b,cursor)*100000+obstacles.reduce((n,o)=>n+overlap(b,o),0)*100+Math.hypot(b.x+w/2-endpoint.x,b.y+h/2-endpoint.y);
 return candidates.reduce((best,b)=>score(b)<score(best)?b:best);
}
type Point={index:number;price:number};
export type RulerReadout={rows:string[];negative:boolean};
export function measureBars(bars:RulerBar[],a:Point,b:Point,tick?:number){
 const low=Math.min(a.index,b.index),high=Math.max(a.index,b.index),interval=bars.slice(low,high+1);
 const delta=b.price-a.price;
 return {delta,percent:a.price?delta/a.price*100:null,ticks:tick&&tick>0?Math.round(delta/tick):null,
  count:high-low,duration:Math.abs(bars[b.index].open_time-bars[a.index].open_time),
  volume:interval.every(c=>Number.isFinite(c.volume))?interval.reduce((n,c)=>n+c.volume!,0):null};
}
const durationText=(ms:number)=>{const minutes=Math.floor(ms/60000),days=Math.floor(minutes/1440),hours=Math.floor(minutes%1440/60);return [days?days+'天':'',hours?hours+'小时':'',minutes%60?minutes%60+'分钟':''].filter(Boolean).join(' ')||'0分钟';};
export function attachRuler(host:HTMLDivElement,chart:IChartApi,series:ISeriesApi<'Candlestick'>,bars:RulerBar[],precision:number,tick?:number,onFinish?:()=>void,onReadout?:(value:RulerReadout|null)=>void){
 const canvas=document.createElement('canvas');canvas.className='chart-ruler-overlay';host.appendChild(canvas);
 let enabled=false,selecting=false,pressed=false,start:Point|null=null,end:Point|null=null,frame=0,disposed=false,suppressClick=false;
 let readoutKey='';
 const publish=(value:RulerReadout|null)=>{const key=JSON.stringify(value);if(key!==readoutKey){readoutKey=key;onReadout?.(value);}};
 const point=(e:PointerEvent):Point|null=>{const box=host.getBoundingClientRect(),x=e.clientX-box.left,y=e.clientY-box.top;
  if(!bars.length||x<0||y<0||x>=chart.timeScale().width()||y>=chart.panes()[0].getHeight())return null;
  const index=chart.timeScale().coordinateToLogical(x),price=series.coordinateToPrice(y);
  return index==null||price==null?null:{index:Math.max(0,Math.min(bars.length-1,Math.round(index))),price:tick&&tick>0?Math.round(price/tick)*tick:price};};
 const draw=()=>{if(disposed)return;const width=host.clientWidth,height=host.clientHeight,dpr=window.devicePixelRatio||1;
  if(canvas.width!==Math.round(width*dpr)||canvas.height!==Math.round(height*dpr)){canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);}
  const ctx=canvas.getContext('2d');if(!ctx)return;ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,width,height);
  if(!start||!end){publish(null);return;}
  const x1=chart.timeScale().logicalToCoordinate(start.index as Logical),x2=chart.timeScale().logicalToCoordinate(end.index as Logical),y1=series.priceToCoordinate(start.price),y2=series.priceToCoordinate(end.price);
  if(x1===null||x2===null||y1===null||y2===null)return;
  const paneWidth=chart.timeScale().width(),paneHeight=chart.panes()[0].getHeight();
  ctx.save();ctx.beginPath();ctx.rect(0,0,paneWidth,paneHeight);ctx.clip();
  const left=Math.min(x1,x2),top=Math.min(y1,y2),w=Math.abs(x2-x1),h=Math.abs(y2-y1),cx=(x1+x2)/2,cy=(y1+y2)/2;
  const m=measureBars(bars,start,end,tick),color=m.delta<0?'#f7525f':'#2962ff';
  ctx.fillStyle=color+'26';ctx.fillRect(left,top,w,h);ctx.strokeStyle=color;ctx.lineWidth=1;
  const arrow=(a:number,b:number,c:number,d:number)=>{ctx.beginPath();ctx.moveTo(a,b);ctx.lineTo(c,d);ctx.stroke();const angle=Math.atan2(d-b,c-a);ctx.beginPath();ctx.moveTo(c-7*Math.cos(angle-.6),d-7*Math.sin(angle-.6));ctx.lineTo(c,d);ctx.lineTo(c-7*Math.cos(angle+.6),d-7*Math.sin(angle+.6));ctx.stroke();};
  arrow(x1,cy,x2,cy);arrow(cx,y1,cx,y2);
  const rows=[`${m.delta.toFixed(precision)} (${m.percent==null?'—':m.percent.toFixed(2)+'%'}) ${m.ticks==null?'跳动点数待验证':m.ticks.toLocaleString()}`,
   `${m.count} 根 K 线，${durationText(m.duration)}`,
   `成交量 ${m.volume==null?'缺失':new Intl.NumberFormat('en',{notation:'compact',maximumFractionDigits:2}).format(m.volume)}`];
  ctx.font='600 12px sans-serif';const bw=Math.min(paneWidth-8,Math.max(...rows.map(s=>ctx.measureText(s).width))+24),bh=66;
  const range=chart.timeScale().getVisibleLogicalRange?.();
  const obstacles:Box[]=[];
  for(let i=Math.max(0,Math.floor(range?.from??0));i<=Math.min(bars.length-1,Math.ceil(range?.to??bars.length-1));i++){
   const b=bars[i],x=chart.timeScale().logicalToCoordinate(i as Logical);
   if(x==null||x<0||x>paneWidth||b.high==null||b.low==null)continue;
   const high=series.priceToCoordinate(b.high),low=series.priceToCoordinate(b.low);
   if(high!=null&&low!=null)obstacles.push({x:x-8,y:high-18,w:16,h:Math.max(1,low-high)+36});
  }
  const box=placeReadout(paneWidth,paneHeight,bw,bh,{x:x2,y:y2},obstacles);
  ctx.fillStyle=color;ctx.beginPath();ctx.roundRect(box.x,box.y,bw,bh,5);ctx.fill();ctx.fillStyle='#fff';ctx.textAlign='center';
  rows.forEach((text,i)=>ctx.fillText(text,box.x+bw/2,box.y+18+i*20,bw-12));
  publish({rows,negative:m.delta<0});ctx.restore();
  // TradingView highlights both endpoint coordinates on the axes in blue.
  ctx.font='12px sans-serif';ctx.textAlign='center';
  const priceYs=Math.abs(y2-y1)<20?[Math.min(y1,y2)-10,Math.min(y1,y2)+10]:[y1,y2];
  [start,end].forEach((p,i)=>{const py=Math.max(10,Math.min(paneHeight-10,priceYs[i]));ctx.fillStyle='#2962ff';ctx.fillRect(paneWidth,py-10,width-paneWidth,20);ctx.fillStyle='#fff';ctx.fillText(p.price.toFixed(precision),paneWidth+(width-paneWidth)/2,py+4,width-paneWidth-4);});
  const stamp=(index:number)=>new Intl.DateTimeFormat('zh-CN',{timeZone:'Asia/Shanghai',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(bars[index].open_time);
  const axisLabels=[stamp(start.index),stamp(end.index)],labelWidth=Math.max(...axisLabels.map(s=>ctx.measureText(s).width))+12;
  const labelXs=[Math.max(labelWidth/2,Math.min(paneWidth-labelWidth/2,x1)),Math.max(labelWidth/2,Math.min(paneWidth-labelWidth/2,x2))];
  if(Math.abs(labelXs[0]-labelXs[1])<labelWidth){const mid=Math.max(labelWidth,Math.min(paneWidth-labelWidth,(labelXs[0]+labelXs[1])/2));labelXs[0]=mid-labelWidth/2;labelXs[1]=mid+labelWidth/2;}
  axisLabels.forEach((s,i)=>{ctx.fillStyle='#2962ff';ctx.fillRect(labelXs[i]-labelWidth/2,paneHeight,labelWidth,22);ctx.fillStyle='#fff';ctx.fillText(s,labelXs[i],paneHeight+15);});
 };
 const loop=()=>{draw();if(start&&!disposed)frame=requestAnimationFrame(loop);};
 const finish=()=>{selecting=false;enabled=false;host.style.cursor='';onFinish?.();};
 const resetDrawing=()=>{pressed=false;suppressClick=false;start=end=null;cancelAnimationFrame(frame);draw();};
 const clear=()=>{const active=enabled||selecting;selecting=false;enabled=false;host.style.cursor='';resetDrawing();if(active)onFinish?.();};
 // The confirmation click must survive until its click event has been consumed.
 // Clear a finished result only on the NEXT press, anywhere in the page.
 const outside=(e:PointerEvent)=>{if(start&&!selecting){clear();return;}if((enabled||selecting)&&(!host.contains(e.target as Node)||!point(e)))clear();};
 const down=(e:PointerEvent)=>{if(e.button!==0||(!enabled&&!selecting&&!e.shiftKey))return;
  const p=point(e);if(!p)return;e.preventDefault();e.stopImmediatePropagation();pressed=true;suppressClick=true;
  if(selecting){end=p;finish();draw();}
  else{selecting=true;start=p;end=p;host.style.cursor='crosshair';cancelAnimationFrame(frame);loop();}};
 const move=(e:PointerEvent)=>{if(!selecting)return;const p=point(e);if(!p)return;end=p;e.preventDefault();e.stopImmediatePropagation();};
 // Releasing the first click leaves the ruler active; the second click confirms.
 const up=(e:PointerEvent)=>{if(!pressed)return;pressed=false;e.preventDefault();e.stopImmediatePropagation();};
 const mouse=(e:MouseEvent)=>{if(suppressClick||enabled||selecting||e.shiftKey){if(e.type==='click')suppressClick=false;e.preventDefault();e.stopImmediatePropagation();}};
 host.addEventListener('pointerdown',down,true);host.addEventListener('mousedown',mouse,true);host.addEventListener('click',mouse,true);
 window.addEventListener('pointerdown',outside,true);window.addEventListener('pointermove',move,true);window.addEventListener('pointerup',up,true);window.addEventListener('pointercancel',clear,true);window.addEventListener('blur',clear);
 return {setEnabled:(v:boolean)=>{selecting=false;resetDrawing();enabled=v;host.style.cursor=v?'crosshair':'';},clear,
  dispose:()=>{disposed=true;cancelAnimationFrame(frame);host.removeEventListener('pointerdown',down,true);host.removeEventListener('mousedown',mouse,true);host.removeEventListener('click',mouse,true);window.removeEventListener('pointerdown',outside,true);window.removeEventListener('pointermove',move,true);window.removeEventListener('pointerup',up,true);window.removeEventListener('pointercancel',clear,true);window.removeEventListener('blur',clear);canvas.remove();}};
}
