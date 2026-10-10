'use client';
import {useEffect,useState} from 'react';
import {INDICATOR_SHORTLIST} from './indicator-shortlist';
export type RuleThresholds={min_activity:number;min_actionability:number};
export type RuleSettings=Record<string,RuleThresholds>;
export const RULE_ID='wt_extreme_cross';
const key='pulse-rule-settings-v1';
const empty={min_activity:0,min_actionability:0};
let sessionSettings:RuleSettings|undefined;
function load():RuleSettings{
  if(sessionSettings)return sessionSettings;
  try{const saved=JSON.parse(localStorage.getItem(key)||'{}') as Record<string,Partial<RuleThresholds>>;
    return Object.fromEntries(INDICATOR_SHORTLIST.map(({id})=>{const value=saved[id]||empty;return [id,Object.fromEntries(Object.keys(empty).map(k=>{const v=value[k as keyof RuleThresholds];return [k,typeof v==='number'&&Number.isFinite(v)?Math.min(100,Math.max(0,v)):0];})) as RuleThresholds];}));
  }catch{return Object.fromEntries(INDICATOR_SHORTLIST.map(({id})=>[id,{...empty}]));}
}
export function useRuleSettings(){
  const [settings,setSettings]=useState<RuleSettings>({[RULE_ID]:{...empty}});
  useEffect(()=>{const sync=()=>setSettings(load());sync();window.addEventListener('pulse-rule-settings',sync);const externalSync=()=>{sessionSettings=undefined;sync();};window.addEventListener('storage',externalSync);return()=>{window.removeEventListener('pulse-rule-settings',sync);window.removeEventListener('storage',externalSync);};},[]);
  function update(id:string,value:RuleThresholds){const next={...settings,[id]:value};sessionSettings=next;try{localStorage.setItem(key,JSON.stringify(next));}catch{/* Session-only when storage is unavailable. */}setSettings(next);window.dispatchEvent(new Event('pulse-rule-settings'));}
  return [settings,update] as const;
}
export function passesRuleScores(row:{activity_score?:number|null;actionability?:{score?:number|null}|null},thresholds?:RuleThresholds){
  if(!thresholds)return true;
  return [[row.activity_score,thresholds.min_activity],[row.actionability?.score,thresholds.min_actionability]].every(([score,min])=>!min||(typeof score==='number'&&Number.isFinite(score)&&score>=min));
}
export function RuleSettingsButton({name,value,onChange,disabled=false,scope="local"}:{name:string;value?:RuleThresholds;onChange:(value:RuleThresholds)=>void;disabled?:boolean;scope?:"local"|"telegram"}){
  const [open,setOpen]=useState(false);const current=value||empty;
  return <div className="rule-settings-inline">
    <button type="button" disabled={disabled} aria-label={`${name}设置`} aria-expanded={open} onClick={()=>setOpen(v=>!v)}>⚙ 设置</button>
    <small>活跃度 ≥{current.min_activity} · 可做度 ≥{current.min_actionability}</small>
    {open&&<fieldset><legend>{name} · 筛选设置</legend>{(['min_activity','min_actionability'] as const).map(field=><label key={field}>{field==='min_activity'?'最低活跃度（原分）':'最低可做度'}<select aria-label={field==='min_activity'?'最低活跃度（原分）':'最低可做度'} value={current[field]} onChange={e=>onChange({...current,[field]:Number(e.target.value)})}>{[...new Set([0,35,40,50,60,65,80,90,100,current[field]])].sort((a,b)=>a-b).map(n=><option key={n} value={n}>{n===0?'不限':`≥ ${n}`}</option>)}</select></label>)}<p>两项同时满足；0 不限制。缺失评分不通过正数门槛。{scope==='telegram'?'保存后用于此用户的 TG 推送，其他用户不受影响。':'设置用于本机列表和研究，TG 可在推送设置中单独配置。'}</p></fieldset>}
  </div>;
}
