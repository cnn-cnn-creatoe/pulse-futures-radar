'use client';
import { WorkspaceTabs } from './workspace-tabs';
import { useEffect, useState } from 'react';
import {
  Bell,
  Check,
  FlaskConical,
  Layers3,
  RefreshCw,
  ShieldCheck,
  Upload,
  Download,
  Activity,
  AlertTriangle,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';

import {
  CATEGORIES,
  categoryLabel,
  contractLabel,
  matchesCategory,
  quoteOf,
  type ContractMeta,
} from './contract-meta';

export type AdvancedSettings = {
  threshold_profile: 'timeframe' | 'manual';
  indicators: {
    enabled_indicators: string[];
    confirmation_mode: 'all' | 'any';
    atr_period: number;
    atr_multiplier: number;
    atr_method: 'wilder' | 'sma';
    smc_internal_length: number;
    smc_swing_length: number;
    structure_max_age: number;
    ema_fast_period: number;
    ema_slow_period: number;
  };
  quality: {
    min_activity_score: number;
    min_relative_volume: number;
    min_move_pct: number;
    min_net_move_pct: number;
    min_liquidity_percentile: number;
    max_spread_bps: number;
    min_risk_reward: number;
    fee_bps_per_side: number;
    slippage_bps_per_side: number;
    require_oi: boolean;
    allow_missing_liquidity: boolean;
  };
};
export const ADVANCED_DEFAULTS: AdvancedSettings = {
  threshold_profile: 'timeframe',
  indicators: {
    enabled_indicators: ['wt_extreme_cross'],
    confirmation_mode: 'all',
    atr_period: 10,
    atr_multiplier: 3,
    atr_method: 'wilder',
    smc_internal_length: 5,
    smc_swing_length: 50,
    structure_max_age: 3,
    ema_fast_period: 20,
    ema_slow_period: 50,
  },
  quality: {
    min_activity_score: 80,
    min_relative_volume: 1.8,
    min_move_pct: 0.4,
    min_net_move_pct: 0.25,
    min_liquidity_percentile: 0.4,
    max_spread_bps: 8,
    min_risk_reward: 2,
    fee_bps_per_side: 5,
    slippage_bps_per_side: 3,
    require_oi: false,
    allow_missing_liquidity: false,
  },
};
export const PRIOR_THRESHOLDS: Record<string, number> = {
  '5m': 2.5,
  '15m': 2,
};
const fmt = (n: number | null | undefined, d = 2) =>
  n == null || !Number.isFinite(n)
    ? '—'
    : n.toLocaleString('zh-CN', { maximumFractionDigits: d });
const dt = (n: number | null | undefined) =>
  n
    ? new Date(n).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
      })
    : '尚无记录';
const direction = (d?: string) =>
  d === 'up' || d === 'long'
    ? '向上'
    : d === 'down' || d === 'short'
      ? '向下'
      : '未确认';
const structureEvent = (event?: string | null) =>
  event === 'BOS'
    ? '结构突破'
    : event === 'CHoCH'
      ? '结构转折'
      : event || '尚无有效突破';
const indicatorDisplayName = (label?: string) =>
  (label || '未知指标')
    .replaceAll('Supertrend', '超级趋势')
    .replaceAll('SMC', '智慧资金市场结构')
    .replaceAll('EMA', '指数移动平均双线');
export type IndicatorContext = {
  status?: string;
  supertrend?: {
    ready: boolean;
    direction: string;
    value: number;
    flipped: boolean;
    atr: number;
  };
  smc?: {
    ready: boolean;
    direction: string;
    event: string | null;
    event_direction?: string;
    support: number | null;
    resistance: number | null;
    zone?: string;
    events?: IndicatorChartEvent[];
    order_blocks?: IndicatorChartZone[];
    fvgs?: IndicatorChartZone[];
    equal_levels?: IndicatorChartLevel[];
    zone_bounds?: {
      low: number;
      high: number;
      equilibrium: number;
      confirmed_at: number;
    } | null;
  };
  ema?: { ready: boolean; direction: string; fast: number; slow: number };
  chart?: {
    supertrend: {
      time: number;
      value: number;
      direction: string;
      label?: string;
    }[];
    flips?: IndicatorChartEvent[];
    ema_fast?: { time: number; value: number }[];
    ema_slow?: { time: number; value: number }[];
    levels: IndicatorChartLevel[];
    events?: IndicatorChartEvent[];
    order_blocks?: IndicatorChartZone[];
    fvgs?: IndicatorChartZone[];
    equal_levels?: IndicatorChartLevel[];
    zone_bounds?: IndicatorChartZone[];
    strong_weak?: IndicatorChartEvent[];
  };
};
export type IndicatorChartEvent = {
  time: number;
  price: number;
  direction: string;
  source?: string;
  code?: string;
  label?: string;
  confirmed_at?: number;
  pivot_time?: number;
};
export type IndicatorChartLevel = {
  start_time?: number;
  end_time?: number;
  price: number;
  direction?: string;
  kind?: string;
  source?: string;
  code?: string;
  label: string;
  confirmed_at: number;
};
export type IndicatorChartZone = {
  start_time: number;
  end_time?: number;
  low: number;
  high: number;
  direction: string;
  source?: string;
  code?: string;
  label: string;
  confirmed_at: number;
  status?: string;
  extend_right?: boolean;
};
export type PositionAssessment = {
  code?: string;
  label?: string;
  advice?: string;
  trade_direction?: string;
  blocked?: boolean;
  status?: string;
  reference_type?: string | null;
  level?: number | null;
  zone_low?: number | null;
  zone_high?: number | null;
  distance_pct?: number | null;
  breakout_confirmed?: boolean;
  reason?: string;
  as_of?: number | null;
  uses_closed_bars_only?: boolean;
};
export type PositionView = {
  status: 'actionable' | 'blocked' | 'wait';
  title: string;
  context: string;
  reason: string;
  assessment: PositionAssessment | null;
};

/** Use only the persisted, direction-specific assessment; missing old data stays neutral. */
export const positionView = (signal: Pick<SignalQuality, 'position_assessment' | 'entry_advice'>): PositionView => {
  const assessment = signal.position_assessment;
  if (!assessment || typeof assessment !== 'object') {
    return {
      status: 'wait',
      title: '位置待确认',
      context: '旧记录缺少已保存的位置判断',
      reason: '系统不会根据现在看到的行情替旧信号补算压力、支撑或趋势位置。',
      assessment: null,
    };
  }
  const side = assessment.trade_direction;
  const context = assessment.label || '结构位置待确认';
  const reason = assessment.reason || '已保存的位置依据不足';
  const savedAdvice = signal.entry_advice || assessment.advice || '';
  const favorableButBlocked =
    assessment.blocked === true &&
    ((side === 'long' &&
      ['lower_support', 'breakout_up_held'].includes(assessment.code || '')) ||
      (side === 'short' &&
        ['upper_resistance', 'breakout_down_held'].includes(
          assessment.code || '',
        ))) &&
    !savedAdvice.includes('不宜');
  // The backend intentionally marks an unconfirmed position as blocked for
  // the execution gate.  Preserve its more specific `wait` status in the UI
  // instead of presenting a directional "do not chase" conclusion.
  if (assessment.status === 'wait' || favorableButBlocked) {
    return {
      status: 'wait',
      title: favorableButBlocked ? '等待结构确认' : '位置待确认',
      context,
      reason: savedAdvice || reason,
      assessment,
    };
  }
  if (assessment.status === 'blocked' || assessment.blocked === true) {
    const blockedTitle = savedAdvice.includes('不宜追空')
      ? '不宜追空'
      : savedAdvice.includes('不宜追多')
        ? '不宜追多'
        : side === 'short'
          ? '不宜追空'
          : side === 'long'
            ? '不宜追多'
            : '不宜追单';
    return {
      status: 'blocked',
      title: blockedTitle,
      context,
      reason,
      assessment,
    };
  }
  if (assessment.status !== 'actionable') {
    return { status: 'wait', title: '位置待确认', context, reason, assessment };
  }
  if (assessment.code === 'lower_support' && side === 'long') {
    return { status: 'actionable', title: '支撑附近可看多', context, reason, assessment };
  }
  if (assessment.code === 'upper_resistance' && side === 'short') {
    return { status: 'actionable', title: '压力附近可看空', context, reason, assessment };
  }
  if (assessment.breakout_confirmed && assessment.code === 'breakout_up_held' && side === 'long') {
    return { status: 'actionable', title: '突破站稳，顺势看多', context, reason, assessment };
  }
  if (assessment.breakout_confirmed && assessment.code === 'breakout_down_held' && side === 'short') {
    return { status: 'actionable', title: '突破站稳，顺势看空', context, reason, assessment };
  }
  // A middle-of-range/trend value can be useful context, but it is not a
  // location-specific entry. Keep the visible instruction conservative.
  return {
    status: 'wait',
    title: '位置待确认',
    context,
    reason: signal.entry_advice || reason,
    assessment,
  };
};
export type SignalQuality = {
  qualified?: boolean;
  quality_schema_version?: number;
  setup?: string;
  quality_reasons?: string[];
  indicators?: IndicatorContext;
  confirmations?: {
    id: string;
    name: string;
    passed: boolean;
    reason: string;
  }[];
  policy_snapshot?: AdvancedSettings;
  policy_hash?: string;
  threshold_used?: number;
  position_assessment?: PositionAssessment | null;
  entry_advice?: string;
  trade_plan?: {
    direction: string;
    reference_entry: number;
    stop_loss: number;
    target: number;
    risk_reward: number;
    stop_distance_pct: number;
    estimated_cost_bps: number;
    net_potential_pct: number;
    not_order: boolean;
  } | null;
};

function Numeric({
  label,
  value,
  onChange,
  min = 0,
  max = 100,
  step = 0.1,
  hint,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  hint?: string;
}) {
  return (
    <label className="field">
      <span>{label}</span>
      <Input
        type="number"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <p>{hint}</p>}
    </label>
  );
}

export function IndicatorSettingsPanel({
  value,
  onChange,
}: {
  value: AdvancedSettings;
  onChange: (s: AdvancedSettings) => void;
  api?: string | null;
  online?: boolean;
}) {
  const i = value.indicators,
    q = value.quality;
  const setI = (patch: Partial<typeof i>) =>
    onChange({ ...value, indicators: { ...i, ...patch } });
  const setQ = (patch: Partial<typeof q>) =>
    onChange({ ...value, quality: { ...q, ...patch } });
  const toggle = (id: string) => {
    const list = i.enabled_indicators.includes(id)
      ? i.enabled_indicators.filter((x) => x !== id)
      : [...i.enabled_indicators, id];
    setI({ enabled_indicators: list });
  };
  return (
    <>
      <WorkspaceTabs labels={['指标选择与参数','强信号门槛']}>
      <section className="panel advanced-panel">
        <div className="panel-heading">
          <h2>
            <Layers3 size={17} />
            指标确认
          </h2>
          <span className="chip">
            {i.enabled_indicators.length
              ? `已选 ${i.enabled_indicators.length} 个指标`
              : '空策略'}
          </span>
        </div>
        <div className="form-body">
          <p className="section-intro">
            指标策略与活跃度独立计算。可不选规则，或使用 WaveTrend 同根、同方向的超买超卖交叉确认。
          </p>
          <div className="indicator-picker">
            <button
              type="button"
              title="不要求技术指标确认，只保留两根 K 线、活跃度、流动性与支撑阻力等质量检查。"
              className={
                'indicator-option ' +
                (!i.enabled_indicators.length ? 'active' : '')
              }
              onClick={() => setI({ enabled_indicators: [] })}
              aria-pressed={!i.enabled_indicators.length}
            >
              <span className="indicator-check">
                {!i.enabled_indicators.length && <Check size={14} />}
              </span>
              <span>
                <b>空策略</b>
                <small>不要求指标共振 · 候选更多</small>
              </span>
            </button>
            {[['wt_extreme_cross','WaveTrend · 超买超卖交叉','WT1低于-53金叉／高于53死叉，收盘确认'],['nan_strategy1_oversold','楠策略1超买超卖','WT极值转折入场，相反转折平仓']].map(([id,name,desc])=><button type="button" key={id} className={'indicator-option '+(i.enabled_indicators.includes(id)?'active':'')} onClick={()=>toggle(id)} aria-pressed={i.enabled_indicators.includes(id)}><span><b>{name}</b><small>{desc}</small></span></button>)}
          </div><p className="field-help">指标策略统一在研究库中选择；每项活跃度与可做度在该项“设置”中配置。这里控制两K异常的确认条件。</p>
          <div className="settings-group">
            <h3>
              可做度基础计算 <small>支撑阻力与已确认结构</small>
            </h3>
            <div className="field-grid">
              <Numeric
                label="内部结构周期"
                hint="较短的局部高低点确认长度，用于捕捉更快的结构突破与结构转折。"
                value={i.smc_internal_length}
                min={2}
                max={30}
                step={1}
                onChange={(v) => setI({ smc_internal_length: v })}
              />
              <Numeric
                label="波段结构周期"
                hint="较大的摆动高低点确认长度，用于主要支撑阻力；越大越稳但确认更晚。"
                value={i.smc_swing_length}
                min={10}
                max={200}
                step={1}
                onChange={(v) => setI({ smc_swing_length: v })}
              />
              <Numeric
                label="反转确认有效根数"
                hint="结构转折或指标翻转发生后，最多多少根 K 线仍可用于确认当前反转形态。"
                value={i.structure_max_age}
                min={1}
                max={20}
                step={1}
                onChange={(v) => setI({ structure_max_age: v })}
              />
            </div>
            <p className="field-help">
              包含结构突破、结构转折、确认的支撑阻力、订单块与同周期公允价值缺口
              辅助信息；未复制原脚本的高周期前视。并非 TradingView
              全部绘图功能的逐像素复刻。
            </p>
          </div>
        </div>
      </section>
      <section className="panel advanced-panel">
        <div className="panel-heading">
          <h2>
            <ShieldCheck size={17} />
            强信号门槛
          </h2>
          <span className="chip">过滤微小波动</span>
        </div>
        <div className="form-body">
          <div className="field-grid">
            <Numeric
              label="最低活跃度 / 100"
              value={q.min_activity_score}
              step={1}
              onChange={(v) => setQ({ min_activity_score: v })}
            />
            <Numeric
              label="最低相对成交额 ×"
              value={q.min_relative_volume}
              min={1}
              max={20}
              onChange={(v) => setQ({ min_relative_volume: v })}
            />
            <Numeric
              label="最低有效实体变化 %"
              value={q.min_move_pct}
              min={0.05}
              max={20}
              step={0.05}
              onChange={(v) => setQ({ min_move_pct: v })}
            />
            <Numeric
              label="扣成本后最低空间 %"
              value={q.min_net_move_pct}
              max={20}
              step={0.05}
              onChange={(v) => setQ({ min_net_move_pct: v })}
            />
            <Numeric
              label="最低净盈亏比"
              value={q.min_risk_reward}
              min={1}
              max={10}
              onChange={(v) => setQ({ min_risk_reward: v })}
            />
            <Numeric
              label="最大买卖价差 bps"
              value={q.max_spread_bps}
              min={0.1}
              max={100}
              onChange={(v) => setQ({ max_spread_bps: v })}
            />
            <Numeric
              label="最低同报价流动性分位 0–1"
              value={q.min_liquidity_percentile}
              max={1}
              step={0.05}
              onChange={(v) => setQ({ min_liquidity_percentile: v })}
            />
            <Numeric
              label="单边手续费 bps"
              value={q.fee_bps_per_side}
              step={1}
              onChange={(v) => setQ({ fee_bps_per_side: v })}
            />
            <Numeric
              label="单边滑点 bps"
              value={q.slippage_bps_per_side}
              step={1}
              onChange={(v) => setQ({ slippage_bps_per_side: v })}
            />
          </div>
          <div className="switch-field">
            <div>
              <b>必须有历史 OI 确认</b>
              <p>打开后，持仓量样本不足也不会进入强信号。</p>
            </div>
            <Switch
              checked={q.require_oi}
              onCheckedChange={(v) => setQ({ require_oi: v })}
              aria-label="要求 OI 数据"
            />
          </div>
          <div className="switch-field">
            <div>
              <b>允许缺失流动性数据</b>
              <p>默认关闭。打开会降低数据完整性要求，请用回测单独评估。</p>
            </div>
            <Switch
              checked={q.allow_missing_liquidity}
              onCheckedChange={(v) => setQ({ allow_missing_liquidity: v })}
              aria-label="允许缺失流动性"
            />
          </div>
          <div className="rule-fixed">
            <ShieldCheck size={17} />
            <p>
              达到门槛只代表符合你的观察规则。参考价、失效位和目标来自信号收盘时的已知结构，不是订单，也不保证成交或收益。
            </p>
          </div>
        </div>
      </section>
      </WorkspaceTabs>

    </>
  );
}

export function ThresholdPanel({
  value,
  onChange,
}: {
  value: AdvancedSettings;
  onChange: (v: AdvancedSettings) => void;
}) {
  return (
    <div className="threshold-profile">
      <label className="field">
        <span>波动门槛方案</span>
        <select
          className="native-select"
          value={value.threshold_profile}
          onChange={(e) =>
            onChange({
              ...value,
              threshold_profile: e.target.value as 'timeframe' | 'manual',
            })
          }
        >
          <option value="timeframe">按周期过滤 · 待验证起点</option>
          <option value="manual">5m / 15m 使用下方统一倍数</option>
        </select>
      </label>
      <div className="threshold-pills">
        {Object.entries(PRIOR_THRESHOLDS).map(([t, n]) => (
          <span key={t}>
            <small>{t}</small>
            <b>{n}×</b>
          </span>
        ))}
      </div>
      <p className="field-help">
        短周期先用更高门槛减少噪声。这组值尚非回测最优值；实际门槛取周期值与下方统一下限中较高者。回测页比较
        1.5 / 2 / 2.5 / 3 / 4 倍。
      </p>
    </div>
  );
}

export function ConfirmationPanel({
  signal,
  synthetic = false,
}: {
  signal: SignalQuality;
  synthetic?: boolean;
}) {
  const ctx = signal.indicators,
    plan = signal.trade_plan,
    position = positionView(signal),
    assessment = position.assessment;
  return (
    <section className="panel confirmation-panel">
      <div className="panel-heading">
        <h2>
          <Layers3 size={17} />
          信号确认与交易空间
        </h2>
        <span
          className={'qualification ' + (signal.qualified ? 'pass' : 'watch')}
        >
          {synthetic
            ? '合成演示'
            : signal.qualified
              ? '通过筛选 · 待人工复核'
              : '观察 / 已过滤'}
        </span>
      </div>
      <div className="confirmation-body">
        <div className="context-grid">
          <div>
            <small>形态逻辑</small>
            <b>
              {signal.setup === 'reversal'
                ? '反转确认'
                : signal.setup === 'trend'
                  ? '趋势延续'
                  : '待新规则计算'}
            </b>
          </div>
          <div>
            <small>超级趋势</small>
            <b>
              {ctx?.supertrend?.ready
                ? direction(ctx.supertrend.direction)
                : '历史不足'}
            </b>
            <span>
              {ctx?.supertrend?.ready ? fmt(ctx.supertrend.value, 8) : '—'}
            </span>
          </div>
          <div>
            <small>智慧资金市场结构</small>
            <b>{ctx?.smc?.ready ? direction(ctx.smc.direction) : '历史不足'}</b>
            <span>{structureEvent(ctx?.smc?.event)}</span>
          </div>
          <div>
            <small>支撑 / 阻力</small>
            <b>{fmt(ctx?.smc?.support, 8)}</b>
            <span>{fmt(ctx?.smc?.resistance, 8)}</span>
          </div>
        </div>
        <div className={`position-card ${position.status}`}>
          <div className="position-card-heading">
            <span>
              {position.status === 'actionable' ? (
                <Check size={16} />
              ) : (
                <AlertTriangle size={16} />
              )}
              <small>指标2 · 入场位置</small>
            </span>
            <b>{position.title}</b>
          </div>
          <div className="position-card-copy">
            <strong>{position.context}</strong>
            <p>{position.reason}</p>
          </div>
          <div className="position-levels">
            <div>
              <small>区域下界</small>
              <b>{fmt(assessment?.zone_low, 8)}</b>
            </div>
            <div>
              <small>区域上界</small>
              <b>{fmt(assessment?.zone_high, 8)}</b>
            </div>
            <div>
              <small>参考位置</small>
              <b>{fmt(assessment?.level, 8)}</b>
            </div>
            <div>
              <small>距离区域</small>
              <b>
                {assessment?.distance_pct == null
                  ? '—'
                  : `${fmt(assessment.distance_pct)}%`}
              </b>
            </div>
          </div>
          <p className="position-causality">
            {assessment?.uses_closed_bars_only === true ? (
              <>
                <ShieldCheck size={13} />
                仅使用截至 {dt(assessment.as_of)} 已收盘的 K 线，不读取未来数据。
              </>
            ) : (
              <>
                <AlertTriangle size={13} />
                旧记录缺少可核验的位置快照，因此只显示“位置待确认”。
              </>
            )}
          </p>
        </div>
        <div className="confirmation-list">
          {signal.confirmations?.map((c) => (
            <div key={c.id} className={c.passed ? 'pass' : 'watch'}>
              {c.passed ? <Check size={15} /> : <AlertTriangle size={15} />}
              <b>{c.name}</b>
              <span>{c.reason}</span>
            </div>
          ))}
        </div>
        {plan && (
          <div className="trade-plan">
            <div>
              <small>参考方向 / 收盘价</small>
              <b>
                {direction(plan.direction)} · {fmt(plan.reference_entry, 8)}
              </b>
            </div>
            <div>
              <small>结构失效参考</small>
              <b className="down">{fmt(plan.stop_loss, 8)}</b>
            </div>
            <div>
              <small>结构目标参考</small>
              <b className="up">{fmt(plan.target, 8)}</b>
            </div>
            <div>
              <small>净盈亏比 / 净空间</small>
              <b>
                {fmt(plan.risk_reward)} / {fmt(plan.net_potential_pct)}%
              </b>
            </div>
          </div>
        )}
        {!!signal.quality_reasons?.length && (
          <details className="quality-details" open={!signal.qualified}>
            <summary>
              筛选依据与未通过原因（{signal.quality_reasons.length}）
            </summary>
            <ul>
              {signal.quality_reasons.map((r, n) => (
                <li key={n}>{r}</li>
              ))}
            </ul>
          </details>
        )}
        {!ctx && (
          <p className="field-help">
            此旧记录未包含新指标的完整历史，不补造确认结果。后续真实信号会保存指标快照、失效位、目标位及未通过原因。
          </p>
        )}
        <p className="field-help">
          信号快照，不是当前报价。
          {plan ? `估计往返成本 ${fmt(plan.estimated_cost_bps)} bps；` : ''}
          下单前仍需检查最新价格、盘口和风险。规则版本{' '}
          {signal.policy_hash || '旧版'}。
        </p>
      </div>
    </section>
  );
}

type Listing = ContractMeta & {
  symbol: string;
  event_type?: string;
  type?: string;
  observed_at?: number;
  onboard_date?: number;
  onboardDate?: number;
  message?: string;
};
type UniverseRow = ContractMeta & {
  symbol: string;
  status: string;
  subscribed: boolean;
  live: boolean;
  ready_timeframes?: number | string[];
  timeframes?: Record<
    string,
    { state: string; reason?: string; bars?: number; minimum?: number }
  >;
};
export function UniversePanel({
  api,
  online,
}: {
  api: string | null;
  online: boolean;
}) {
  const [universe, setUniverse] = useState<UniverseRow[]>([]),
    [listings, setListings] = useState<Listing[]>([]),
    [query, setQuery] = useState(''),
    [category, setCategory] = useState('all'),
    [error, setError] = useState('');
  useEffect(() => {
    if (!api || !online) return;
    let ended = false;
    const pull = async () => {
      try {
        const [a, b] = await Promise.all([
          fetch(api + '/api/universe'),
          fetch(api + '/api/listings'),
        ]);
        if (!a.ok || !b.ok) throw Error();
        const [u, l] = await Promise.all([
          a.json() as Promise<{ items: UniverseRow[] }>,
          b.json() as Promise<{ items: Listing[] }>,
        ]);
        if (!ended) {
          setUniverse(u.items || []);
          setListings(l.items || []);
          setError('');
        }
      } catch {
        if (!ended) setError('范围或上市事件暂时无法读取');
      }
    };
    pull();
    const id = setInterval(pull, 15000);
    return () => {
      ended = true;
      clearInterval(id);
    };
  }, [api, online]);
  const rows = universe.filter(
    (u) =>
      u.symbol.toLowerCase().includes(query.toLowerCase()) &&
      matchesCategory(u, category) &&
      quoteOf(u) === 'USDT',
  );
  return (
    <WorkspaceTabs labels={['上线提醒','合约覆盖']}>
      <section className="panel">
        <div className="panel-heading">
          <h2>
            <Bell size={17} />
            新币 / 新合约上架提醒
          </h2>
          <span className="chip">仅看板 · TG 未接入</span>
        </div>
        <div className="form-body">
          <p className="section-intro">
            每分钟核对全部 USDT 计价的 U
            本位官方合约，包括传统金融与交割。扩大范围时，已有合约作为基线纳入，不误报成新上市；待上架与开始交易分别记录、持久去重。
          </p>
          {listings.some((x) => quoteOf(x) === 'USDT') ? (
            listings
              .filter((x) => quoteOf(x) === 'USDT')
              .slice(0, 20)
              .map((x, n) => (
                <div
                  className="listing-event"
                  key={x.symbol + String(x.observed_at) + n}
                >
                  <div className="listing-icon">
                    <Bell size={17} />
                  </div>
                  <div>
                    <b>{x.symbol}</b>
                    <small>
                      {categoryLabel(x)} · {contractLabel(x)} · {quoteOf(x)}
                    </small>
                    <p>
                      {x.message ||
                        (x.event_type === 'upcoming'
                          ? '发现待上架合约'
                          : x.event_type === 'listed'
                            ? '新合约已开放交易'
                            : '新合约状态变化')}
                    </p>
                    <small>{dt(x.observed_at)} UTC+8</small>
                  </div>
                </div>
              ))
          ) : (
            <div className="research-empty">
              <Bell size={30} />
              <h3>暂无已确认的新上架事件</h3>
              <p>
                {universe.length
                  ? '正在等待基线之后的新合约变化。'
                  : '官方合约列表尚未成功载入；暂不能判断是否有新币上架。'}
              </p>
            </div>
          )}
        </div>
      </section>
      <section className="panel">
        <div className="panel-heading">
          <h2>
            <Activity size={17} />
            全量监测覆盖
          </h2>
          <span className="count-pill">{universe.length}</span>
        </div>
        <div className="form-body">
          <p className="section-intro">
            按币安 U_MARGINED 官方市场归属和 quoteAsset=USDT
            筛选，包含全部资产类别及合约类型，不排除股票、商品或交割。USDC、USD1、U、BTC
            等其他报价不监测，旧数据保留。类别未知仍标注；历史不足、缺口与未收盘不生成正式信号。
          </p>
          <Input
            placeholder="搜索合约，例如 TSLAUSDT / XAUUSDT / BTCUSDT_260925"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="universe-classification">
            <select
              className="native-select"
              aria-label="监测范围资产类别"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              {CATEGORIES.map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </select>
            <span className="chip">仅 USDT 计价</span>
            <span className="muted">
              筛选 {rows.length} / {universe.length} 个合约
            </span>
          </div>
          {error && <p className="form-notice">{error}</p>}
          <div className="universe-table">
            <table>
              <thead>
                <tr>
                  <th>合约</th>
                  <th>行情</th>
                  <th>5m / 15m 历史</th>
                </tr>
              </thead>
              <tbody>
                {rows.slice(0, 100).map((u) => (
                  <tr key={u.symbol}>
                    <td>
                      <b>{u.symbol}</b>
                      <small>
                        {categoryLabel(u)} · {contractLabel(u)}
                      </small>
                      <small>
                        报价 {quoteOf(u)} · 保证金 {u.margin_asset || '待核验'}
                      </small>
                      <small>{u.status}</small>
                    </td>
                    <td>
                      <span className={u.live ? 'up' : 'muted'}>
                        {u.live
                          ? '有实时心跳'
                          : u.subscribed
                            ? '等待心跳'
                            : '未订阅'}
                      </span>
                    </td>
                    <td>
                      <div className="coverage-pills">
                        {Object.entries(u.timeframes || {})
                          .filter(([tf]) => Object.hasOwn(PRIOR_THRESHOLDS, tf))
                          .map(([tf, v]) => (
                          <span
                            key={tf}
                            title={`${v.reason || v.state} · ${v.bars || 0}/${v.minimum || 0} 根`}
                            className={v.state === 'ready' ? 'ready' : ''}
                          >
                            {tf}
                          </span>
                          ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {!rows.length && (
            <p className="field-help">
              {!api
                ? '请打开本机看板查看真实覆盖。'
                : '暂无范围记录，连接失败时不会显示虚构的覆盖数量。'}
            </p>
          )}
          {rows.length > 100 && (
            <p className="field-help">
              显示前 100 项，共 {rows.length} 项；使用搜索查找具体币种。
            </p>
          )}
        </div>
      </section>
    </WorkspaceTabs>
  );
}

type Dataset = {
  id: string;
  label: string;
  symbol: string;
  timeframe: string;
  bars: number;
  first_close_time: number;
  last_close_time: number;
  gaps: number;
  provenance?: { type: string; verified_checksum?: boolean };
};
type Metrics = {
  trades?: number;
  win_rate?: number;
  win_rate_pct?: number;
  expectancy_net_pct?: number;
  profit_factor?: number | null;
  max_drawdown_pct?: number;
  [k: string]: unknown;
};
type BacktestReport = {
  status: string;
  dataset?: Record<string, unknown>;
  strategy?: Record<
    string,
    { metrics: Metrics; comparisons?: unknown[]; [k: string]: unknown }
  >;
  recommendation?: unknown;
  limitations?: string[];
  indicator_study?: {
    status?: string;
    threshold?: number;
    trial_count?: number;
    predeclared_combinations?: boolean;
    validation_status?: string;
    minimum_closed_trades_per_segment?: number;
    primary_policy?: string;
    selection_performed?: boolean;
    rows?: {
      id: string;
      label: string;
      enabled_indicators: string[];
      combination_size: number;
      train?: { metrics?: Metrics; sample_sufficient?: boolean };
      holdout?: { metrics?: Metrics; sample_sufficient?: boolean };
      walk_forward?: {
        metrics?: Metrics;
        sample_sufficient?: boolean;
        sample_sufficient_all_folds?: boolean;
        fold_count?: number;
      };
      sample_sufficient_both?: boolean;
      policies?: Record<
        string,
        {
          train?: { metrics?: Metrics; sample_sufficient?: boolean };
          holdout?: { metrics?: Metrics; sample_sufficient?: boolean };
          walk_forward?: {
            metrics?: Metrics;
            sample_sufficient?: boolean;
            sample_sufficient_all_folds?: boolean;
            fold_count?: number;
          };
          sample_sufficient_both?: boolean;
        }
      >;
    }[];
  };
  policy_hash?: string;
  [k: string]: unknown;
};
type Job = {
  id: string;
  kind: string;
  state: string;
  message: string;
  result?: BacktestReport | Dataset;
};
function yearDefaults() {
  const end = new Date();
  end.setUTCDate(1);
  // The newest monthly archive can appear after the UTC month changes. Use
  // the latest month that is already verifiable instead of promising a 404.
  end.setUTCMonth(end.getUTCMonth() - 2);
  const start = new Date(end);
  start.setUTCMonth(start.getUTCMonth() - 11);
  const key = (value: Date) =>
    `${value.getUTCFullYear()}-${String(value.getUTCMonth() + 1).padStart(2, '0')}`;
  return { start: key(start), end: key(end) };
}
const DEFAULT_YEAR = yearDefaults();
function monthsBetween(first: string, last: string) {
  if (!/^20\d{2}-(0[1-9]|1[0-2])$/.test(first) ||
      !/^20\d{2}-(0[1-9]|1[0-2])$/.test(last)) return [];
  const cursor = new Date(`${first}-01T00:00:00Z`);
  const end = new Date(`${last}-01T00:00:00Z`);
  const values: string[] = [];
  while (cursor <= end && values.length <= 12) {
    values.push(
      `${cursor.getUTCFullYear()}-${String(cursor.getUTCMonth() + 1).padStart(2, '0')}`,
    );
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  }
  return values.length <= 12 ? values : [];
}
export function BacktestPanel({
  api,
  online,
}: {
  api: string | null;
  online: boolean;
}) {
  const [datasets, setDatasets] = useState<Dataset[]>([]),
    [datasetId, setDatasetId] = useState(''),
    [symbol, setSymbol] = useState('BTCUSDT'),
    [tf, setTf] = useState('5m'),
    [firstMonth, setFirstMonth] = useState(DEFAULT_YEAR.start),
    [lastMonth, setLastMonth] = useState(DEFAULT_YEAR.end),
    [hold, setHold] = useState(5),
    [job, setJob] = useState<Job | null>(null),
    [error, setError] = useState(''),
    [working, setWorking] = useState(false),
    [report, setReport] = useState<BacktestReport | null>(null),
    [policy, setPolicy] = useState('strict_full_policy'),
    [studyPolicy, setStudyPolicy] = useState<
      'price_only_ablation' | 'strict_full_policy'
    >('price_only_ablation');
  const reload = async () => {
    if (!api) return;
    const r = await fetch(api + '/api/history');
    if (!r.ok) throw Error('无法读取历史数据');
    const data = (await r.json()) as { items: Dataset[] };
    setDatasets(data.items || []);
    setDatasetId((v) => v || data.items?.[0]?.id || '');
  };
  const loadRecent = async () => {
    if (!api) return;
    const response = await fetch(api + '/api/backtests/reports');
    if (!response.ok) return;
    const data = (await response.json()) as { items: Job[] };
    if (!data.items[0]) return;
    const detail = await fetch(api + '/api/jobs/' + data.items[0].id);
    if (!detail.ok) return;
    const latest = (await detail.json()) as Job;
    setJob(latest);
    setReport(latest.result as BacktestReport);
  };
  useEffect(() => {
    if (api && online) {
      reload().catch((e) => setError(e.message));
      try {
        const id = localStorage.getItem('pulse-research-job');
        if (id)
          fetch(api + '/api/jobs/' + id)
            .then((r) => (r.ok ? (r.json() as Promise<Job>) : null))
            .then((j) => {
              if (j) {
                setJob(j);
                if (j.kind === 'backtest' && j.state === 'completed')
                  setReport(j.result as BacktestReport);
              }
            })
            .catch(() => {});
        else loadRecent().catch(() => {});
      } catch {}
    }
  }, [api, online]);
  useEffect(() => {
    if (!api || !job || job.state !== 'running') return;
    let ended = false;
    const id = setInterval(async () => {
      try {
        const r = await fetch(api + '/api/jobs/' + job.id, {
          signal: AbortSignal.timeout(5000),
        });
        if (!r.ok) throw Error();
        const j = (await r.json()) as Job & Dataset & { detail?: unknown };
        if (ended) return;
        setJob(j);
        if (j.state === 'completed') {
          if (j.kind === 'backtest') setReport(j.result as BacktestReport);
          else {
            await reload();
            setDatasetId((j.result as Dataset).id);
          }
        }
      } catch {
        if (!ended) setError('任务状态暂时无法读取，后台仍可能在运行。');
      }
    }, 2000);
    return () => {
      ended = true;
      clearInterval(id);
    };
  }, [api, job?.id, job?.state]);
  const submit = async (path: string, body: unknown) => {
    if (!api) return;
    setWorking(true);
    setError('');
    try {
      const r = await fetch(api + path, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Pulse-Client': 'dashboard',
        },
        body: JSON.stringify(body),
      });
      const j = (await r.json()) as Job & Dataset & { detail?: unknown };
      if (!r.ok)
        throw Error(
          typeof j.detail === 'string' ? j.detail : '参数无效或已有任务在运行',
        );
      setJob(j);
      localStorage.setItem('pulse-research-job', j.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorking(false);
    }
  };
  const upload = async (file: File) => {
    if (!api) return;
    if (file.size > 20000000) {
      setError('文件不能超过 20 MB');
      return;
    }
    setWorking(true);
    setError('');
    try {
      const r = await fetch(
        `${api}/api/history/import?symbol=${encodeURIComponent(symbol)}&timeframe=${tf}&format=${file.name.toLowerCase().endsWith('.json') ? 'json' : 'csv'}`,
        {
          method: 'POST',
          headers: {
            'Content-Type': 'text/plain',
            'X-Pulse-Client': 'dashboard',
          },
          body: await file.text(),
        },
      );
      const j = (await r.json()) as Job & Dataset & { detail?: unknown };
      if (!r.ok)
        throw Error(typeof j.detail === 'string' ? j.detail : '导入失败');
      await reload();
      setDatasetId(j.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setWorking(false);
    }
  };
  const chosen = datasets.find((d) => d.id === datasetId),
    busy = working || job?.state === 'running';
  const selectedMonths = monthsBetween(firstMonth, lastMonth);
  const strategies = report?.strategy || {};
  const current = strategies[policy],
    metrics = current?.metrics || {};
  const comparisons = (current?.comparisons || []) as Record<string, unknown>[];
  const indicatorRows = report?.indicator_study?.rows || [];
  function exportReport() {
    if (!report) return;
    const url = URL.createObjectURL(
      new Blob([JSON.stringify(report, null, 2)], { type: 'application/json' }),
    );
    const a = document.createElement('a');
    a.href = url;
    a.download = '合约推送-历史回测-' + (report.policy_hash || '报告') + '.json';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  function exportCsv(
    kind: 'summary' | 'trades' | 'entry-events' | 'entry-summary',
  ) {
    if (!api || !job || job.kind !== 'backtest' || job.state !== 'completed')
      return;
    const a = document.createElement('a');
    a.href = `${api}/api/backtests/${job.id}/${kind}.csv`;
    const labels = {
      summary: '完整汇总',
      trades: '完整逐笔交易',
      'entry-events': '双向入场点',
      'entry-summary': '双向胜率汇总',
    } as const;
    a.download = `合约推送-历史回测-${labels[kind]}.csv`;
    a.click();
  }
  return (
    <div className="backtest-page">
      <section className="panel research-intro">
        <div>
          <span className="eyebrow">RESEARCH / VALIDATE BEFORE TRUST</span>
          <h2>让历史检验你的筛选规则</h2>
          <p>
            70% 时间段用于比较门槛，后 30%
            留作验证，并增加三折滚动时间外验证。信号收盘后下一根开盘模拟入场，包含双边手续费、滑点和结构止损；同根同时触及止盈止损，按止损处理。
          </p>
        </div>
        <FlaskConical size={46} />
      </section>
      <div className="research-layout">
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <Download size={17} />
              历史数据
            </h2>
            <span className="chip">与实时数据隔离</span>
          </div>
          <div className="form-body">
            <div className="field-grid">
              <label className="field">
                <span>合约</span>
                <Input
                  value={symbol}
                  onChange={(e) => setSymbol(e.target.value.toUpperCase())}
                  placeholder="BTCUSDT"
                />
              </label>
              <label className="field">
                <span>周期</span>
                <select
                  className="native-select"
                  value={tf}
                  onChange={(e) => setTf(e.target.value)}
                >
                  {Object.keys(PRIOR_THRESHOLDS).map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </label>
            </div>
            <div className="field-grid">
              <label className="field">
                <span>一年数据开始月份</span>
                <Input
                  type="month"
                  value={firstMonth}
                  onChange={(e) => setFirstMonth(e.target.value)}
                />
              </label>
              <label className="field">
                <span>一年数据结束月份</span>
                <Input
                  type="month"
                  value={lastMonth}
                  onChange={(e) => setLastMonth(e.target.value)}
                />
              </label>
            </div>
            <Button
              disabled={!online || busy || !selectedMonths.length}
              onClick={() =>
                submit('/api/history/download', {
                  symbol,
                  timeframe: tf,
                  months: selectedMonths,
                })
              }
            >
              <Download size={15} />
              下载这一年并逐月校验
            </Button>
            <label
              className={'upload-button ' + (!online || busy ? 'disabled' : '')}
            >
              <Upload size={15} />
              导入 CSV / JSON
              <input
                type="file"
                accept=".csv,.json"
                disabled={!online || busy}
                onChange={(e) => {
                  if (e.target.files?.[0]) upload(e.target.files[0]);
                  e.target.value = '';
                }}
              />
            </label>
            <p className="field-help">
              当前选择 {selectedMonths.length || '无效'} 个月。每个官方月包都校验 SHA-256；
              5 分钟一年约 105,120 根，15 分钟一年约 35,040 根，单数据集上限
              120,000 根。官方最新月份尚未发布时，默认退到最近可验证的十二个完整月。
            </p>
          </div>
        </section>
        <section className="panel">
          <div className="panel-heading">
            <h2>
              <FlaskConical size={17} />
              回测设置
            </h2>
            <Button
              variant="ghost"
              size="sm"
              disabled={!online || busy}
              onClick={() =>
                loadRecent().catch(() => setError('暂时无法读取已完成报告'))
              }
            >
              读取最近报告
            </Button>
          </div>
          <div className="form-body">
            <label className="field">
              <span>选择历史数据集</span>
              <select
                className="native-select"
                value={datasetId}
                onChange={(e) => setDatasetId(e.target.value)}
              >
                <option value="">请先下载或导入</option>
                {datasets.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.label}
                  </option>
                ))}
              </select>
            </label>
            {chosen && (
              <div className="dataset-info">
                <b>
                  {chosen.provenance?.verified_checksum
                    ? '官方归档 · 校验通过'
                    : '本地导入 · 未核验官方来源'}
                </b>
                <p>
                  {dt(chosen.first_close_time)} — {dt(chosen.last_close_time)}
                </p>
                <small>
                  {fmt(chosen.bars, 0)} 根 · {chosen.gaps} 处缺口 · 不含历史盘口
                  / OI 快照
                </small>
              </div>
            )}
            <Numeric
              label="最长持有根数"
              value={hold}
              min={1}
              max={100}
              step={1}
              onChange={setHold}
            />
            <p className="field-help">
              会同时比较 1.5 / 2 / 2.5 / 3 / 4
              倍。指标、形态、裁剪算法和质量门槛取自「监测规则」最近一次保存；运行后快照固定。
            </p>
            <Button
              disabled={!online || busy || !datasetId}
              onClick={() =>
                submit('/api/backtests', {
                  dataset_id: datasetId,
                  max_holding_bars: hold,
                })
              }
            >
              <FlaskConical size={16} />
              开始真实历史回测
            </Button>
          </div>
        </section>
      </div>
      {!api && (
        <div className="form-notice">
          在线地址仅演示看板。真实历史下载与回测请打开本机看板。
        </div>
      )}
      {error && (
        <div role="alert" className="form-notice">
          {error}
        </div>
      )}
      {job && (
        <div
          className={'job-status ' + (job.state === 'failed' ? 'failed' : '')}
        >
          {job.state === 'running' ? (
            <RefreshCw className="spin" size={17} />
          ) : job.state === 'failed' ? (
            <AlertTriangle size={17} />
          ) : (
            <Check size={17} />
          )}
          <b>{job.kind === 'download' ? '历史下载' : '历史回测'}</b>
          <span>{job.message}</span>
        </div>
      )}
      {report && (
        <section className="panel backtest-results">
          <div className="panel-heading">
            <h2>回测结果</h2>
            <div className="backtest-export-actions">
              {!!(report.exports as Record<string, unknown> | undefined)
                ?.entry_events_csv && (
                <Button
                  variant="outline"
                  disabled={!job || job.kind !== 'backtest' || job.state !== 'completed'}
                  onClick={() => exportCsv('entry-events')}
                  title="超级趋势与智慧资金市场结构同向确认后的买入做多、卖出做空入场点，以及后1/2/3/5根K线的成本后结果。"
                >
                  <Download size={15} />
                  双向入场点 CSV
                </Button>
              )}
              {!!(report.exports as Record<string, unknown> | undefined)
                ?.entry_summary_csv && (
                <Button
                  variant="outline"
                  disabled={!job || job.kind !== 'backtest' || job.state !== 'completed'}
                  onClick={() => exportCsv('entry-summary')}
                  title="按5分钟/15分钟、做多/做空和后1/2/3/5根K线分别统计的胜率与平均成本后收益。"
                >
                  <Download size={15} />
                  双向胜率 CSV
                </Button>
              )}
              <Button
                variant="outline"
                disabled={!job || job.kind !== 'backtest' || job.state !== 'completed'}
                onClick={() => exportCsv('summary')}
                title="适合先看：7 个指标组合在训练、时间外和滚动验证中的汇总指标。"
              >
                <Download size={15} />
                汇总 CSV
              </Button>
              <Button
                variant="outline"
                disabled={!job || job.kind !== 'backtest' || job.state !== 'completed'}
                onClick={() => exportCsv('trades')}
                title="每一笔模拟交易的信号、入场、出场、成本、净收益和最大有利/不利波动。"
              >
                <Download size={15} />
                逐笔交易 CSV
              </Button>
              <Button variant="ghost" onClick={exportReport}>
                完整 JSON
              </Button>
            </div>
          </div>
          <div className="form-body">
            <div className="result-tabs">
              {Object.keys(strategies).map((k) => (
                <button
                  key={k}
                  className={policy === k ? 'active' : ''}
                  onClick={() => setPolicy(k)}
                >
                  {k === 'strict_full_policy'
                    ? '完整门槛（严格）'
                    : '价格 / 成交量消融实验'}
                </button>
              ))}
            </div>
            <p className="form-notice">
              历史 K 线没有同期 OI
              和盘口数据。严格模式不会伪造这些信息；消融实验去掉相关缺失门槛及综合分数门槛，不能代表完整实盘策略。
            </p>
            {!!indicatorRows.length && (
              <section className="indicator-study">
                <div className="study-heading">
                  <div>
                    <h3>指标逐项与组合验证</h3>
                    <p>
                      固定同一异常倍数、支撑阻力、成本和风险收益规则，只改变指标确认组合。胜率必须与净期望、盈利因子和样本数一起看。
                    </p>
                  </div>
                  <div className="study-policy-switch" aria-label="指标回测数据口径">
                    <button
                      className={studyPolicy === 'price_only_ablation' ? 'active' : ''}
                      onClick={() => setStudyPolicy('price_only_ablation')}
                      title="历史 K 线可验证的指标、成交额、价格空间与风险收益；不伪造同期盘口和持仓量。"
                    >
                      K线可验证口径
                    </button>
                    <button
                      className={studyPolicy === 'strict_full_policy' ? 'active' : ''}
                      onClick={() => setStudyPolicy('strict_full_policy')}
                      title="完整实时门槛；历史数据缺少同期盘口/OI时会严格拒绝，因此可能没有交易。"
                    >
                      完整实时门槛
                    </button>
                  </div>
                </div>
                <div className="comparison-table indicator-study-table">
                  <table>
                    <thead>
                      <tr>
                        <th title="单项先独立测试；组合统一要求全部所选指标通过。">指标 / 组合</th>
                        <th>训练笔数</th>
                        <th title="胜率不等于盈利；还要扣除成本并检查盈亏比。">训练胜率</th>
                        <th>验证笔数</th>
                        <th title="后30%时间段，不参与选择参数。">验证胜率</th>
                        <th title="每笔收益扣除双边手续费与滑点后的平均值。">验证净期望</th>
                        <th title="三个连续的未见时间窗口合并成交笔数；每折仍单独检查至少30笔。">滚动验证笔数</th>
                        <th title="三个滚动时间外窗口合并后的扣成本单笔净期望。">滚动净期望</th>
                        <th title="总盈利 ÷ 总亏损；大于1仅表示本次样本为正。">盈利因子</th>
                        <th>最大回撤</th>
                        <th>结论</th>
                      </tr>
                    </thead>
                    <tbody>
                      {indicatorRows.map((row) => {
                        const chosenPolicy = row.policies?.[studyPolicy];
                        const train = chosenPolicy?.train || row.train || {};
                        const holdout = chosenPolicy?.holdout || row.holdout || {};
                        const trainMetrics = train.metrics || {};
                        const holdoutMetrics = holdout.metrics || {};
                        const walk =
                          chosenPolicy?.walk_forward || row.walk_forward || {};
                        const walkMetrics = walk.metrics || {};
                        const enough =
                          chosenPolicy?.sample_sufficient_both ??
                          row.sample_sufficient_both ??
                          false;
                        const positive =
                          enough &&
                          Number(holdoutMetrics.expectancy_net_pct) > 0 &&
                          Number(holdoutMetrics.profit_factor) > 1;
                        return (
                          <tr key={row.id}>
                            <td>
                              <b>{indicatorDisplayName(row.label)}</b>
                              <small>
                                {row.combination_size === 1 ? '单指标' : `${row.combination_size} 项全部共振`}
                              </small>
                            </td>
                            <td>{fmt(trainMetrics.trades as number, 0)}</td>
                            <td>
                              {fmt(
                                trainMetrics.win_rate == null
                                  ? null
                                  : Number(trainMetrics.win_rate) * 100,
                              )}
                              %
                            </td>
                            <td>{fmt(holdoutMetrics.trades as number, 0)}</td>
                            <td>
                              {fmt(
                                holdoutMetrics.win_rate == null
                                  ? null
                                  : Number(holdoutMetrics.win_rate) * 100,
                              )}
                              %
                            </td>
                            <td>{fmt(holdoutMetrics.expectancy_net_pct as number)}%</td>
                            <td>{fmt(walkMetrics.trades as number, 0)}</td>
                            <td>{fmt(walkMetrics.expectancy_net_pct as number)}%</td>
                            <td>{fmt(holdoutMetrics.profit_factor as number)}</td>
                            <td>{fmt(holdoutMetrics.max_drawdown_pct as number)}%</td>
                            <td>
                              <span
                                className={
                                  'study-verdict ' +
                                  (!enough ? 'insufficient' : positive ? 'positive' : 'negative')
                                }
                              >
                                {!enough
                                  ? '样本不足'
                                  : positive
                                    ? '本次验证为正'
                                    : '未通过验证'}
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
                <p className="study-footnote">
                  本次预先固定 {report.indicator_study?.trial_count || 7} 组，不从这一个数据集自动挑“最佳指标”。当前只是单次时间顺序切分的初步验证；滚动向前验证、夏普比率多重检验校正与回测过拟合概率检验尚未完成。任一训练或验证段少于{' '}
                  {report.indicator_study?.minimum_closed_trades_per_segment || 30} 笔，结论一律显示样本不足。
                </p>
              </section>
            )}
            <div className="backtest-metrics">
              <div>
                <small>交易次数</small>
                <b>{fmt(metrics.trades as number, 0)}</b>
              </div>
              <div>
                <small>胜率</small>
                <b>
                  {fmt(
                    metrics.win_rate_pct ??
                      (metrics.win_rate == null
                        ? null
                        : metrics.win_rate * 100),
                  )}
                  %
                </b>
              </div>
              <div>
                <small>平均净收益 / 笔</small>
                <b>{fmt(metrics.expectancy_net_pct as number)}%</b>
              </div>
              <div>
                <small>盈利因子</small>
                <b>{fmt(metrics.profit_factor as number)}</b>
              </div>
              <div>
                <small>最大回撤</small>
                <b>{fmt(metrics.max_drawdown_pct as number)}%</b>
              </div>
            </div>
            <div className="comparison-table">
              <table>
                <thead>
                  <tr>
                    <th>波动倍数</th>
                    <th>训练笔数</th>
                    <th>训练净期望</th>
                    <th>验证笔数</th>
                    <th>验证净期望</th>
                    <th>验证回撤</th>
                  </tr>
                </thead>
                <tbody>
                  {comparisons.map((r, n) => {
                    const train = ((r.train as { metrics?: Metrics })
                        ?.metrics || {}) as Metrics,
                      test = ((r.holdout as { metrics?: Metrics })?.metrics ||
                        {}) as Metrics;
                    return (
                      <tr key={n}>
                        <td>
                          {fmt(
                            (r.threshold ?? r.volatility_ratio_min) as number,
                          )}
                          ×
                        </td>
                        <td>{fmt(train.trades as number, 0)}</td>
                        <td>{fmt(train.expectancy_net_pct as number)}%</td>
                        <td>{fmt(test.trades as number, 0)}</td>
                        <td>{fmt(test.expectancy_net_pct as number)}%</td>
                        <td>{fmt(test.max_drawdown_pct as number)}%</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="recommendation">
              <ShieldCheck size={20} />
              <div>
                <b>参数结论</b>
                <p>
                  {formatRecommendation(
                    current?.recommendation ?? report.recommendation,
                  )}
                </p>
                <small>
                  至少需要足够的独立验证交易，才给出数据支持的建议；回测不构成未来收益保证。
                </small>
              </div>
            </div>
            {!!report.limitations?.length && (
              <details className="quality-details" open>
                <summary>数据与模拟边界</summary>
                <ul>
                  {report.limitations.map((x, n) => (
                    <li key={n}>{translateLimitation(x)}</li>
                  ))}
                </ul>
              </details>
            )}
            <details className="quality-details">
              <summary>查看可复现的结果与规则快照</summary>
              <pre>{JSON.stringify(report, null, 2)}</pre>
            </details>
          </div>
        </section>
      )}
      {!report && (
        <section className="panel research-empty">
          <FlaskConical size={32} />
          <h3>还没有这次规则的回测结果</h3>
          <p>
            先选择数据并开始回测。没有真实样本时，不展示模拟胜率或“最优参数”。
          </p>
        </section>
      )}
    </div>
  );
}
function translateLimitation(text: string): string {
  if (text.startsWith('Research simulation'))
    return '仅用于研究模拟，不执行订单，也不保证收益。';
  if (text.startsWith('One symbol/timeframe'))
    return '单一币种和周期；未还原历史全市场名单、已下架合约或新币提醒送达效果，存在存活偏差。';
  if (text.startsWith('Klines alone'))
    return 'K 线无法验证历史 OI、全市场流动性排名、盘口价差、资金费率与订单成交，未用当前快照回填。';
  if (text.startsWith('Fixed per-side'))
    return '费用与滑点采用固定假设；未模拟杠杆、爆仓、账户仓位或资金费率，实际结果可能不同。';
  if (text.startsWith('OHLC intrabar'))
    return 'K 线内部先后顺序未知：同根碰到止盈止损按止损处理。最大浮盈浮亏包含完整退出 K 线，仅作区间描述。';
  if (text.startsWith('Timeframe defaults'))
    return '分周期倍数为待验证起点，不能称为回测最优参数。';
  return text;
}
function formatRecommendation(value: unknown): string {
  if (!value || typeof value !== 'object') return '尚无足够证据调整默认门槛。';
  const r = value as Record<string, unknown>;
  const status = String(r.status || '');
  const threshold = fmt(r.selected_threshold as number);
  return status === 'insufficient_sample'
    ? '训练样本不足：没有门槛达到最低交易数量，暂不推荐最优参数。'
    : status === 'insufficient_holdout'
      ? `训练段选出 ${threshold}×，但验证交易不足 30 笔，尚不能验证。`
      : status === 'holdout_not_profitable'
        ? `训练段选出 ${threshold}×，验证段净期望未通过，不建议据此采用。`
        : status === 'heldout_positive_not_guaranteed'
          ? `训练段选出 ${threshold}×，本次验证段净期望为正；仍需更多币种与未见过的时段验证。`
          : '当前没有足够样本支持修改默认参数。';
}
