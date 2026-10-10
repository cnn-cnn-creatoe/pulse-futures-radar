'use client';
import {usePickerDismiss} from './picker-dismiss';
const PATTERNS=['阳阳','阴阴','阳阴','阴阳'];
export function PatternPicker({value,onChange,inline=false}:{value:string[];onChange:(v:string[])=>void;inline?:boolean}){
 const ref=usePickerDismiss();
 return <div className={'period-picker'+(inline?' pattern-inline':'')}><details ref={ref}><summary>形态 · {value.length===4?'全部':value.length?value.join(' / '):'无形态'}</summary><div className="period-menu"><b>两 K 形态 · 可多选</b><label><input type="checkbox" checked={value.length===4} onChange={()=>onChange(value.length===4?[]:[...PATTERNS])}/>全部四种形态</label><label><input type="checkbox" checked={!value.length} onChange={()=>onChange([])}/>无形态 · 不按 K 线颜色筛选</label>{PATTERNS.map(p=><div key={p}><label><input type="checkbox" checked={value.includes(p)} onChange={()=>onChange(value.includes(p)?value.filter(x=>x!==p):PATTERNS.filter(x=>x===p||value.includes(x)))}/>{p}</label></div>)}<small>全部：四种组合任一符合；无形态：跳过颜色筛选，按当前指标或策略条件判断。市场异常列表仍保留异常门槛。</small></div></details></div>;
}
