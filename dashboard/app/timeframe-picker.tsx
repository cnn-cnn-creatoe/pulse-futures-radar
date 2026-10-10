 'use client';
import {usePickerDismiss} from './picker-dismiss';
export const PERIODS=[['1m','1 分钟'],['5m','5 分钟'],['15m','15 分钟'],['30m','30 分钟'],['1h','1 小时'],['4h','4 小时'],['1d','1 天'],['1w','1 周'],['1M','1 月']];
export function TimeframePicker({value,onChange,compact=false}:{value:string[];onChange:(v:string[])=>void;compact?:boolean}){
 const selected=value.filter(id=>PERIODS.some(p=>p[0]===id));
 const ref=usePickerDismiss();
 const toggle=(id:string)=>onChange(selected.includes(id)?selected.filter(x=>x!==id):PERIODS.map(p=>p[0]).filter(x=>x===id||selected.includes(x)));
 return <div className="period-picker">{!compact&&PERIODS.slice(0,5).map(([id,label])=><label key={id}><input type="checkbox" checked={selected.includes(id)} onChange={()=>toggle(id)}/>{label}</label>)}<details ref={ref}><summary>选择时间 {compact&&selected.length>0?'· '+selected.map(id=>PERIODS.find(p=>p[0]===id)?.[1]).join(' / '):'⌄'}</summary><div className="period-menu"><b>K 线周期 · 可多选</b>{PERIODS.map(([id,label])=><div key={id}><label><input type="checkbox" checked={selected.includes(id)} onChange={()=>toggle(id)}/>{label}</label></div>)}<small>最多选择 9 个周期。日、周、月线按交易所 UTC 边界收盘。</small></div></details></div>;
}
