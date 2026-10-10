"""Subscriber-only SMC filters; never change signal generation."""
DIRECTIONS={'long':'偏多 / 向上突破','short':'偏空 / 向下跌破','wait':'等待确认'}
EVENTS={'structure':'BOS / CHoCH结构','zone_inside':'触及区域','zone_rejection':'压力受阻','zone_bounce':'支撑反弹','zone_invalidated':'区域失效'}
CONTENT={'zones':'支撑压力价位 / 区域','candles':'连续阴阳线','volume':'相对成交额','volatility':'波动率 / 追价距离','warnings':'风险提示'}

def direction(s):
    event=s.get('event_type','structure')
    if s.get('zone_conflict') or event in ('zone_inside','zone_invalidated'):return 'wait'
    if event.startswith('zone_') and not (s.get('zone_context') or {}).get('atr_ready'):return 'wait'
    if event=='zone_rejection':return 'short'
    if event=='zone_bounce':return 'long'
    return s.get('entry_direction') if s.get('entry_direction') in ('long','short') else 'wait'

def matches(signal,profile):
    return ((not profile.get('smc_directions') or direction(signal) in profile['smc_directions'])
        and (not profile.get('smc_events') or (signal.get('event_type') or 'structure') in profile['smc_events']))

def format_message(s,profile,time_text):
    side=direction(s);event=s.get('event_type') or 'structure'
    label={'long':'偏多观察','short':'偏空观察','wait':'等待确认'}[side]
    if event=='structure' and side!='wait':label='向上突破' if side=='long' else '向下跌破'
    if event=='zone_inside':label='触区观察，等待确认'
    fields=profile.get('smc_content',list(CONTENT))
    lines=[f"{s.get('symbol')} · {s.get('timeframe')} · {label}",f"SMC：{s.get('signal_kind')}",f"触发价：{s.get('signal_price')}",f"触发时间：{time_text(s.get('observed_at'))}（北京时间）"]
    z=s.get('zone')
    if 'zones' in fields:
        if z:
            lo,hi=z['lower'],z['upper']
            if side=='short':lo,hi=hi,lo
            lines.append(('供给压力区' if z['side']=='supply' else '需求支撑区')+f'：{lo} — {hi}')
        elif s.get('structure_level') is not None:lines.append(f"结构价位：{s['structure_level']}")
    a=s.get('assessment') or {}
    if a.get('ready'):
        if 'candles' in fields and a.get('streak'):lines.append(f"前序已收盘：连续{a['streak']}根{a.get('streak_side','')}线")
        if 'volume' in fields and a.get('volume_ratio') is not None:lines.append(f"前一根相对成交额：{a['volume_ratio']:.2f}倍（已收盘）")
        if 'volatility' in fields:
            if a.get('atr_pct') is not None:lines.append(f"ATR14 / 价格：{a['atr_pct']:.2f}%")
            if a.get('extension_atr') is not None:lines.append(f"距结构位：{a['extension_atr']:.2f} ATR")
        if 'warnings' in fields:lines.extend('风险：'+v for v in a.get('warnings',[]))
    if s.get('zone_conflict'):lines.append('区域冲突：'+s['zone_conflict'])
    lines.append('盘中观察，未收盘；不等于开仓指令。' if s.get('intrabar',True) else '本根已收盘；不等于开仓指令。')
    return '\n'.join(lines)
