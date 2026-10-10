'use client';
import {SmcFields} from './smc-fields';
import {RuleSettingsButton} from './rule-settings';
import {useEffect,useState} from 'react';
export type DeliveryProfile={kind:'indicator'|'quant';rule_id:string;timeframes:string[];min_activity:number;min_actionability:number;patterns:string[];symbols:string[];smc_directions?:string[];smc_events?:string[];smc_content?:string[]};
export type ProfilePreferences={rule_settings?:DeliveryProfile[]|null;event_kind?:'anomaly'|'indicator'|'quant';rules?:string[];timeframes?:string[];min_activity?:number;min_actionability?:number;patterns?:string[]};
const periods=['1m','5m','15m','30m','1h','4h','1d','1w','1M'];
const shapes=['阳阳','阴阴','阳阴','阴阳'];
type Rule={id:string;name:string;description:string};
export function selectedProfiles(value:ProfilePreferences):DeliveryProfile[]{
 if(value.rule_settings!=null)return value.rule_settings.filter(r=>r.kind==='indicator'&&['wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure'].includes(r.rule_id));
 if(value.event_kind!=='indicator'&&value.event_kind!=='quant')return [];
 return (value.rules||[]).filter(id=>['wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure'].includes(id)&&value.event_kind==='indicator').map(rule_id=>({kind:value.event_kind as 'indicator'|'quant',rule_id,timeframes:value.timeframes||['5m'],min_activity:value.min_activity||0,min_actionability:value.min_actionability||0,patterns:value.patterns||[],symbols:[]}));
}
export function IndependentRuleFields({api,value,onChange,smcOnly=false}:{api:string|null;value:ProfilePreferences;onChange:(v:ProfilePreferences)=>void;smcOnly?:boolean}){
 const [catalog,setCatalog]=useState<{indicator:Rule[];quant:Rule[]}>({indicator:[],quant:[]}),[error,setError]=useState(''),[search,setSearch]=useState('');
 const kind='indicator' as const;
 const rows=selectedProfiles(value);
 useEffect(()=>{if(!api)return;const controller=new AbortController();fetch(api+'/api/telegram/rule-catalog',{signal:controller.signal}).then(async r=>{if(!r.ok)throw Error('规则清单读取失败');return await r.json() as {indicator:Rule[];quant:Rule[]};}).then(setCatalog).catch(e=>{if(!controller.signal.aborted)setError(e.message);});return()=>controller.abort();},[api]);
 const change=(next:DeliveryProfile[])=>onChange({...value,rule_settings:next});
 const update=(index:number,patch:Partial<DeliveryProfile>)=>change(rows.map((r,i)=>i===index?{...r,...patch}:r));
 return <section className="rule-delivery" style={{gridColumn:'1 / -1'}}><h3>{smcOnly?'美股 SMC 设置':'指标策略 · 每项独立设置'}</h3><p>{smcOnly?'选择美股合约和周期后保存。SMC 监测结构支撑与压力，盘中突破时提醒。':'先选规则，再在各自卡片里设置条件；任一规则满足自己的全部条件就推送。0分不限制，无形态不限制K线颜色。SMC 使用盘中实时信号；其他规则使用新收盘信号。'}</p>
 {value.rule_settings==null&&value.event_kind==='anomaly'&&<p>原异常提醒暂时保留；添加并保存规则后，改用下方独立规则。</p>}
 <div className="rule-delivery-actions"><b>指标策略研究库</b><input aria-label="搜索推送规则" placeholder="搜索指标或策略" value={search} onChange={e=>setSearch(e.target.value)}/></div>
 {error&&<p role="alert">{error}</p>}<div className="rule-delivery-list" style={{maxHeight:220,overflowY:'auto'}}>{catalog[kind].filter(r=>!smcOnly||r.id==='smc_equity_structure').filter(r=>r.name.toLowerCase().includes(search.toLowerCase())).map(r=>{const chosen=rows.some(p=>p.kind===kind&&p.rule_id===r.id);return <label key={r.id} title={r.description}><input type="checkbox" checked={chosen} onChange={()=>change(chosen?rows.filter(p=>p.kind!==kind||p.rule_id!==r.id):[...rows,{kind,rule_id:r.id,timeframes:r.id==='smc_equity_structure'?['1h','4h','1d']:['5m'],min_activity:0,min_actionability:0,patterns:[],symbols:[]}])}/>{r.name}</label>;})}</div>
 <p>已选 {rows.filter(r=>!smcOnly||r.rule_id==='smc_equity_structure').length} 项，每项条件互不影响。</p>{rows.map((r,i)=>smcOnly&&r.rule_id!=='smc_equity_structure'?null:<fieldset key={r.kind+':'+r.rule_id} style={{border:'1px solid #334155',borderRadius:8,padding:16,marginBottom:12}}><legend>{r.kind==='indicator'?'指标':'策略'}：{catalog[r.kind].find(c=>c.id===r.rule_id)?.name||r.rule_id}</legend>
 <div className="rule-delivery-actions">周期：{periods.map(tf=><label key={tf}><input type="checkbox" checked={r.timeframes.includes(tf)} onChange={()=>update(i,{timeframes:r.timeframes.includes(tf)?r.timeframes.filter(t=>t!==tf):[...r.timeframes,tf]})}/>{tf}</label>)}</div>
 {r.rule_id==='nan_strategy1_oversold'&&<p>本策略的评分和形态门槛只筛选入场；原生平仓提示始终保留。</p>}
 {r.rule_id==='smc_equity_structure'?<>{([['smc_directions','方向',[['long','偏多 / 向上突破'],['short','偏空 / 向下跌破'],['wait','等待确认']]],['smc_events','事件',[['structure','BOS / CHoCH'],['zone_inside','触及区域'],['zone_rejection','压力受阻'],['zone_bounce','支撑反弹'],['zone_invalidated','区域失效']]],['smc_content','辅助内容',[['zones','支撑压力价位 / 区域'],['candles','连续阴阳线'],['volume','相对成交额'],['volatility','波动率 / 追价距离'],['warnings','风险提示']]]] as const).map(([field,label,options])=>{const values=r[field]??(field==='smc_content'?options.map(o=>o[0]):[]);const all=field!=='smc_content'&&!values.length;return <div key={field} className="rule-delivery-actions"><b>{label}：</b><button type="button" onClick={()=>update(i,{[field]:field==='smc_content'?options.map(o=>o[0]):[]})}>全部</button>{field==='smc_content'&&<button type="button" onClick={()=>update(i,{smc_content:[]})}>只发简讯</button>}{options.map(([key,text])=><label key={key}><input type="checkbox" checked={all||values.includes(key)} onChange={()=>{const current=all?options.map(o=>o[0]):values;const next=current.includes(key)?current.filter(v=>v!==key):[...current,key];update(i,{[field]:field!=='smc_content'&&!next.length?[key]:next});}}/>{text}</label>)}</div>;})}<p>方向和事件可多选；辅助内容只决定消息展示。机器人内也可修改个人选项。</p><SmcFields api={api} symbols={r.symbols} onChange={symbols=>update(i,{symbols})}/><button type="button" onClick={()=>change(rows.filter((_,index)=>i!==index))}>移除此项</button></>:<>
 <RuleSettingsButton scope="telegram" name={catalog[r.kind].find(c=>c.id===r.rule_id)?.name||r.rule_id} value={r} onChange={v=>update(i,v)}/>

 <div className="rule-delivery-actions">形态：<label><input type="checkbox" checked={!r.patterns.length} onChange={()=>update(i,{patterns:[]})}/>无形态限制</label>{shapes.map(p=><label key={p}><input type="checkbox" checked={r.patterns.includes(p)} onChange={()=>update(i,{patterns:r.patterns.includes(p)?r.patterns.filter(x=>x!==p):[...r.patterns,p]})}/>{p}</label>)}</div>
 <div className="rule-delivery-actions"><label>币种范围 <select aria-label={`第${i+1}项币种范围`} value={r.symbols.length?'selected':'all'} onChange={e=>update(i,{symbols:e.target.value==='all'?[]:['BTCUSDT']})}><option value="all">全部币种</option><option value="selected">指定单个 / 多个币种</option></select></label>{r.symbols.length>0&&<input aria-label={`第${i+1}项交易对`} style={{minWidth:250}} defaultValue={r.symbols.join(', ')} placeholder="BTCUSDT, ETHUSDT" onBlur={e=>{const symbols=e.target.value.trim().split(/[,，\s]+/).filter(Boolean).map(s=>s.toUpperCase());update(i,{symbols:symbols.length?symbols:['BTCUSDT']});}}/>}<button type="button" onClick={()=>change(rows.filter((_,index)=>i!==index))}>移除此项</button></div>
 </>}
 </fieldset>)}{!smcOnly&&<p>消息示例：BTCUSDT · 5m · 多<br/>时间：2026-09-08 10:24:59（北京时间）<br/>活跃度：65 / 可做度：80<br/>形态：无<br/>指标策略：所选规则名称</p>}</section>;
}
