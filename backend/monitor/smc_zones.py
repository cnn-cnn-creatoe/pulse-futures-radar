"""Live-only zone transitions; touching is observation, not an entry signal."""
LABELS={'inside':'进入供需区','rejection':'压力区受阻回落','bounce':'支撑区止跌反弹','invalidated':'订单块失效'}

def zone_signals(snapshot,candle,previous):
    signals=[];next_states={}
    for z in snapshot['items']:
        old=previous.get(z['id']);phase=z['phase']
        touched=phase=='inside' or bool(old and old['touched'])
        next_states[z['id']]=dict(phase=phase,touched=touched)
        eligible=phase=='inside' or (phase in ('rejection','bounce') and old and old['touched']) or (phase=='invalidated' and old and old['phase']!='invalidated')
        if not eligible or (old and old['phase']==phase):continue
        direction='short' if z['side']=='supply' else 'long'
        if phase=='invalidated':direction='long' if z['side']=='supply' else 'short'
        signals.append(dict(structure=z['structure'],tag=LABELS[phase],direction=direction,
                            level=z['lower'] if z['side']=='supply' else z['upper'],
                            event_type='zone_'+phase,zone=z))
    return signals,next_states
