"""Independent per-rule Telegram filters, stored in each subscriber's preferences."""
import math
import re
from .selected_period_rules import TIMEFRAMES


def normalize_profiles(value):
    if value is None:return None  # Read legacy subscriptions without changing them.
    if not isinstance(value,list):raise ValueError('规则设置必须是列表')
    from .live_rule_signals import rule_catalog
    allowed={k:{r['id'] for r in rule_catalog(k)} for k in ('indicator','quant')}
    result=[];seen=set()
    for row in value:
        if not isinstance(row,dict):raise ValueError('规则设置格式错误')
        kind=row.get('kind');rule=row.get('rule_id')
        if kind not in allowed or rule not in allowed[kind]:raise ValueError('请选择已接入的指标或策略')
        key=(kind,rule)
        if key in seen:raise ValueError('同一规则请在一张卡片中选择多个周期')
        seen.add(key)
        tf=row.get('timeframes');patterns=row.get('patterns',[]);symbols=row.get('symbols',[])
        if not isinstance(tf,list) or not tf or any(t not in TIMEFRAMES for t in tf):raise ValueError('每条规则至少选择一个有效周期')
        if not isinstance(patterns,list) or any(p not in ('阳阳','阴阴','阳阴','阴阳') for p in patterns):raise ValueError('形态选择无效')
        if not isinstance(symbols,list) or any(not isinstance(s,str) or not re.fullmatch(r'[\w.-]{1,50}',s.strip(),re.UNICODE) for s in symbols):raise ValueError('币种请填写交易对名称，例如 BTCUSDT')
        scores={}
        for field in ('min_activity','min_actionability'):
            v=row.get(field,0)
            if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=100:raise ValueError('分数门槛应为0至100')
            scores[field]=v
        extra={}
        if rule == 'smc_equity_structure':
            from .smc_delivery import DIRECTIONS,EVENTS,CONTENT
            for field,allowed,default in [('smc_directions',DIRECTIONS,[]),('smc_events',EVENTS,[]),('smc_content',CONTENT,list(CONTENT))]:
                values=row.get(field,default)
                if not isinstance(values,list) or any(v not in allowed for v in values):raise ValueError('SMC推送选项无效')
                extra[field]=list(dict.fromkeys(values))
            if patterns or any(scores.values()):raise ValueError('SMC盘中结构不使用形态或评分门槛')
        result.append(dict(kind=kind,rule_id=rule,timeframes=[t for t in TIMEFRAMES if t in tf],patterns=list(dict.fromkeys(patterns)),symbols=list(dict.fromkeys(s.strip().upper() for s in symbols)),**scores,**extra))
    return result


def profiles(preferences):
    saved=preferences.get('rule_settings')
    if saved is not None:return [r for r in saved if r.get('kind')=='indicator' and r.get('rule_id') in ('wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure')]
    kind=preferences.get('event_kind','anomaly')
    if kind not in ('indicator','quant'):return []
    return [dict(kind=kind,rule_id=r,timeframes=preferences['timeframes'],patterns=preferences.get('patterns',[]),symbols=[],min_activity=preferences['min_activity'],min_actionability=preferences.get('min_actionability',0)) for r in preferences.get('rules',[]) if kind=='indicator' and r in ('wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure')]
