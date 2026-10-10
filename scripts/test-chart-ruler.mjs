import ts from '../dashboard/node_modules/typescript/lib/typescript.js';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../dashboard/app/chart-ruler.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {measureBars,attachRuler,placeReadout}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const bars=[{open_time:0,volume:10},{open_time:300000,volume:20},{open_time:1800000,volume:30}];
const a={index:0,price:100},b={index:2,price:104};
assert.deepEqual(measureBars(bars,a,b,.25),{delta:4,percent:4,ticks:16,count:2,duration:1800000,volume:60});
const reverse=measureBars(bars,b,a,.25);
assert.equal(reverse.delta,-4);assert.equal(reverse.ticks,-16);assert.equal(reverse.volume,60);assert.equal(reverse.duration,1800000);
assert.equal(measureBars([{open_time:0}],a,a).volume,null);
assert.equal(measureBars(bars,a,a).ticks,null);
assert.equal(measureBars(bars,a,a).volume,10);
// Exercise the actual event handlers, including the browser's final click after
// pointerup. A completed measurement must not disappear on that same click.
const surface=()=>{const listeners=new Map();return {listeners,
 addEventListener(type,fn){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(fn);},
 removeEventListener(type,fn){listeners.get(type)?.delete(fn);},
 emit(type,event){for(const fn of listeners.get(type)||[]){fn(event);if(event.stopped)break;}}
};};
const screen=surface(),host=Object.assign(surface(),{clientWidth:400,clientHeight:300,style:{},
 getBoundingClientRect:()=>({left:0,top:0}),contains:target=>target===host,appendChild(){}});
let readout=null,texts=[],removed=false,finishCount=0,nextFrame=0;
const frames=new Map();
globalThis.requestAnimationFrame=fn=>{frames.set(++nextFrame,fn);return nextFrame;};
globalThis.cancelAnimationFrame=id=>frames.delete(id);
globalThis.window=screen;
const ctx=new Proxy({clearRect(){texts=[];},fillText(text){texts.push(text);},measureText:text=>({width:text.length*5})},{get:(obj,key)=>obj[key]??(()=>{})});
globalThis.document={createElement:()=>({width:0,height:0,getContext:()=>ctx,remove(){removed=true;}})};
const chart={timeScale:()=>({width:()=>300,coordinateToLogical:x=>x/100,logicalToCoordinate:i=>i*100}),panes:()=>[{getHeight:()=>250}]};
const series={coordinateToPrice:y=>200-y,priceToCoordinate:p=>200-p};
const ruler=attachRuler(host,chart,series,bars,2,.25,()=>finishCount++,value=>{readout=value;});
const event=(x=100,y=100,target=host)=>({clientX:x,clientY:y,target,button:0,shiftKey:false,stopped:false,
 preventDefault(){this.prevented=true;},stopImmediatePropagation(){this.stopped=true;}});
const click=(x,y,target=host)=>{
 const down=event(x,y,target);screen.emit('pointerdown',down);if(!down.stopped&&target===host)host.emit('pointerdown',down);
 if(target===host)host.emit('mousedown',event(x,y,target));
 screen.emit('pointerup',event(x,y,target));
 const last=event(x,y,target);if(target===host)host.emit('click',last);return last;
};
const paint=()=>{const callbacks=[...frames.values()];frames.clear();callbacks.forEach(fn=>fn());};
ruler.setEnabled(true);
assert.equal(click(0,100).prevented,true);
assert.equal(finishCount,0,'releasing the first click must keep measuring');
screen.emit('pointermove',event(200,96));paint();
assert.ok(readout.rows[0].includes('4.00 (4.00%)'),'unpressed mouse movement previews the endpoint');
assert.equal(click(200,96).prevented,true,'confirmation must not also select a signal');
assert.equal(finishCount,1);
assert.ok(texts.some(text=>text.includes('成交量')),'measurement readout is drawn inside the chart');
assert.equal(host.style.cursor,'');
assert.ok(readout.rows[0].includes('4.00 (4.00%)'),'confirmation keeps the result');
assert.equal(click(100,110).prevented,undefined,'next chart click remains usable');
assert.equal(readout,null,'next chart click clears the completed drawing');
ruler.setEnabled(true);click(0,100);click(200,96);click(500,350,{});
assert.equal(readout,null,'clicking outside the chart clears the result');
ruler.setEnabled(true);click(0,100);click(500,350,{});
assert.equal(readout,null,'clicking outside also cancels an unfinished measurement');
assert.equal(host.style.cursor,'');
ruler.setEnabled(true);click(0,100);ruler.clear();
assert.equal(readout,null);assert.equal(host.style.cursor,'');
ruler.dispose();assert.equal(removed,true);assert.equal(frames.size,0);
assert.ok([...screen.listeners.values(),...host.listeners.values()].every(set=>set.size===0));
console.log('Ruler: arithmetic, two-click preview/confirmation, outside dismissal, cancellation and cleanup passed.');

const obstacles=[{x:0,y:120,w:600,h:100}];
const overlaps=(a,b)=>a.x<b.x+b.w&&a.x+a.w>b.x&&a.y<b.y+b.h&&a.y+a.h>b.y;
for(const endpoint of [{x:300,y:100},{x:590,y:5},{x:2,y:295}]){
 const box=placeReadout(600,300,170,66,endpoint,obstacles);
 assert.ok(box.x>=4&&box.y>=4&&box.x+box.w<=596&&box.y+box.h<=296,'readout stays inside pane');
 assert.ok(!overlaps(box,obstacles[0]),'readout uses candle-free space');
 assert.ok(!overlaps(box,{x:endpoint.x-22,y:endpoint.y-22,w:44,h:44}),'readout avoids the measurement endpoint');
}
