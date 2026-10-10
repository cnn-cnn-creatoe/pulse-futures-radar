import ts from '../dashboard/node_modules/typescript/lib/typescript.js';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const source=readFileSync(new URL('../dashboard/app/chart-marker-layout.ts',import.meta.url),'utf8');
const compiled=ts.transpileModule(source,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText;
const {markerBarIndex,markerMatchesPattern,arrowAnchor}=await import('data:text/javascript;base64,'+Buffer.from(compiled).toString('base64'));
const bars=[{open_time:0,close_time:299999,open:1,close:2},{open_time:300000,close_time:599999,open:2,close:2},{open_time:900000,close_time:1199999,open:2,close:1}];
assert.equal(markerBarIndex(bars,299999),0);
assert.equal(markerBarIndex(bars,300000),1);
assert.equal(markerBarIndex(bars,600000),-1);
assert.equal(markerBarIndex(bars,1200000),-1);
assert.equal(markerMatchesPattern('close_long','阴阴',2,bars,['阳阳']),true);
assert.equal(markerMatchesPattern('buy',undefined,1,bars,['阳阳']),false);
assert.equal(markerMatchesPattern('buy',undefined,1,bars,[]),true);
for(const above of [true,false]){
 const placed=[];
 for(let i=0;i<100;i++){
  const ay=arrowAnchor(above?-20:420,above,400,100,placed);
  assert(ay>=14&&ay<=386);
  placed.push({x:100,ay,above});
 }
}
console.log('Chart marker timestamp, exit, doji and clipping regressions passed');
