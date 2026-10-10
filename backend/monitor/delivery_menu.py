"""Small private-chat editor for independent rule subscriptions."""
import hashlib
from copy import deepcopy
from .delivery_profiles import profiles,normalize_profiles
from .selected_period_rules import TIMEFRAMES
from .smc_delivery import DIRECTIONS,EVENTS,CONTENT

def token(kind,rule):return hashlib.sha256(f'{kind}:{rule}'.encode()).hexdigest()[:12]

def catalogue():
    from .live_rule_signals import rule_catalog
    return {token(k,r['id']):dict(kind=k,rule_id=r['id'],name=r['name']) for k in ('indicator','quant') for r in rule_catalog(k)}

def button(text,data):return dict(text=text,callback_data=data)

def overview(p):
    rows=[[button('选择指标策略','q:list:indicator:0')]]
    names=catalogue()
    for r in profiles(p):
        t=token(r['kind'],r['rule_id']);name=names.get(t,{}).get('name',r['rule_id'])
        rows.append([button(name+' · '+('/'.join(r['timeframes'])),f'q:open:{t}')])
    rows.append([button('信号：'+('开启' if p['signals_enabled'] else '暂停'),'p:signals'),button('新上架：'+('开启' if p['listing_enabled'] else '关闭'),'p:list')])
    return dict(inline_keyboard=rows)

async def show(service,chat_id,message_id,text,rows):
    if service._client is None:return
    await service._rate_wait(chat_id)
    args=dict(chat_id=chat_id,text=text,reply_markup=dict(inline_keyboard=rows))
    if isinstance(message_id,int):args['message_id']=message_id
    await service._client.call('editMessageText' if isinstance(message_id,int) else 'sendMessage',args)

def persist(service,member,p):
    p=service.state.update_preferences(p,chat_id=member['chat_id'])
    if member['role']=='admin':service.config.update(preferences=p)

def equity_choices(service):
    provider=getattr(service,'equity_symbols',None)
    return sorted(set(provider())) if callable(provider) else []

async def equity_picker(service,member,p,items,r,t,parts,message_id):
    available=equity_choices(service)
    # Saved unavailable contracts remain removable, but are never added by all.
    choices=sorted(set(available)|set(r['symbols']))
    op=parts[1];page=int(parts[-1]) if parts[-1].isdigit() else 0
    changed=False
    if op=='eqall' and available:r['symbols']=list(available);changed=True
    elif op=='eqclear':r['symbols']=[];changed=True
    elif op=='eqcoin' and len(parts)==5:
        symbol=next((v for v in choices if token('equity',v)==parts[3]),None)
        if symbol:
            if symbol in r['symbols']:r['symbols'].remove(symbol);changed=True
            elif symbol in available:r['symbols'].append(symbol);changed=True
    if changed:p['rule_settings']=normalize_profiles(items);persist(service,member,p)
    pages=max(1,(len(choices)+11)//12);page=min(page,pages-1)
    rows=[[button('全选美股（当前名单）',f'q:eqall:{t}:{page}'),button('清空选择（暂停）',f'q:eqclear:{t}:{page}')]]
    chunk=choices[page*12:page*12+12]
    for i in range(0,len(chunk),2):
        rows.append([button(('✓ ' if v in r['symbols'] else '▫ ')+v+('（不可用）' if v not in available else ''),f'q:eqcoin:{t}:{token("equity",v)}:{page}') for v in chunk[i:i+2]])
    rows.append([button('上一页',f'q:eqpage:{t}:{max(0,page-1)}'),button(f'{page+1}/{pages} 下一页',f'q:eqpage:{t}:{min(pages-1,page+1)}')])
    rows.append([button('手动输入美股代码',f'q:eqinput:{t}')])
    rows.append([button('返回SMC推送设置',f'q:open:{t}')])
    text=f"选择美股代币 · 已选 {len(r['symbols'])} 个 / 当前可选 {len(available)} 个\n点击合约勾选或取消，立即保存，仅影响本人。\n全选选择当前名单，不自动加入未来新上市合约。"
    if not available:text+='\n合约目录尚未就绪，请稍后重新打开；已保存名单保留。'
    await show(service,member['chat_id'],message_id,text,rows)

async def handle(service,member,data,message_id):
    parts=data.split(':');p=deepcopy(member['preferences']);items=deepcopy(profiles(p));names=catalogue();chat_id=member['chat_id']
    if len(parts)==4 and parts[1]=='list' and parts[2] in ('indicator','quant') and parts[3].isdigit():
        values=[(t,r) for t,r in names.items() if r['kind']==parts[2]];page=min(int(parts[3]),max(0,(len(values)-1)//6))
        rows=[[button(r['name'],f'q:open:{t}')] for t,r in values[page*6:page*6+6]]
        rows.append([button('上一页',f'q:list:{parts[2]}:{max(0,page-1)}'),button('下一页',f'q:list:{parts[2]}:{page+1}')]);rows.append([button('返回','p:refresh')])
        await show(service,chat_id,message_id,'选择指标或策略，打开后可添加并独立设置。',rows);return
    if len(parts)<3 or parts[2] not in names:return
    t=parts[2];ref=names[t];index=next((i for i,r in enumerate(items) if token(r['kind'],r['rule_id'])==t),None)
    r=items[index] if index is not None else dict(kind=ref['kind'],rule_id=ref['rule_id'],timeframes=['1h','4h','1d'] if ref['rule_id']=='smc_equity_structure' else ['5m'],min_activity=0,min_actionability=0,patterns=[],symbols=[])
    operation=parts[1]
    if index is not None and r['rule_id']=='smc_equity_structure' and operation=='eqinput':
        await show(service,chat_id,message_id,f"手动输入美股代币\n复制下面命令，把代码改成你要的：\n/coins {t} NVDA MU TSLA\n支持大小写、空格或逗号分隔，也支持完整代码 NVDAUSDT。\n全部识别成功后替换你的SMC名单；任何代码无法识别都会保留原名单。",[[button('返回选择美股代币',f'q:eqpage:{t}:0')]])
        return
    if index is not None and r['rule_id']=='smc_equity_structure' and operation in ('eqpage','eqall','eqclear','eqcoin'):
        await equity_picker(service,member,p,items,r,t,parts,message_id);return
    if operation=='add' and index is None:items.append(r);index=len(items)-1
    elif operation=='remove' and index is not None:
        items.pop(index);p['rule_settings']=items;persist(service,member,p);await service._send_menu(chat_id,edit_message_id=message_id);return
    elif index is not None:
        if operation=='tf' and len(parts)==4 and parts[3] in TIMEFRAMES:
            tf=parts[3]
            if tf in r['timeframes']:
                if len(r['timeframes'])>1:r['timeframes'].remove(tf)
            else:r['timeframes'].append(tf)
        elif operation in ('act','action') and r['rule_id']!='smc_equity_structure':
            field='min_activity' if operation=='act' else 'min_actionability';r[field]=next((v for v in (0,35,50,65,80,90,100) if v>r[field]),0)
        elif operation=='shape' and len(parts)==4 and r['rule_id']!='smc_equity_structure':
            if parts[3]=='none':r['patterns']=[]
            elif parts[3] in ('0','1','2','3'):
                v=('阳阳','阴阴','阳阴','阴阳')[int(parts[3])]
                r['patterns'].remove(v) if v in r['patterns'] else r['patterns'].append(v)
        elif operation in ('sd','se','sc') and len(parts)==4 and r['rule_id']=='smc_equity_structure':
            field,options,default={'sd':('smc_directions',DIRECTIONS,[]),'se':('smc_events',EVENTS,[]),'sc':('smc_content',CONTENT,list(CONTENT))}[operation]
            values=list(r.get(field,default));v=parts[3]
            if v=='all':values=list(options) if operation=='sc' else []
            elif v=='none' and operation=='sc':values=[]
            elif v in options:
                if operation!='sc' and not values:values=list(options)
                values.remove(v) if v in values else values.append(v)
                # Direction/event filters always retain at least one selection.
                if not values and operation!='sc':values=[v]
            r[field]=values
        elif operation=='allcoins':r['symbols']=[]
    if operation!='open' and index is not None:p['rule_settings']=normalize_profiles(items);persist(service,member,p)
    rows=[]
    if index is None:rows.append([button('添加这项规则',f'q:add:{t}')])
    else:
        rows.extend([[button(('✓ ' if tf in r['timeframes'] else '')+tf,f'q:tf:{t}:{tf}') for tf in TIMEFRAMES[i:i+3]] for i in range(0,len(TIMEFRAMES),3)])
        if r['rule_id']!='smc_equity_structure':
            rows.append([button(f"活跃度 ≥{r['min_activity']}",f'q:act:{t}'),button(f"可做度 ≥{r['min_actionability']}",f'q:action:{t}')])
            rows.append([button('无形态限制',f'q:shape:{t}:none')])
            rows.append([button(('✓ ' if s in r['patterns'] else '')+s,f'q:shape:{t}:{i}') for i,s in enumerate(('阳阳','阴阴','阳阴','阴阳'))])
        else:
            for op,field,options,default,label in [('sd','smc_directions',DIRECTIONS,[],'方向'),('se','smc_events',EVENTS,[],'事件'),('sc','smc_content',CONTENT,list(CONTENT),'辅助内容')]:
                chosen=r.get(field,default)
                rows.append([button(label+'：全部',f'q:{op}:{t}:all')]+([button('只发简讯',f'q:{op}:{t}:none')] if op=='sc' else []))
                for value,name in options.items():
                    selected=value in chosen or (op!='sc' and not chosen)
                    rows.append([button(('✓ ' if selected else '')+name,f'q:{op}:{t}:{value}')])
        if r['rule_id']=='smc_equity_structure':rows.append([button(f"选择美股代币（已选 {len(r['symbols'])}）",f'q:eqpage:{t}:0'),button('全选美股',f'q:eqall:{t}:0')])
        if r['rule_id']=='smc_equity_structure':rows.append([button('手动输入美股代码',f'q:eqinput:{t}')])
        rows.append([button('清空名单（暂停）' if r['rule_id']=='smc_equity_structure' else '全部币种',f'q:allcoins:{t}'),button('移除此项',f'q:remove:{t}')])
    rows.append([button('返回规则列表','p:refresh')])
    text=f"{ref['name']}\n周期：{' / '.join(r['timeframes'])}\n活跃度 ≥{r['min_activity']} / 可做度 ≥{r['min_actionability']}\n形态：{' / '.join(r['patterns']) or '无形态限制'}\n币种：{', '.join(r['symbols']) or ('未选择（暂停）' if r['rule_id']=='smc_equity_structure' else '全部')}"
    if r['rule_id']=='smc_equity_structure':
        text=f"{ref['name']}\n周期：{' / '.join(r['timeframes'])}\n已选 {len(r['symbols'])} 个合约。方向、事件可多选；辅助内容只控制消息展示。\n点击即保存，仅影响你自己的推送。"
    if index is not None:
        example='NVDA MU TSLA' if r['rule_id']=='smc_equity_structure' else 'BTCUSDT ETHUSDT'
        text+=f'\n手动替换名单，发送：\n/coins {t} {example}\n各规则条件独立。'
    await show(service,chat_id,message_id,text,rows)

async def coins(service,member,argument):
    parts=(argument or '').replace(',',' ').replace('，',' ').split()
    p=deepcopy(member['preferences']);items=deepcopy(profiles(p))
    r=next((r for r in items if parts and token(r['kind'],r['rule_id'])==parts[0]),None)
    if r is None or len(parts)<2:await service._send_text(member['chat_id'],'请打开规则卡片，复制其中的 /coins 命令并填写交易对。');return
    if parts[1:] in (['all'],['全部']) and r['rule_id']=='smc_equity_structure':
        values=equity_choices(service)
        if not values:
            await service._send_text(member['chat_id'],'合约目录尚未就绪，已保留原名单，请稍后重试。');return
        r['symbols']=values
    elif r['rule_id']=='smc_equity_structure':
        available=set(equity_choices(service))
        if not available:
            await service._send_text(member['chat_id'],'合约目录尚未就绪，已保留原名单，请稍后重试。');return
        chosen=[];unknown=[]
        for raw in parts[1:]:
            value=raw.upper()
            match=value if value in available else value+'USDT' if value+'USDT' in available else None
            if match:chosen.append(match)
            else:unknown.append(raw)
        if unknown:
            await service._send_text(member['chat_id'],'未识别为当前可用美股合约：'+', '.join(unknown)+'。请检查代码；原名单未修改。');return
        r['symbols']=list(dict.fromkeys(chosen))
    else:r['symbols']=[] if parts[1:] in (['all'],['全部']) else parts[1:]
    try:p['rule_settings']=normalize_profiles(items)
    except ValueError as e:await service._send_text(member['chat_id'],str(e));return
    persist(service,member,p)
    if r['rule_id']=='smc_equity_structure':await service._send_text(member['chat_id'],'已保存 '+str(len(r['symbols']))+' 个美股合约：'+', '.join(r['symbols']))
    await handle(service,member={**member,'preferences':p},data='q:open:'+parts[0],message_id=None)
