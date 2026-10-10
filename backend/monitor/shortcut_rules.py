"""Compatibility endpoint for the single rule shared with the research library."""
def options():
    from .community_indicators import CATALOG
    return [dict(**r,short=r['name'],engine='indicator',family=r['category'],help=r['description']) for r in CATALOG]

def selected():
    return options()

def save(ids):
    if not ids or set(ids)-{r['id'] for r in options()}:
        raise ValueError('仅保留 WaveTrend 超买超卖交叉')
    return selected()

def quant_states(candles,config,items):
    return {}
