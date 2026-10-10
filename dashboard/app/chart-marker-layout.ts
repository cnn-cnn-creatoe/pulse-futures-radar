type Candle = {open:number;close:number;open_time:number;close_time:number};

export function markerBarIndex(bars:Candle[],time:number):number {
 let lo=0,hi=bars.length-1;
 while(lo<=hi){const mid=(lo+hi)>>1;if(bars[mid].open_time<=time)lo=mid+1;else hi=mid-1;}
 return hi>=0&&time<=bars[hi].close_time?hi:-1;
}

export function markerMatchesPattern(action:string,pattern:string|undefined,index:number,bars:Candle[],patterns:string[]):boolean {
 // Exit events are part of the position lifecycle, not new entry candidates.
 if(action==='close_long'||action==='close_short'||!patterns.length)return true;
 const color=(bar:Candle)=>bar.close===bar.open?'':bar.close>bar.open?'阳':'阴';
 const previous=index>0?color(bars[index-1]):'',current=index>=0?color(bars[index]):'';
 return patterns.includes(pattern||(previous&&current?previous+current:''));
}

export function arrowAnchor(edge:number,above:boolean,height:number,x:number,placed:{x:number;ay:number;above:boolean}[]):number {
 const low=14,high=Math.max(low,height-14);
 let ay=Math.max(low,Math.min(high,edge+(above?-14:14)));
 while(placed.some(p=>p.above===above&&Math.abs(p.x-x)<12&&Math.abs(p.ay-ay)<20)){
  const next=ay+(above?-20:20);
  if(next<low||next>high)break;
  ay=next;
 }
 // Dense events may overlap at the boundary, but remain available in navigation.
 return ay;
}
