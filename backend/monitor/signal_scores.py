"""Causal historical scores for display and per-rule gates; no future evidence."""
from functools import lru_cache
from bisect import bisect_right
from .engine import analyze, LOOKBACK, TIMEFRAME_MS
from .policy import Settings, migrate_saved_settings
import json
from .indicators import indicator_context, _trade_plan, _directional_position
from .actionability import assess, refresh_saved

@lru_cache(maxsize=4)
def _imported_candles(research, symbol, timeframe, cache_epoch):
    merged={}
    for meta in research.datasets():
        if (meta.get('symbol'),meta.get('timeframe'))==(symbol,timeframe):
            imported,_=research.load(meta['id'])
            for c in imported:
                if c.closed:merged.setdefault(c.open_time,c)
    return tuple(sorted(merged.values(),key=lambda c:c.open_time))

@lru_cache(maxsize=1024)
def historical_scores(store, symbol, timeframe, close_time, direction, settings_json, cache_epoch=0, research=None):
    settings=Settings(**migrate_saved_settings(json.loads(settings_json)))
    count=min(20000,max(2020,LOOKBACK[timeframe]['days']*86400000//TIMEFRAME_MS[timeframe]+3))
    bars=store.get_candles(symbol,timeframe,end=close_time,limit=count)
    if research is not None:
        merged={c.open_time:c for c in bars if c.closed and c.close_time<=close_time}
        imported=_imported_candles(research,symbol,timeframe,cache_epoch)
        last=bisect_right(imported,close_time,key=lambda c:c.close_time)
        for c in imported[max(0,last-count):last]:merged.setdefault(c.open_time,c)
        bars=sorted(merged.values(),key=lambda c:c.open_time)[-count:]
    bars=[c for c in bars if c.closed and c.close_time<=close_time]
    if not bars or bars[-1].close_time!=close_time:
        return dict(activity_score=None,score_available_max=None,actionability=None,score_note='缺少信号当时的已收盘K线')
    # Reset at the latest gap, matching signal calculations.
    gap=max((i for i in range(1,len(bars)) if bars[i].open_time!=bars[i-1].close_time+1),default=0)
    bars=bars[gap:]
    saved=store.get_alert(f'{symbol}:{timeframe}:{close_time}:TWO_BAR_VOLATILITY') or {}
    if direction=='record' and saved:
        saved=refresh_saved(saved)
        return dict(activity_score=saved.get('activity_score'),score_available_max=saved.get('score_available_max'),actionability=saved.get('actionability'),score_note='所选列表记录的原始评分快照；该定位点不代表当前指标的买卖信号。')
    if direction=='record':
        return dict(activity_score=None,score_available_max=None,actionability=None,score_note='当前规则在所选记录时点没有信号，且没有保存的记录评分；不套用其他信号的分数。')
    evidence=store.get_market_evidence(symbol,close_time)
    enrichment={k:v for k in ('spread_bps','liquidity_percentile','liquidity_bounds','oi_ratio','oi_change_pct') if (v:=saved.get(k,evidence.get(k))) is not None}
    activity=analyze(bars,settings.engine(),enrichment,score_only=True).get('signal') or {}
    context=indicator_context(bars[-2020:],settings.indicator_config())
    side='up' if direction in ('long','up') else 'down'
    costs=2*(settings.quality.fee_bps_per_side+settings.quality.slippage_bps_per_side)
    plan=_trade_plan(context,side,bars[-1].close,costs) if context.get('status')=='ready' else None
    position=_directional_position(context.get('smc',{}).get('position_assessment'),side)
    score=assess(dict(close_time=close_time,**enrichment),bars,context,plan,position)
    return dict(activity_score=activity.get('activity_score'),score_available_max=activity.get('score_available_max'),actionability=score,
                score_note='按信号时点本地K线及当时已保存盘口重算；正数评分门槛用于筛选，不改变 WaveTrend 原始交叉条件。')
