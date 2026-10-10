 'use client';
import {useState} from 'react';
import {SourcePane} from './source-pane';
import {INDICATOR_SHORTLIST} from './indicator-shortlist';
export function ScriptLibrary({api}:{api:string|null;onlyKind?:string}){
 const [selected,setSelected]=useState(INDICATOR_SHORTLIST[0].id);
 return <><label>源码规则<select aria-label="源码规则" value={selected} onChange={e=>setSelected(e.target.value)}>{INDICATOR_SHORTLIST.map(r=><option key={r.id} value={r.id}>{r.name}</option>)}</select></label><SourcePane api={api} rules={INDICATOR_SHORTLIST.filter(i=>i.id===selected).map(i=>({...i,description:i.help}))} draft={false} onDraft={()=>{}}/></>;
}
