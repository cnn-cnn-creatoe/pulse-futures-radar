"""Read-only allowlist for the single retained source."""
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
FILES={'wt_extreme_cross':'wavetrend.pine','nan_strategy1_oversold':'nan-strategy1.pine'}

def archived_sources():
    from .community_indicators import CATALOG
    return [dict(id=r['id'],name=r['name'],author=r['author'],url=r['source'],file=FILES[r['id']],kind='indicator') for r in CATALOG]

def source_for(rule_id, registry=None):
    if rule_id not in FILES:
        raise ValueError('该指标策略已移除；仅保留 WaveTrend 超买超卖交叉')
    path=ROOT/'sources/tradingview'/FILES[rule_id]
    return dict(code=path.read_text('utf-8'),filename=path.name,language='Pine',
        note=('用户提供的原始Pine v6；默认参数原样适配，先平后开。横线为参考价，非真实成交。' if rule_id=='nan_strategy1_oversold' else 'LazyBear 原公式；本地仅启用 WT1 < -53 金叉、WT1 > 53 死叉。收盘确认，无原生平仓。'))
