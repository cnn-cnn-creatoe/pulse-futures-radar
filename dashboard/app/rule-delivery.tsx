'use client';
import {IndependentRuleFields,selectedProfiles,type DeliveryProfile} from './independent-rule-fields';
import {useEffect,useState} from 'react';
import './rule-delivery.css';
import {PatternPicker} from './pattern-picker';
export type RuleDeliveryPreferences={rule_settings?:DeliveryProfile[]|null;event_kind?:'anomaly'|'indicator'|'quant';rules?:string[];patterns?:string[];min_actionability?:number;require_book?:boolean};
type Rule={id:string;name:string;description:string};
type Catalog={indicator:Rule[];quant:Rule[];processed:number;dropped:number;queue:number;last_error:string|null};
export function RuleDeliveryFields({api,value,onChange}:{api:string|null;value:RuleDeliveryPreferences;onChange:(v:RuleDeliveryPreferences)=>void}){
 return <IndependentRuleFields api={api} value={value} onChange={onChange}/>;
}
export function PersonalRuleSettings({api,smcOnly=false,onSaved}:{api:string|null;smcOnly?:boolean;onSaved?:()=>void}){
 const [value,setValue]=useState<RuleDeliveryPreferences&Record<string,unknown>|null>(null),[message,setMessage]=useState(''),[busy,setBusy]=useState(false);
 useEffect(()=>{if(!api)return;const abort=new AbortController();fetch(api+'/api/telegram/settings',{signal:abort.signal}).then(async r=>{if(!r.ok)throw Error('读取个人推送设置失败');return await r.json() as {preferences:RuleDeliveryPreferences&Record<string,unknown>};}).then(r=>{const v=r.preferences;if(smcOnly){const rows=selectedProfiles(v);if(!rows.some(p=>p.rule_id==='smc_equity_structure'))v.rule_settings=[...rows,{kind:'indicator',rule_id:'smc_equity_structure',timeframes:['1h','4h','1d'],symbols:[],patterns:[],min_activity:0,min_actionability:0}];}setValue(v);}).catch(e=>{if(!abort.signal.aborted)setMessage(e.message);});return()=>abort.abort();},[api,smcOnly]);
 const save=async()=>{if(!api||!value)return;setBusy(true);try{const r=await fetch(api+'/api/telegram/settings',{method:'PUT',headers:{'Content-Type':'application/json','X-Pulse-Client':'dashboard'},body:JSON.stringify({preferences:{...value,rule_settings:selectedProfiles(value)}})});if(!r.ok)throw Error('保存失败，请检查门槛和服务');onSaved?.();setMessage('已保存你的个人推送条件。其他用户设置未改变；原信号推送开关保持不变。');}catch(e){setMessage((e as Error).message);}finally{setBusy(false);}};
 return <div style={{gridColumn:"1 / -1",minWidth:0}}>{value&&<IndependentRuleFields smcOnly={smcOnly} api={api} value={value} onChange={v=>setValue(old=>({...old,...v}))}/>}<button className="rule-delivery-save" type="button" disabled={!value||busy} onClick={()=>void save()}>保存个人实时规则</button><p>{message}</p><p>选择币种和周期后保存。Telegram 连接和信号开关在“系统与推送”中管理。</p></div>;
}
