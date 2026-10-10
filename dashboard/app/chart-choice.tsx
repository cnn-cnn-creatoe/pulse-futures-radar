'use client';
import {usePickerDismiss} from './picker-dismiss';
export function ChartChoice({label,value,options,disabled,onChange}:{label:string;value:string;options:{id:string;name:string}[];disabled?:boolean;onChange:(value:string)=>void}){
 const ref=usePickerDismiss();
 return <details ref={ref} className="chart-choice"><summary onClick={e=>{if(disabled)e.preventDefault();}} aria-disabled={disabled}>{label} · {options.find(o=>o.id===value)?.name||value}</summary><div className="chart-choice-options" role="group" aria-label={label}>{options.map(o=><button type="button" key={o.id} disabled={disabled} aria-pressed={value===o.id} onClick={()=>{if(ref.current)ref.current.open=false;onChange(o.id);}}>{o.name}</button>)}</div></details>;
}
