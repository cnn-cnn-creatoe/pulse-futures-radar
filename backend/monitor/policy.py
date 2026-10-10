"""Versioned, reproducible confirmation policy shared by live and backtests."""
from dataclasses import asdict
import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .engine import EngineConfig, TIMEFRAME_MS
from .indicators import IndicatorConfig, evaluate_candidate

THRESHOLD_PRIORS = {'1m': 3.0, '5m': 2.5, '15m': 2.0, '30m': 2.0, '1h': 2.0, '2h': 1.8, '1d': 1.5}
ENGINE_KEYS = tuple(EngineConfig.__dataclass_fields__)

class IndicatorSettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    # An empty list is an explicit raw-price strategy: activity and K-line
    # quality gates still apply, but no technical indicator confirmation is
    # required.  This is already supported by IndicatorConfig/evaluate_candidate.
    enabled_indicators: list[Literal['wt_extreme_cross','nan_strategy1_oversold']] = Field(default_factory=lambda:['wt_extreme_cross'], min_length=0, max_length=2)
    confirmation_mode: Literal['all','any'] = 'all'
    atr_period: int = Field(10,ge=2,le=100)
    atr_multiplier: float = Field(3,ge=.5,le=10)
    atr_method: Literal['wilder','sma'] = 'wilder'
    smc_internal_length: int = Field(5,ge=2,le=30)
    smc_swing_length: int = Field(50,ge=10,le=200)
    structure_max_age: int = Field(3,ge=1,le=20)
    ema_fast_period: int = Field(20,ge=2,le=100)
    ema_slow_period: int = Field(50,ge=3,le=300)

    @model_validator(mode='after')
    def ordered(self):
        if len(set(self.enabled_indicators)) != len(self.enabled_indicators):
            raise ValueError('不能重复选择指标')
        if self.ema_slow_period <= self.ema_fast_period:
            raise ValueError('EMA 慢线周期必须大于快线')
        if self.smc_swing_length <= self.smc_internal_length:
            raise ValueError('波段结构周期必须大于内部结构周期')
        return self

class QualitySettings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    min_activity_score: float = Field(80,ge=0,le=100)
    min_relative_volume: float = Field(1.8,ge=1,le=20)
    min_move_pct: float = Field(.4,ge=.05,le=20)
    min_net_move_pct: float = Field(.25,ge=0,le=20)
    min_liquidity_percentile: float = Field(.4,ge=0,le=1)
    max_spread_bps: float = Field(8,ge=.1,le=100)
    min_risk_reward: float = Field(2,ge=1,le=10)
    fee_bps_per_side: float = Field(5,ge=0,le=100)
    slippage_bps_per_side: float = Field(3,ge=0,le=100)
    require_oi: bool = False
    allow_missing_liquidity: bool = False

class Settings(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    volatility_ratio_min: float = Field(1.5,ge=1.5,le=20)
    baseline_method: Literal['trimmed_mean','simple_mean'] = 'trimmed_mean'
    trim_percent: float = Field(2.5,ge=0,le=10)
    pattern_mode: Literal['all','mixed_only'] = 'all'
    allow_doji: bool = False
    indicators: IndicatorSettings = Field(default_factory=IndicatorSettings)
    quality: QualitySettings = Field(default_factory=QualitySettings)
    threshold_profile: Literal['timeframe','manual'] = 'timeframe'

    def engine(self):
        return EngineConfig(**{key:getattr(self,key) for key in ENGINE_KEYS})

    def indicator_config(self):
        values = self.indicators.model_dump()
        values['enabled_indicators'] = tuple(values['enabled_indicators'])
        return IndicatorConfig(**values)

    def fingerprint(self):
        return hashlib.sha256(json.dumps(self.model_dump(),sort_keys=True,separators=(',',':')).encode()).hexdigest()[:16]

def apply_policy(signal, candles, settings: Settings):
    result = evaluate_candidate(signal,candles,settings.indicator_config(),settings.quality.model_dump())
    # The base 1.5 gate still records raw anomalies. This second gate determines
    # which of those enter the user's strong-signal inbox.
    threshold = max(settings.volatility_ratio_min, THRESHOLD_PRIORS[signal['timeframe']]) if settings.threshold_profile=='timeframe' else settings.volatility_ratio_min
    result['threshold_used'] = threshold
    if signal['volatility_ratio'] < threshold:
        result['qualified'] = False
        result.setdefault('quality_reasons',[]).append(f'波动异常 {signal["volatility_ratio"]:.2f}× 低于本周期门槛 {threshold:g}×')
    result['quality_schema_version'] = 2
    result['policy_hash'] = settings.fingerprint()
    result['policy_snapshot'] = settings.model_dump()
    result['threshold_status'] = 'unvalidated_prior' if settings.threshold_profile=='timeframe' else 'user_defined'
    # Keep one heavy chart payload, not two aliases of the same context.
    result.pop('indicator_context',None)
    return result


def migrate_saved_settings(values):
    """Retired rule ids are pruned only when reading saved policy, never API input."""
    from copy import deepcopy
    values=deepcopy(values)
    indicators=values.get('indicators',{})
    ids=indicators.get('enabled_indicators')
    if isinstance(ids,list):
        indicators['enabled_indicators']=[i for i in ids if i not in ('supertrend','smc','ema')]
    return values
