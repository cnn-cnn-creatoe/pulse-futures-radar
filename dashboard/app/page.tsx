'use client';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  Radar,
  LayoutDashboard,
  History,
  SlidersHorizontal,
  Radio,
  Send,
  ArrowUpRight,
  Search,
  ChevronRight,
  ChevronLeft,
  Star,
  Download,
  Pause,
  Play,
  RefreshCw,
  ExternalLink,
  ArrowDownUp,
  Check,
  ShieldCheck,
  WifiOff,
  CircleHelp,
  X,
  Database,
  Clock,
  CheckCircle2,
  AlertTriangle,
  Maximize2,
  BarChart3,
  Layers3,
  ChevronDown,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Dialog,
  DialogContent,
  DialogTitle,
  DialogDescription,
} from '@/components/ui/dialog';
import {
  Select,
  SelectTrigger,
  SelectValue,
  SelectContent,
  SelectItem,
} from '@/components/ui/select';
import { Switch } from '@/components/ui/switch';
import {
  Tooltip,
  TooltipContent,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverHeader,
  PopoverTitle,
  PopoverTrigger,
} from '@/components/ui/popover';
import { DownloadPanel } from './download-panel';
import { TelegramSettingsPanel } from './telegram-settings';
import { UpdatePanel } from './update-panel';
import {
  CATEGORIES,
  categoryLabel,
  contractLabel,
  matchesCategory,
  quoteOf,
  assetOf,
  type ContractMeta,
} from './contract-meta';
import {
  ADVANCED_DEFAULTS,
  BacktestPanel,
  ConfirmationPanel,
  IndicatorSettingsPanel,
  positionView,
  ThresholdPanel,
  UniversePanel,
  type AdvancedSettings,
  type IndicatorChartEvent,
  type IndicatorChartLevel,
  type IndicatorChartZone,
  type SignalQuality,
} from './monitor-panels';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';
import {
  type ColumnDef,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  getPaginationRowModel,
  useReactTable,
} from '@tanstack/react-table';
type Candle = {
  open_time: number;
  close_time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  quote_volume: number;
  closed: boolean;
};
type Signal = SignalQuality &
  ContractMeta & {
    id: string;
    symbol: string;
    timeframe: string;
    pattern: string;
    direction: string;
    open_time: number;
    close_time: number;
    price: number;
    change_pct: number;
    volatility_ratio: number;
    v2_pct: number;
    baseline_v2_pct: number;
    range_pct: number;
    relative_volume: number | null;
    quote_volume: number;
    taker_buy_ratio: number | null;
    oi_change_pct: number | null;
    oi_ratio?: number | null;
    oi_structure: string;
    liquidity_percentile: number | null;
    liquidity_rank_scope?: string;
    liquidity_peer_count?: number;
    spread_bps: number | null;
    activity_score: number;
    daily_signal_date?: string;
    daily_signal_count?: number;
    daily_timeframe_signal_count?: number;
    daily_direction_signal_count?: number;
    priority_score: number;
    level: string;
    activity_level?: string;
    score_breakdown: Record<string, number>;
    score_available_max: number;
    warnings: string[];
    baseline_samples: number;
    candles: Candle[];
    source?: string;
    available_confirmations?: {
      id: string;
      name: string;
      passed: boolean;
      reason: string;
    }[];
  };
type Status = {
  state?: string;
  message?: string;
  universe_count?: number;
  ready_symbols?: number;
  ready_pairs?: number;
  warming_pairs?: number;
  timeframe_total?: number;
  last_event_time?: number;
  last_closed_time?: number;
  database?: Record<string, number>;
  [key: string]: unknown;
};
type Settings = AdvancedSettings & {
  volatility_ratio_min: number;
  baseline_method: string;
  trim_percent: number;
  pattern_mode: string;
  allow_doji: boolean;
};
const TFS = ['5m', '15m'];
const PATTERNS = ['阳阳', '阴阴', '阳阴', '阴阳'];
type StrategyId = 'supertrend' | 'smc';
type StrategyMode = 'all' | 'any';
const STRATEGIES: {
  id: StrategyId;
  short: string;
  name: string;
  help: string;
}[] = [
  {
    id: 'supertrend',
    short: '指标1',
    name: '超级趋势',
    help: '用平均真实波幅判断趋势方向；反转形态要求近期发生同向翻转。',
  },
  {
    id: 'smc',
    short: '指标2',
    name: '智慧资金市场结构',
    help: '用已确认的结构突破、结构转折以及支撑阻力判断方向和空间。',
  },
];
const DESCRIPTIONS = ['连续向上', '连续向下', '冲高回落', '下探反弹'];
const BASELINE = [
  ['5m', '7 天', '864'],
  ['15m', '7 天', '288'],
];
const DEFAULT_SETTINGS: Settings = {
  ...ADVANCED_DEFAULTS,
  volatility_ratio_min: 1.5,
  baseline_method: 'trimmed_mean',
  trim_percent: 2.5,
  pattern_mode: 'all',
  allow_doji: false,
};
const number = (v: number | null | undefined, d = 2) =>
  v == null || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString('en-US', {
        maximumFractionDigits: d,
        minimumFractionDigits: d,
      });
const compact = (v: number) =>
  v >= 1e9
    ? number(v / 1e9) + 'B'
    : v >= 1e6
      ? number(v / 1e6) + 'M'
      : v >= 1e3
        ? number(v / 1e3) + 'K'
        : number(v);
const pricePrecision = (v: number) =>
  Math.min(12, Math.max(2, 6 - Math.floor(Math.log10(Math.abs(v) || 1))));
const priceText = (v: number) => number(v, pricePrecision(v));
const signed = (v: number | null) =>
  v == null ? '—' : (v >= 0 ? '+' : '') + number(v) + '%';
const stamp = (v: number | undefined, full = false) =>
  !v
    ? '—'
    : new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        ...(full ? { month: '2-digit', day: '2-digit' } : {}),
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).format(new Date(v));
const quote = quoteOf;
const asset = assetOf;
const patternIndex = (p: string) => Math.max(0, PATTERNS.indexOf(p));
const patternSide = (p: string) =>
  p === '阳阳' || p === '阴阳'
    ? 'long'
    : p === '阴阴' || p === '阳阴'
      ? 'short'
      : 'neutral';
const availableActivityMax = (signal: Signal) => {
  const finite = (value: number | null | undefined) =>
    value != null && Number.isFinite(value);
  return (
    75 +
    (finite(signal.oi_change_pct) && finite(signal.oi_ratio) ? 15 : 0) +
    (finite(signal.liquidity_percentile) ? 8 : 0) +
    (finite(signal.spread_bps) ? 2 : 0)
  );
};
const missingActivityInputs = (signal: Signal) => {
  const finite = (value: number | null | undefined) =>
    value != null && Number.isFinite(value);
  const missing: string[] = [];
  if (!finite(signal.oi_change_pct) || !finite(signal.oi_ratio))
    missing.push('持仓变化');
  if (!finite(signal.liquidity_percentile)) missing.push('流动性排名');
  if (!finite(signal.spread_bps)) missing.push('买卖价差');
  return missing;
};
const displayWarning = (signal: Signal, warning: string) =>
  warning.replace(
    /当前可用满分\s*\d+\s*分/,
    `当前可用满分 ${availableActivityMax(signal)} 分`,
  );
const confirmationList = (signal: Signal) =>
  signal.available_confirmations?.length
    ? signal.available_confirmations
    : signal.confirmations || [];
const strategyPasses = (
  signal: Signal,
  selected: StrategyId[],
  mode: StrategyMode,
) => {
  if (!selected.length) return true;
  const passed = new Map(
    confirmationList(signal).map((item) => [item.id, item.passed]),
  );
  return mode === 'all'
    ? selected.every((id) => passed.get(id) === true)
    : selected.some((id) => passed.get(id) === true);
};
const SCORE_LABELS: [string, string, number][] = [
  ['volume', '相对成交额', 35],
  ['structure', '价格结构', 20],
  ['flow', '主动买卖', 20],
  ['oi', '持仓量变化', 15],
  ['liquidity', '流动性质量', 10],
];
type ChartLayerId =
  | 'volume'
  | 'supertrend'
  | 'supertrendSignals'
  | 'emaFast'
  | 'emaSlow'
  | 'structureEvents'
  | 'structureLevels'
  | 'equalLevels'
  | 'orderBlocks'
  | 'fairValueGaps'
  | 'zones'
  | 'strongWeak';
type ChartLayers = Record<ChartLayerId, boolean>;
const DEFAULT_CHART_LAYERS: ChartLayers = {
  volume: true,
  supertrend: true,
  supertrendSignals: true,
  emaFast: false,
  emaSlow: false,
  structureEvents: true,
  structureLevels: true,
  equalLevels: true,
  orderBlocks: true,
  fairValueGaps: true,
  zones: false,
  strongWeak: true,
};
const CHART_LAYER_OPTIONS: {
  id: ChartLayerId;
  label: string;
  help: string;
  tone?: string;
}[] = [
  {
    id: 'volume',
    label: '成交额',
    help: '每根已收盘 K 线的 USDT 成交额柱。',
  },
  {
    id: 'supertrend',
    label: '超级趋势线',
    help: 'HL2 中间价配合平均真实波幅 ATR10 与 3 倍系数，绿色为上涨、红色为下跌。',
    tone: 'teal',
  },
  {
    id: 'supertrendSignals',
    label: '趋势转向',
    help: '超级趋势在已收盘 K 线上发生的转为上涨或转为下跌；对应原脚本 Buy / Sell 标记。',
    tone: 'amber',
  },
  {
    id: 'emaFast',
    label: '指数均线快线',
    help: '指数移动平均线的快线，默认周期为 20。',
    tone: 'gold',
  },
  {
    id: 'emaSlow',
    label: '指数均线慢线',
    help: '指数移动平均线的慢线，默认周期为 50。',
    tone: 'violet',
  },
  {
    id: 'structureEvents',
    label: '结构突破 / 转折',
    help: '结构突破：收盘价越过已确认结构高低点；结构转折：突破方向与此前结构方向相反。',
    tone: 'blue',
  },
  {
    id: 'structureLevels',
    label: '支撑 / 阻力',
    help: '只显示当时已经确认的内部和波段高低点；不把未来枢轴回画到过去。',
  },
  {
    id: 'equalLevels',
    label: '等高点 / 等低点',
    help: '两个已确认枢轴价格足够接近；等高点常作上方流动性参考，等低点常作下方流动性参考。',
  },
  {
    id: 'orderBlocks',
    label: '订单块',
    help: '结构突破前形成的候选供需价格区；被完整缓解后不再显示。',
  },
  {
    id: 'fairValueGaps',
    label: '公允价值缺口',
    help: '三根 K 线形成的未完全成交价格区，仅使用当前 5 分钟或 15 分钟周期。',
  },
  {
    id: 'zones',
    label: '溢价 / 均衡 / 折价区',
    help: '以已确认波段高低点划分：上部为溢价区，中部为均衡区，下部为折价区。',
  },
  {
    id: 'strongWeak',
    label: '强弱高低点',
    help: '依据已确认波段方向标注强高点、弱高点、强低点和弱低点。',
  },
];
const SMC_LABELS: Record<string, string> = {
  BOS: '结构突破',
  CHoCH: '结构转折',
  EQH: '等高点',
  EQL: '等低点',
  INTERNAL_OB: '内部订单块',
  SWING_OB: '波段订单块',
  BULLISH_FVG: '看涨公允价值缺口',
  BEARISH_FVG: '看跌公允价值缺口',
  PREMIUM: '溢价区',
  EQUILIBRIUM: '均衡区',
  DISCOUNT: '折价区',
  STRONG_HIGH: '强高点',
  WEAK_HIGH: '弱高点',
  STRONG_LOW: '强低点',
  WEAK_LOW: '弱低点',
  BUY: '转为上涨',
  SELL: '转为下跌',
};
const chartLabel = (value: { code?: string; label?: string }) =>
  (value.code && SMC_LABELS[value.code]) || value.label || value.code || '已确认结构';

function ChartLayerControls({
  layers,
  available,
  onToggle,
}: {
  layers: ChartLayers;
  available: Partial<Record<ChartLayerId, boolean>>;
  onToggle: (id: ChartLayerId) => void;
}) {
  return (
    <div className="chart-layer-controls">
      <div className="chart-layer-title">
        <Layers3 size={14} />
        <b>图表指标</b>
        <span>点击开关；鼠标停留可看中文解释</span>
      </div>
      <div className="chart-layer-chips">
        {CHART_LAYER_OPTIONS.map((item) => {
          const hasData = available[item.id] !== false;
          return (
            <button
              key={item.id}
              type="button"
              disabled={!hasData}
              aria-pressed={layers[item.id] && hasData}
              className={`${layers[item.id] && hasData ? 'active' : ''} ${item.tone ? `tone-${item.tone}` : ''}`}
              title={
                hasData
                  ? item.help
                  : `${item.help} 当前信号快照没有这一图层，系统不会猜测补画。`
              }
              onClick={() => onToggle(item.id)}
            >
              <i />
              {item.label}
            </button>
          );
        })}
      </div>
      <p>
        智慧资金市场结构为非重绘改编：只在确认收盘后绘制，已去掉高周期前视与回溯标签，因此不会与 TradingView 原图逐像素相同。
      </p>
    </div>
  );
}
function FilterSelect({
  value,
  onChange,
  items,
  label,
  hint,
}: {
  value: string;
  onChange: (v: string) => void;
  items: [string, string][];
  label: string;
  hint?: string;
}) {
  return (
    <Select value={value} onValueChange={(v) => v && onChange(v)}>
      <SelectTrigger
        aria-label={label}
        aria-description={hint}
        title={hint}
        className="filter-select"
      >
        <SelectValue>{items.find((i) => i[0] === value)?.[1]}</SelectValue>
      </SelectTrigger>
      <SelectContent>
        {items.map(([v, text]) => (
          <SelectItem key={v} value={v}>
            {text}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
function StrategyFilter({
  selected,
  mode,
  onChange,
  onModeChange,
}: {
  selected: StrategyId[];
  mode: StrategyMode;
  onChange: (value: StrategyId[]) => void;
  onModeChange: (value: StrategyMode) => void;
}) {
  const label = selected.length
    ? selected
        .map((id) => STRATEGIES.find((item) => item.id === id)?.short)
        .filter(Boolean)
        .join(' + ')
    : '空策略';
  const toggle = (id: StrategyId) =>
    onChange(
      selected.includes(id)
        ? selected.filter((item) => item !== id)
        : [...selected, id],
    );
  return (
    <Popover>
      <PopoverTrigger
        render={
          <button
            type="button"
            className={'strategy-filter ' + (selected.length ? 'active' : '')}
            aria-label="选择指标策略"
            title="活跃度与策略分开计算；空策略只按活跃度和两根 K 线筛选"
          >
            <Layers3 size={14} />
            <span>策略：{label}</span>
            <ChevronDown size={13} />
          </button>
        }
      />
      <PopoverContent align="start" side="bottom" className="strategy-popover">
        <PopoverHeader>
          <PopoverTitle>选择指标策略</PopoverTitle>
          <PopoverDescription>
            活跃度不绑定指标。空策略保留更多两 K 候选；单选或多选只过滤当前列表。
          </PopoverDescription>
        </PopoverHeader>
        <button
          type="button"
          className={'strategy-option ' + (!selected.length ? 'active' : '')}
          onClick={() => onChange([])}
        >
          <span className="strategy-check">{!selected.length && <Check size={13} />}</span>
          <span>
            <b>空策略</b>
            <small>只看活跃度与两根 K 线形态，候选最多</small>
          </span>
        </button>
        {STRATEGIES.map((item) => (
          <button
            type="button"
            key={item.id}
            className={
              'strategy-option ' + (selected.includes(item.id) ? 'active' : '')
            }
            onClick={() => toggle(item.id)}
            title={item.help}
          >
            <span className="strategy-check">
              {selected.includes(item.id) && <Check size={13} />}
            </span>
            <span>
              <b>
                {item.short} · {item.name}
              </b>
              <small>{item.help}</small>
            </span>
          </button>
        ))}
        {selected.length > 1 && (
          <div className="strategy-mode" aria-label="多指标组合方式">
            <button
              type="button"
              className={mode === 'any' ? 'active' : ''}
              onClick={() => onModeChange('any')}
            >
              任一通过
            </button>
            <button
              type="button"
              className={mode === 'all' ? 'active' : ''}
              onClick={() => onModeChange('all')}
            >
              全部通过
            </button>
          </div>
        )}
        <p className="strategy-footnote">
          “指标通过”只代表该指标与形态方向一致，不等于自动下单或保证盈利。
        </p>
      </PopoverContent>
    </Popover>
  );
}
function PatternIcon({ pattern }: { pattern: string }) {
  return (
    <span
      aria-hidden="true"
      className={'pattern-icon tone-' + patternIndex(pattern)}
    >
      <i />
      <i />
    </span>
  );
}
function Coin({ symbol }: { symbol: string }) {
  const base = asset(symbol);
  return (
    <span
      className={
        'coin coin-' +
        (base === 'BTC'
          ? 'btc'
          : base === 'ETH'
            ? 'eth'
            : base === 'SOL'
              ? 'sol'
              : 'other')
      }
    >
      {base === 'BTC'
        ? '₿'
        : base === 'ETH'
          ? 'Ξ'
          : base === 'SOL'
            ? '≋'
            : base.replace(/^1000/, '').slice(0, 1)}
    </span>
  );
}
function CandleChart({
  signal,
  layers,
  onToggleLayer,
  expanded = false,
}: {
  signal: Signal;
  layers: ChartLayers;
  onToggleLayer: (id: ChartLayerId) => void;
  expanded?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const overlayRef = useRef<HTMLDivElement>(null);
  const [error, setError] = useState('');
  const context = signal.indicators?.chart;
  const smc = signal.indicators?.smc;
  // Keep the chart intentionally simple. Indicators still participate in the
  // saved decision evidence and confirmation panel, but are not painted over
  // the candles unless a later product decision explicitly restores them.
  const showIndicatorLayers = false;
  const supertrendPoints = context?.supertrend || [];
  const derivedFlipCount = supertrendPoints.some(
    (point, index) =>
      index > 0 && point.direction !== supertrendPoints[index - 1].direction,
  );
  const available: Partial<Record<ChartLayerId, boolean>> = {
    volume: Boolean(signal.candles?.length),
    supertrend: Boolean(supertrendPoints.length),
    supertrendSignals: Boolean(context?.flips?.length || derivedFlipCount),
    emaFast: Boolean(context?.ema_fast?.length),
    emaSlow: Boolean(context?.ema_slow?.length),
    structureEvents: Boolean(context?.events?.length || smc?.events?.length),
    structureLevels: Boolean(context?.levels?.some((item) => item.source !== 'equal')),
    equalLevels: Boolean(context?.equal_levels?.length || smc?.equal_levels?.length),
    orderBlocks: Boolean(context?.order_blocks?.length || smc?.order_blocks?.length),
    fairValueGaps: Boolean(context?.fvgs?.length || smc?.fvgs?.length),
    zones: Boolean(context?.zone_bounds?.length || smc?.zone_bounds),
    strongWeak: Boolean(context?.strong_weak?.length),
  };
  useEffect(() => {
    let cancelled = false;
    let cleanup: (() => void) | undefined;
    setError('');
    if (!signal.candles?.length) return;
    import('lightweight-charts')
      .then(
        ({
          createChart,
          CandlestickSeries,
          HistogramSeries,
          LineSeries,
          ColorType,
          createSeriesMarkers,
        }) => {
          if (cancelled || !ref.current) return;
          const chart = createChart(ref.current, {
            autoSize: true,
            height: expanded ? 620 : 360,
            layout: {
              background: { type: ColorType.Solid, color: '#0e1622' },
              textColor: '#70849c',
              fontSize: 10,
              attributionLogo: true,
            },
            grid: {
              vertLines: { color: '#192535' },
              horzLines: { color: '#192535' },
            },
            rightPriceScale: {
              borderColor: '#253142',
              scaleMargins: { top: 0.12, bottom: 0.23 },
            },
            timeScale: {
              borderColor: '#253142',
              timeVisible: true,
              secondsVisible: false,
              rightOffset: 4,
              tickMarkFormatter: (time: unknown) =>
                typeof time === 'number'
                  ? new Intl.DateTimeFormat('zh-CN', {
                      timeZone: 'Asia/Shanghai',
                      ...(signal.timeframe === '1d'
                        ? ({ month: '2-digit', day: '2-digit' } as const)
                        : ({
                            hour: '2-digit',
                            minute: '2-digit',
                            hour12: false,
                          } as const)),
                    }).format(new Date(time * 1000))
                  : '',
            },
            crosshair: {
              vertLine: { color: '#5c748b', labelBackgroundColor: '#253848' },
              horzLine: { color: '#5c748b', labelBackgroundColor: '#253848' },
            },
            localization: {
              locale: 'zh-CN',
              timeFormatter: (t: number) => stamp(t * 1000, true),
            },
          });
          const series = chart.addSeries(CandlestickSeries, {
            upColor: '#49cdb0',
            downColor: '#ed7e91',
            borderVisible: false,
            wickUpColor: '#49cdb0',
            wickDownColor: '#ed7e91',
            priceFormat: {
              type: 'price',
              precision: pricePrecision(signal.price),
              minMove: 10 ** -pricePrecision(signal.price),
            },
          });
          const sorted = [...signal.candles]
            .sort((a, b) => a.open_time - b.open_time)
            .filter((c, i, all) => !i || c.open_time !== all[i - 1].open_time);
          series.setData(
            sorted.map((c) => ({
              time: Math.floor(c.open_time / 1000) as never,
              open: c.open,
              high: c.high,
              low: c.low,
              close: c.close,
            })),
          );
          if (showIndicatorLayers && layers.volume) {
            const volumes = chart.addSeries(HistogramSeries, {
              priceFormat: { type: 'volume' },
              priceScaleId: 'volume',
              lastValueVisible: false,
              priceLineVisible: false,
            });
            chart
              .priceScale('volume')
              .applyOptions({ scaleMargins: { top: 0.84, bottom: 0 } });
            volumes.setData(
              sorted.map((c) => ({
                time: Math.floor(c.open_time / 1000) as never,
                value: c.quote_volume,
                color: c.close >= c.open ? '#49cdb033' : '#ed7e9133',
              })),
            );
          }
          const firstTime = sorted[0].open_time;
          const lastTime = sorted.at(-1)!.open_time;
          const snapOpenTime = (milliseconds: number | undefined) => {
            if (!milliseconds || milliseconds <= firstTime) return firstTime;
            if (milliseconds >= sorted.at(-1)!.close_time) return lastTime;
            return (
              sorted.find(
                (c) => c.open_time <= milliseconds && c.close_time >= milliseconds,
              )?.open_time ||
              sorted.find((c) => c.open_time >= milliseconds)?.open_time ||
              lastTime
            );
          };
          const chartTime = (milliseconds: number | undefined) =>
            Math.floor(snapOpenTime(milliseconds) / 1000) as never;
          if (showIndicatorLayers && context && layers.supertrend) {
            for (const dir of ['up', 'down']) {
              const trend = chart.addSeries(LineSeries, {
                color: dir === 'up' ? '#45dcc0' : '#ef8297',
                lineWidth: 2,
                lastValueVisible: false,
                priceLineVisible: false,
                crosshairMarkerVisible: false,
              });
              trend.setData(
                (context.supertrend || [])
                  .filter(
                    (p) =>
                      p.time >= firstTime && p.time <= sorted.at(-1)!.open_time,
                  )
                  .map((p) =>
                    p.direction === dir
                      ? {
                          time: Math.floor(p.time / 1000) as never,
                          value: p.value,
                        }
                      : { time: Math.floor(p.time / 1000) as never },
                  ),
              );
            }
          }
          if (showIndicatorLayers && context) {
            for (const [points, color, visible] of [
              [context.ema_fast, '#d6b578', layers.emaFast],
              [context.ema_slow, '#a28de3', layers.emaSlow],
            ] as const) {
              if (!visible) continue;
              const line = chart.addSeries(LineSeries, {
                color,
                lineWidth: 1,
                lastValueVisible: false,
                priceLineVisible: false,
              });
              line.setData(
                (points || [])
                  .filter(
                    (p) =>
                      p.time >= firstTime && p.time <= sorted.at(-1)!.open_time,
                  )
                  .map((p) => ({
                    time: Math.floor(p.time / 1000) as never,
                    value: p.value,
                  })),
              );
            }
          }
          const levels = (context?.levels || []).filter(
            (item) => item.confirmed_at <= signal.close_time,
          );
          if (showIndicatorLayers && layers.structureLevels) {
            for (const level of levels
              .filter((item) => item.source !== 'equal')
              .slice(-24)) {
              series.createPriceLine({
                price: level.price,
                color: (level.kind || '').includes('support')
                  ? '#45dcc077'
                  : '#e0b47288',
                lineWidth: 1,
                lineStyle: 2,
                axisLabelVisible: true,
                title: chartLabel(level),
              });
            }
          }
          type LegacyEvent = IndicatorChartEvent & { event?: string };
          type LegacyZone = IndicatorChartZone & { origin_time?: number };
          type LegacyLevel = IndicatorChartLevel & {
            previous_pivot_time?: number;
            pivot_time?: number;
          };
          const events = (
            context?.events?.length ? context.events : smc?.events || []
          ).map((event) => {
            const legacy = event as LegacyEvent;
            return { ...legacy, code: legacy.code || legacy.event };
          });
          const equalLevels = (
            context?.equal_levels?.length
              ? context.equal_levels
              : smc?.equal_levels || []
          ).map((item) => {
            const legacy = item as LegacyLevel;
            return {
              ...legacy,
              start_time:
                legacy.start_time ||
                legacy.previous_pivot_time ||
                legacy.pivot_time ||
                legacy.confirmed_at,
              end_time: legacy.end_time || signal.close_time,
              code: legacy.code || legacy.label,
            };
          });
          if (showIndicatorLayers && layers.equalLevels) {
            for (const level of equalLevels
              .filter((item) => item.confirmed_at <= signal.close_time)
              .slice(-12)) {
              series.createPriceLine({
                price: level.price,
                color: level.code === 'EQL' ? '#35d5b8aa' : '#f0b16aaa',
                lineWidth: 1,
                lineStyle: 3,
                axisLabelVisible: true,
                title: chartLabel(level),
              });
            }
          }
          const normalizeZones = (
            current: IndicatorChartZone[] | undefined,
            fallback: IndicatorChartZone[] | undefined,
          ) =>
            (current?.length ? current : fallback || []).map((item) => {
              const legacy = item as LegacyZone;
              return {
                ...legacy,
                start_time:
                  legacy.start_time || legacy.origin_time || legacy.confirmed_at,
                end_time: legacy.end_time || signal.close_time,
              };
            });
          const orderBlocks = normalizeZones(
            context?.order_blocks,
            smc?.order_blocks,
          );
          const fairValueGaps = normalizeZones(context?.fvgs, smc?.fvgs);
          let zoneBounds: IndicatorChartZone[] = context?.zone_bounds || [];
          if (!zoneBounds.length && smc?.zone_bounds) {
            const bounds = smc.zone_bounds;
            const width = bounds.high - bounds.low;
            zoneBounds = [
              {
                start_time: bounds.confirmed_at,
                end_time: signal.close_time,
                low: bounds.high - width * 0.25,
                high: bounds.high,
                direction: 'down',
                code: 'PREMIUM',
                label: '溢价区',
                confirmed_at: bounds.confirmed_at,
              },
              {
                start_time: bounds.confirmed_at,
                end_time: signal.close_time,
                low: bounds.equilibrium - width * 0.025,
                high: bounds.equilibrium + width * 0.025,
                direction: 'neutral',
                code: 'EQUILIBRIUM',
                label: '均衡区',
                confirmed_at: bounds.confirmed_at,
              },
              {
                start_time: bounds.confirmed_at,
                end_time: signal.close_time,
                low: bounds.low,
                high: bounds.low + width * 0.25,
                direction: 'up',
                code: 'DISCOUNT',
                label: '折价区',
                confirmed_at: bounds.confirmed_at,
              },
            ];
          }
          const visibleZones: { item: IndicatorChartZone; kind: string }[] = [
            ...(showIndicatorLayers && layers.orderBlocks
              ? orderBlocks.slice(-10).map((item) => ({ item, kind: 'order-block' }))
              : []),
            ...(showIndicatorLayers && layers.fairValueGaps
              ? fairValueGaps.slice(-10).map((item) => ({ item, kind: 'fair-gap' }))
              : []),
            ...(showIndicatorLayers && layers.zones
              ? zoneBounds.map((item) => ({ item, kind: 'value-zone' }))
              : []),
          ].filter(({ item }) => item.confirmed_at <= signal.close_time);
          for (const { item, kind } of visibleZones.slice(-16)) {
            series.createPriceLine({
              price: item.high,
              color:
                kind === 'fair-gap'
                  ? '#e6a15a55'
                  : item.direction === 'up'
                    ? '#3ad2b455'
                    : item.direction === 'down'
                      ? '#ed708355'
                      : '#8b97aa44',
              lineWidth: 1,
              lineStyle: 2,
              axisLabelVisible: false,
              title: chartLabel(item),
            });
          }
          let flips = context?.flips || [];
          if (!flips.length) {
            flips = (context?.supertrend || []).flatMap((point, index, all) =>
              index && point.direction !== all[index - 1].direction
                ? [
                    {
                      time: point.time,
                      price: point.value,
                      direction: point.direction,
                      source: 'supertrend',
                      code: point.direction === 'up' ? 'BUY' : 'SELL',
                      label:
                        point.direction === 'up' ? '转为上涨' : '转为下跌',
                    },
                  ]
                : [],
            );
          }
          const markers: {
            time: never;
            position: 'aboveBar' | 'belowBar' | 'inBar';
            color: string;
            shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square';
            text: string;
          }[] = [];
          if (showIndicatorLayers && layers.supertrendSignals) {
            for (const event of flips.slice(-30)) {
              const up = event.direction === 'up';
              markers.push({
                time: chartTime(event.time),
                position: up ? 'belowBar' : 'aboveBar',
                color: up ? '#43d8bc' : '#ef758a',
                shape: up ? 'arrowUp' : 'arrowDown',
                text: chartLabel(event),
              });
            }
          }
          if (showIndicatorLayers && layers.structureEvents) {
            for (const event of events
              .filter((item) => (item.confirmed_at || item.time) <= signal.close_time)
              .slice(-40)) {
              const up = event.direction === 'up';
              markers.push({
                time: chartTime(event.time || event.confirmed_at),
                position: up ? 'belowBar' : 'aboveBar',
                color: up ? '#24c9b1' : '#ec6d82',
                shape: 'circle',
                text: `${event.source === 'swing' ? '波段' : '内部'}${chartLabel(event)}`,
              });
            }
          }
          if (showIndicatorLayers && layers.strongWeak) {
            for (const event of (context?.strong_weak || []).slice(-20)) {
              const low = event.code?.includes('LOW');
              markers.push({
                time: chartTime(event.time || event.confirmed_at),
                position: low ? 'belowBar' : 'aboveBar',
                color: event.code?.startsWith('STRONG') ? '#61d6bd' : '#d5a96d',
                shape: 'square',
                text: chartLabel(event),
              });
            }
          }
          const last = sorted.at(-1);
          if (last) {
            const screenedDirection =
              signal.qualified && signal.trade_plan?.direction === 'long'
                ? '参考买点'
                : signal.qualified && signal.trade_plan?.direction === 'short'
                  ? '参考卖点'
                : '仅观察';
            const isBuyPoint = screenedDirection === '参考买点';
            const isSellPoint = screenedDirection === '参考卖点';
            markers.push({
              time: Math.floor(last.open_time / 1000) as never,
              position: isBuyPoint ? 'belowBar' : 'aboveBar',
              color: isBuyPoint ? '#45d4b7' : isSellPoint ? '#ed788c' : '#eac180',
              shape: isBuyPoint ? 'arrowUp' : isSellPoint ? 'arrowDown' : 'circle',
              text:
                signal.pattern +
                ` · ${screenedDirection} · 异常 ` +
                number(signal.volatility_ratio) +
                '×',
            });
          }
          markers.sort((a, b) => Number(a.time) - Number(b.time));
          createSeriesMarkers(series, markers as never);
          const drawZoneOverlay = () => {
            if (cancelled) return;
            const overlay = overlayRef.current;
            if (!overlay) return;
            overlay.replaceChildren();
            for (const { item, kind } of visibleZones.slice(-16)) {
              const high = series.priceToCoordinate(item.high);
              const low = series.priceToCoordinate(item.low);
              const start = chart.timeScale().timeToCoordinate(chartTime(item.start_time));
              const end = chart.timeScale().timeToCoordinate(chartTime(item.end_time));
              if (high == null || low == null) continue;
              const left = Math.max(0, start == null ? 0 : start);
              const right = Math.min(
                overlay.clientWidth,
                end == null || item.extend_right ? overlay.clientWidth - 54 : end,
              );
              if (right <= left) continue;
              const box = document.createElement('div');
              box.className = `chart-zone-box ${kind} zone-${item.direction}`;
              box.style.left = `${left}px`;
              box.style.top = `${Math.min(high, low)}px`;
              box.style.width = `${Math.max(2, right - left)}px`;
              box.style.height = `${Math.max(2, Math.abs(low - high))}px`;
              const label = document.createElement('span');
              label.textContent = chartLabel(item);
              box.appendChild(label);
              overlay.appendChild(box);
            }
          };
          chart.timeScale().fitContent();
          chart.timeScale().subscribeVisibleLogicalRangeChange(drawZoneOverlay);
          const observer = new ResizeObserver(drawZoneOverlay);
          observer.observe(ref.current);
          requestAnimationFrame(drawZoneOverlay);
          cleanup = () => {
            observer.disconnect();
            chart.timeScale().unsubscribeVisibleLogicalRangeChange(drawZoneOverlay);
            chart.remove();
          };
        },
      )
      .catch(() => setError('图表暂时无法加载，触发数据仍可在下方查看。'));
    return () => {
      cancelled = true;
      cleanup?.();
    };
  }, [signal, expanded, layers]);
  return (
    <div className="chart-workbench">
      <p className="simple-chart-note">
        图中仅显示已收盘 K 线与两根 K 线形态；超级趋势和智慧资金市场结构只参与后台判断。
      </p>
      <div
        className="candle-chart"
        style={{ height: expanded ? 620 : 360 }}
      >
        <div ref={ref} className="chart-render-host" />
        <div ref={overlayRef} className="chart-zone-overlay" aria-hidden="true" />
        {error && <p className="chart-error">{error}</p>}
      </div>
    </div>
  );
}
export default function Home() {
  const [view, setView] = useState('overview');
  const [mode, setMode] = useState<'demo' | 'live'>('demo');
  const [demo, setDemo] = useState<Signal[]>([]);
  const [live, setLive] = useState<Signal[]>([]);
  const [status, setStatus] = useState<Status>({});
  const [api, setApi] = useState<string | null>(null);
  const [apiOnline, setApiOnline] = useState(false);
  const [loading, setLoading] = useState(true);
  const [dataError, setDataError] = useState('');
  const [signalError, setSignalError] = useState('');
  const [retryBusy, setRetryBusy] = useState(false);
  const refreshing = useRef(false);
  const [lastSync, setLastSync] = useState<number>();
  const [selectedId, setSelectedId] = useState('');
  const [pattern, setPattern] = useState('all');
  const [timeframe, setTimeframe] = useState('all');
  const [query, setQuery] = useState('');
  const [minScore, setMinScore] = useState('65');
  const [categoryFilter, setCategoryFilter] = useState('all');
  const [dateFilter, setDateFilter] = useState('all');
  const [onlyStars, setOnlyStars] = useState(false);
  const [onlyQualified, setOnlyQualified] = useState(false);
  const [selectedStrategies, setSelectedStrategies] = useState<StrategyId[]>([]);
  const [strategyMode, setStrategyMode] = useState<StrategyMode>('all');
  const [stars, setStars] = useState<string[]>([]);
  const [paused, setPaused] = useState(false);
  const [sorting, setSorting] = useState<SortingState>([
    { id: 'close_time', desc: true },
  ]);
  const [help, setHelp] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [chartLayers, setChartLayers] = useState<ChartLayers>(
    DEFAULT_CHART_LAYERS,
  );
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [draft, setDraft] = useState<Settings>(DEFAULT_SETTINGS);
  const [saving, setSaving] = useState(false);
  const [toast, setToast] = useState('');
  const [clock, setClock] = useState('UTC+8');
  const notify = (message: string) => {
    setToast(message);
    window.setTimeout(() => setToast(''), 4200);
  };
  const updateStrategySelection = useCallback((next: StrategyId[]) => {
    setSelectedStrategies(next);
    try {
      localStorage.setItem('pulse-signal-strategies', JSON.stringify(next));
    } catch {}
  }, []);
  const updateStrategyMode = useCallback((next: StrategyMode) => {
    setStrategyMode(next);
    try {
      localStorage.setItem('pulse-signal-strategy-mode', next);
    } catch {}
  }, []);
  const toggleChartLayer = useCallback((id: ChartLayerId) => {
    setChartLayers((previous) => {
      const next = { ...previous, [id]: !previous[id] };
      try {
        localStorage.setItem('pulse-chart-layers', JSON.stringify(next));
      } catch {}
      return next;
    });
  }, []);
  useEffect(() => {
    const host = window.location.hostname;
    if (new URLSearchParams(window.location.search).get('view') === 'downloads')
      setView('universe');
    if (host === '127.0.0.1' || host === 'localhost') {
      setApi('http://127.0.0.1:8765');
      setMode('live');
    }
    try {
      const saved = JSON.parse(localStorage.getItem('pulse-watchlist') || '[]');
      if (Array.isArray(saved))
        setStars(saved.filter((x) => typeof x === 'string'));
    } catch {}
    try {
      const savedStrategies = JSON.parse(
        localStorage.getItem('pulse-signal-strategies') || '[]',
      );
      if (Array.isArray(savedStrategies))
        setSelectedStrategies(
          savedStrategies.filter((item): item is StrategyId =>
            STRATEGIES.some((strategy) => strategy.id === item),
          ),
        );
      const savedMode = localStorage.getItem('pulse-signal-strategy-mode');
      if (savedMode === 'all' || savedMode === 'any')
        setStrategyMode(savedMode);
    } catch {}
    try {
      const savedLayers = JSON.parse(
        localStorage.getItem('pulse-chart-layers') || '{}',
      );
      if (savedLayers && typeof savedLayers === 'object')
        setChartLayers({ ...DEFAULT_CHART_LAYERS, ...savedLayers });
    } catch {}
    const tick = () => setClock(stamp(Date.now()) + ' UTC+8');
    tick();
    const timer = setInterval(tick, 1000);
    fetch('/demo-signals.json')
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json() as Promise<{ items: Signal[] }>;
      })
      .then((d) => {
        setDemo(
          d.items.map((s) =>
            s.source === 'demo'
              ? {
                  ...s,
                  quote_asset: quote(s),
                  contract_type: 'PERPETUAL',
                  underlying_type: 'COIN',
                }
              : s,
          ),
        );
        setLoading(false);
      })
      .catch(() => {
        setDataError('演示样本加载失败，请刷新页面。');
        setLoading(false);
      });
    return () => clearInterval(timer);
  }, []);
  const refresh = useCallback(async () => {
    if (!api || refreshing.current) return;
    refreshing.current = true;
    try {
      try {
        const r = await fetch(api + '/api/status', {
          signal: AbortSignal.timeout(8000),
        });
        if (!r.ok) throw Error();
        setStatus(await r.json());
        setApiOnline(true);
      } catch {
        setApiOnline(false);
        return;
      }
      if (!paused) {
        try {
          const data = await fetch(
            api + '/api/signals?mode=live&limit=1000&include_candles=false',
            {
              signal: AbortSignal.timeout(8000),
            },
          );
          if (!data.ok) throw Error();
          const incoming = ((await data.json()) as { items: Signal[] }).items;
          // Stored alerts are immutable. Retain hydrated detail and chart identity.
          setLive((previous) => {
            const existing = new Map(previous.map((s) => [s.id, s]));
            return incoming.map(
              (s) => existing.get(s.id) || { ...s, candles: [] },
            );
          });
          setLastSync(Date.now());
          setSignalError('');
        } catch {
          setSignalError(
            '信号列表暂时读取较慢；已显示记录保留，行情采集状态以顶部为准。',
          );
        }
      }
    } finally {
      refreshing.current = false;
    }
  }, [api, paused]);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);
  useEffect(() => {
    if (!api) return;
    fetch(api + '/api/settings', { signal: AbortSignal.timeout(4000) })
      .then((r) => r.json() as Promise<Settings>)
      .then((s) => {
        const next = {
          ...DEFAULT_SETTINGS,
          volatility_ratio_min: s.volatility_ratio_min,
          baseline_method: s.baseline_method,
          trim_percent: s.trim_percent,
          pattern_mode: s.pattern_mode,
          allow_doji: s.allow_doji,
          threshold_profile: s.threshold_profile || 'timeframe',
          indicators: { ...ADVANCED_DEFAULTS.indicators, ...s.indicators },
          quality: { ...ADVANCED_DEFAULTS.quality, ...s.quality },
        };
        setSettings(next);
        setDraft(next);
      })
      .catch(() => {});
  }, [api, apiOnline]);
  const toggleStar = useCallback(
    (symbol: string) =>
      setStars((previous) => {
        const next = previous.includes(symbol)
          ? previous.filter((s) => s !== symbol)
          : [...previous, symbol];
        try {
          localStorage.setItem('pulse-watchlist', JSON.stringify(next));
        } catch {}
        return next;
      }),
    [],
  );
  const source = useMemo(
    () =>
      (mode === 'demo' ? demo : live).filter(
        (s) => quote(s) === 'USDT' && TFS.includes(s.timeframe),
      ),
    [mode, demo, live],
  );
  const filtered = useMemo(
    () =>
      source.filter(
        (s) =>
          (mode === 'demo' ||
            !onlyQualified ||
            (s.qualified === true && s.quality_schema_version === 2)) &&
          (pattern === 'all' || s.pattern === pattern) &&
          (timeframe === 'all' || s.timeframe === timeframe) &&
          s.symbol.toLowerCase().includes(query.trim().toLowerCase()) &&
          s.activity_score >= Number(minScore) &&
          strategyPasses(s, selectedStrategies, strategyMode) &&
          matchesCategory(s, categoryFilter) &&
          (!onlyStars || stars.includes(s.symbol)) &&
          (dateFilter === 'all' ||
            s.close_time >= Date.now() - Number(dateFilter) * 86400000),
      ),
    [
      source,
      mode,
      onlyQualified,
      pattern,
      timeframe,
      query,
      minScore,
      selectedStrategies,
      strategyMode,
      categoryFilter,
      onlyStars,
      stars,
      dateFilter,
    ],
  );
  const selected = filtered.find((s) => s.id === selectedId) || filtered[0];
  const [detailFailure, setDetailFailure] = useState('');
  const [detailRetry, setDetailRetry] = useState(0);
  useEffect(() => {
    setDetailFailure('');
    if (!api || mode !== 'live' || !selected || selected.candles?.length)
      return;
    const controller = new AbortController();
    const timeout = setTimeout(() => {
      setDetailFailure('K 线详情读取超时');
      controller.abort();
    }, 8000);
    const id = selected.id;
    fetch(api + '/api/signals/' + encodeURIComponent(id), {
      signal: controller.signal,
    })
      .then((r) => {
        if (!r.ok) throw Error();
        return r.json() as Promise<{ item: Signal }>;
      })
      .then(({ item }) =>
        setLive((previous) => previous.map((s) => (s.id === id ? item : s))),
      )
      .catch(() => {
        if (!controller.signal.aborted) setDetailFailure('K 线详情加载失败');
      })
      .finally(() => clearTimeout(timeout));
    return () => {
      controller.abort();
      clearTimeout(timeout);
    };
  }, [api, mode, selected?.id, selected?.candles?.length, detailRetry]);
  const patternCounts = PATTERNS.map(
    (p) => source.filter((s) => s.pattern === p).length,
  );
  const connected =
    apiOnline && ['running', 'live', 'ready'].includes(status.state || '');
  const blocked =
    status.state === 'blocked' ||
    status.state === 'restricted' ||
    String(status.message || '').includes('451');
  const heading: Record<string, string> = {
    overview: '市场总览',
    history: '信号记录',
    universe: '行情下载与新币',
    backtest: '历史回测',
    rules: '监测规则',
    system: '系统与推送',
  };
  const statusText = !api
    ? '在线演示版'
    : !apiOnline
      ? '本地服务离线'
      : blocked
        ? '行情访问受限'
        : connected
          ? '行情监测中'
          : status.state === 'warming'
            ? '历史数据预热中'
            : (
                {
                  reconnecting: '行情重连中',
                  degraded: '部分数据补齐中',
                  stopped: '采集已停止',
                  error: '行情连接失败',
                } as Record<string, string>
              )[status.state || ''] || '行情连接中';
  const columns = useMemo<ColumnDef<Signal>[]>(
    () => [
      {
        id: 'favorite',
        header: '',
        cell: ({ row }) => (
          <button
            className={
              'star-button ' +
              (stars.includes(row.original.symbol) ? 'is-starred' : '')
            }
            aria-label={
              (stars.includes(row.original.symbol) ? '取消关注 ' : '关注 ') +
              row.original.symbol
            }
            onClick={(e) => {
              e.stopPropagation();
              toggleStar(row.original.symbol);
            }}
          >
            <Star size={13} />
          </button>
        ),
        enableSorting: false,
      },
      {
        accessorKey: 'symbol',
        header: '交易对',
        cell: ({ row }) => (
          <div className="symbol-cell">
            <Coin symbol={row.original.symbol} />
            <div>
              <b>
                {row.original.symbol.includes('_')
                  ? row.original.symbol
                  : asset(row.original)}
              </b>
              <small>
                {quote(row.original)} · {contractLabel(row.original)}
              </small>
              <small>{categoryLabel(row.original)}</small>
              <small
                className="symbol-daily-count"
                title={
                  row.original.daily_signal_count == null
                    ? '演示样本或旧接口暂未提供今日统计'
                    : `北京时间 ${row.original.daily_signal_date || '当日'}：本合约写入系统的原始异常共 ${row.original.daily_signal_count} 次；其中 ${row.original.timeframe} 周期 ${row.original.daily_timeframe_signal_count ?? 0} 次，同方向 ${row.original.daily_direction_signal_count ?? 0} 次。北京时间次日 00:00 重新计数。`
                }
              >
                {row.original.daily_signal_count == null
                  ? '今日次数 —'
                  : `今日 ${row.original.daily_signal_count} 次`}
              </small>
            </div>
          </div>
        ),
      },
      {
        accessorKey: 'timeframe',
        header: '周期',
        cell: ({ getValue }) => (
          <span className="tf-pill">{getValue<string>()}</span>
        ),
      },
      {
        accessorKey: 'pattern',
        header: () => (
          <span title="两根已收盘 K 线的组合：阳阳/阴阴偏趋势，阴阳/阳阴偏反转">
            K 线形态
          </span>
        ),
        cell: ({ getValue, row }) => (
          <span
            title={(row.original.quality_reasons || []).join('；')}
            className={'pattern-tag tone-' + patternIndex(getValue<string>())}
          >
            {getValue<string>()}
          </span>
        ),
      },
      {
        id: 'decision',
        header: () => (
          <span title="这里只表示两根 K 线与已选指标形成的候选方向，不等于可以入场；是否适合入场请看右侧位置判断">
            候选方向
          </span>
        ),
        cell: ({ row }) => {
          const signal = row.original;
          const side = patternSide(signal.pattern);
          const fullQuality =
            signal.qualified === true && signal.quality_schema_version === 2;
          const prefix = fullQuality
            ? '强筛'
            : selectedStrategies.length
              ? '指标'
              : '两K';
          const label =
            side === 'long'
              ? `${prefix}多向候选`
              : side === 'short'
                ? `${prefix}空向候选`
                : '方向待定';
          const strategyText = selectedStrategies.length
            ? `已通过${selectedStrategies
                .map(
                  (id) => STRATEGIES.find((item) => item.id === id)?.name || id,
                )
                .join(strategyMode === 'all' ? ' + ' : ' / ')}筛选。`
            : '当前为空策略，仅按活跃度与两根 K 线形态显示。';
          const explanation = `${strategyText}${
            fullQuality
              ? '全部质量与空间门槛也已通过。'
              : `尚未通过全部强筛：${(signal.quality_reasons || []).join('；') || '质量或空间条件不足'}。`
          } 方向只用于观察，不是自动下单或收益保证。`;
          return (
            <span
              className={
                'decision-tag ' +
                (side === 'long'
                  ? 'long'
                  : side === 'short'
                    ? 'short'
                    : 'wait')
              }
              title={explanation}
            >
              {label}
            </span>
          );
        },
        enableSorting: false,
      },
      {
        id: 'position',
        header: () => (
          <span title="指标2使用信号当时已经收盘的 K 线，判断前方压力、下方支撑、区间中部或已站稳突破；旧记录缺少快照时不会补算">
            位置判断
          </span>
        ),
        cell: ({ row }) => {
          const view = positionView(row.original);
          return (
            <span
              className={`position-pill ${view.status}`}
              title={`${view.context}：${view.reason}；${
                view.assessment?.uses_closed_bars_only
                  ? '仅使用信号时已经收盘的K线'
                  : '旧记录未补算'
              }`}
            >
              <b>{view.title}</b>
              <small>{view.context}</small>
            </span>
          );
        },
        enableSorting: false,
      },
      {
        accessorKey: 'volatility_ratio',
        header: () => (
          <span title="当前两根 K 线的 NTR 总和 ÷ 同币种同周期历史两根 NTR 均值；越高表示相对自身历史越异常">
            波动倍数
          </span>
        ),
        cell: ({ getValue }) => (
          <span className="ratio">
            {number(getValue<number>())}
            <small> ×</small>
          </span>
        ),
      },
      {
        accessorKey: 'change_pct',
        header: () => (
          <span title="两根已收盘 K 线从第一根开盘到第二根收盘的净涨跌幅">
            两根涨跌
          </span>
        ),
        cell: ({ getValue }) => (
          <span className={getValue<number>() >= 0 ? 'up' : 'down'}>
            {signed(getValue<number>())}
          </span>
        ),
      },
      {
        accessorKey: 'relative_volume',
        header: () => (
          <span title="当前两根成交额相对同币种同周期历史基准的倍数；与波动倍数独立计算">
            相对成交额
          </span>
        ),
        cell: ({ getValue }) => (
          <span>
            {number(getValue<number>())}
            <span className="muted"> ×</span>
          </span>
        ),
      },
      {
        accessorKey: 'activity_score',
        header: () => (
          <span title="成交额、价格结构、主动买卖、持仓量与流动性构成的可用信息分数；缺失项不会伪造补分">
            活跃度
          </span>
        ),
        cell: ({ row }) => (
          <div className="score-cell">
            <b>{row.original.activity_score}</b>
            <div className="score-track">
              <i
                style={{
                  width:
                    Math.min(
                      100,
                      (row.original.activity_score /
                        availableActivityMax(row.original)) *
                        100,
                    ) + '%',
                  background:
                    row.original.activity_score >= 80 ? '#4bdac1' : '#738ad5',
                }}
              />
            </div>
            <span
              className="muted"
              title="分母是本条记录实际具备的数据满分；缺失项不会补造"
            >
              /{availableActivityMax(row.original)}
            </span>
          </div>
        ),
      },
      {
        accessorKey: 'close_time',
        header: '收线时间',
        cell: ({ getValue }) => (
          <span className="muted time-cell">
            {stamp(getValue<number>(), true)}
          </span>
        ),
      },
      {
        id: 'detail',
        header: '',
        cell: ({ row }) => (
          <button
            className="row-detail"
            aria-label={
              '查看 ' +
              row.original.symbol +
              ' ' +
              row.original.timeframe +
              ' 信号详情'
            }
            onClick={() => {
              setSelectedId(row.original.id);
              setView('overview');
            }}
          >
            <ChevronRight size={16} />
          </button>
        ),
        enableSorting: false,
      },
    ],
    [stars, toggleStar, selectedStrategies, strategyMode],
  );
  const table = useReactTable({
    data: filtered,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 8 } },
  });
  function resetFilters() {
    setPattern('all');
    setTimeframe('all');
    setQuery('');
    setMinScore('65');
    setCategoryFilter('all');
    setOnlyStars(false);
    setDateFilter('all');
    updateStrategySelection([]);
    updateStrategyMode('all');
  }
  function switchMode(next: 'demo' | 'live') {
    setMode(next);
    setSelectedId('');
    resetFilters();
    setPaused(false);
  }
  function exportCsv() {
    if (!filtered.length) return;
    const escape = (v: unknown) =>
      '"' +
      String(v ?? '')
        .replace(/^[=+@-]/, "'$&")
        .replace(/"/g, '""') +
      '"';
    const fields = [
      'source',
      'symbol',
      'timeframe',
      'pattern',
      'close_time',
      'volatility_ratio',
      'v2_pct',
      'baseline_v2_pct',
      'activity_score',
      'activity_available_max',
      'daily_signal_count',
      'daily_timeframe_signal_count',
      'daily_direction_signal_count',
      'daily_signal_date',
      'change_pct',
      'relative_volume',
      'oi_change_pct',
      'candidate_direction',
      'entry_advice',
      'position_status',
      'position_label',
      'position_reason',
      'position_zone_low',
      'position_zone_high',
      'position_uses_closed_bars_only',
      'selected_strategy',
      'strategy_mode',
      'supertrend_pass',
      'smc_pass',
    ];
    const valueFor = (signal: Signal, key: string) => {
      const confirmations = new Map(
        confirmationList(signal).map((item) => [item.id, item.passed]),
      );
      if (key === 'source') return mode;
      if (key === 'activity_available_max') return availableActivityMax(signal);
      if (key === 'candidate_direction')
        return patternSide(signal.pattern) === 'long' ? '做多' : '做空';
      if (key === 'entry_advice') return positionView(signal).title;
      if (key === 'position_status') return positionView(signal).status;
      if (key === 'position_label') return positionView(signal).context;
      if (key === 'position_reason') return positionView(signal).reason;
      if (key === 'position_zone_low')
        return signal.position_assessment?.zone_low;
      if (key === 'position_zone_high')
        return signal.position_assessment?.zone_high;
      if (key === 'position_uses_closed_bars_only')
        return signal.position_assessment?.uses_closed_bars_only === true;
      if (key === 'selected_strategy')
        return selectedStrategies.length
          ? selectedStrategies
              .map((id) => STRATEGIES.find((item) => item.id === id)?.name)
              .join('+')
          : '空策略';
      if (key === 'strategy_mode')
        return selectedStrategies.length > 1
          ? strategyMode === 'all'
            ? '全部通过'
            : '任一通过'
          : '不适用';
      if (key === 'supertrend_pass') return confirmations.get('supertrend');
      if (key === 'smc_pass') return confirmations.get('smc');
      return (signal as unknown as Record<string, unknown>)[key];
    };
    const text =
      '\ufeff' +
      [
        fields.join(','),
        ...filtered.map((s) =>
          fields
            .map((k) => escape(valueFor(s, k)))
            .join(','),
        ),
      ].join('\r\n');
    const url = URL.createObjectURL(
      new Blob([text], { type: 'text/csv;charset=utf-8;' }),
    );
    const a = document.createElement('a');
    a.href = url;
    a.download =
      'PULSE-' + mode + '-' + new Date().toISOString().slice(0, 10) + '.csv';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 500);
    notify(
      '已导出 ' +
        filtered.length +
        ' 条' +
        (mode === 'demo' ? '演示' : '真实') +
        '信号',
    );
  }
  async function saveSettings() {
    if (!api || !apiOnline) return;
    setSaving(true);
    try {
      const result = await fetch(api + '/api/settings', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-Pulse-Client': 'dashboard',
        },
        body: JSON.stringify(draft),
        signal: AbortSignal.timeout(5000),
      });
      if (!result.ok) throw Error();
      setSettings(draft);
      notify('监测规则已保存，将用于之后的新信号。');
    } catch {
      notify('保存失败，原规则未被替换。请检查服务或参数范围。');
    } finally {
      setSaving(false);
    }
  }
  const signalTable = (
    <section className="panel signals-panel">
      {mode === 'live' && signalError && (
        <p className="form-notice">{signalError}</p>
      )}
      <div className="panel-heading">
        <div className="panel-label">
          <h2>
            <Radar size={17} />
            {mode === 'live' && onlyQualified
              ? '强信号候选'
              : selectedStrategies.length
                ? '指标确认候选'
                : '两 K 异常候选'}
          </h2>
          <span className="count-pill">{filtered.length}</span>
          <span className="muted desktop-only">
            活跃度独立计分 · 当前{selectedStrategies.length ? '已选指标' : '为空策略'}
          </span>
        </div>
        <div className="table-actions">
          {mode === 'live' && (
            <Tooltip>
              <TooltipTrigger
                render={
                  <button
                    className={
                      'quality-toggle ' + (onlyQualified ? 'active' : '')
                    }
                    onClick={() => setOnlyQualified((v) => !v)}
                    aria-pressed={onlyQualified}
                  >
                    {onlyQualified ? '仅通过强筛选' : '全部原始异常'}
                  </button>
                }
              />
              <TooltipContent side="bottom" className="pulse-tooltip">
                {onlyQualified
                  ? '当前只显示：异常倍数、指标共振、流动性、成交额、支撑阻力空间、成本与风险收益均通过的候选。'
                  : '当前显示达到基础异常门槛的记录，再按右侧“策略”筛选；活跃度与指标结果互不加分。这些记录不是可直接交易的信号。'}
              </TooltipContent>
            </Tooltip>
          )}
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setPaused((p) => !p)}
            disabled={mode === 'demo'}
            title="暂停仅冻结页面刷新，后台继续监测"
          >
            {paused ? <Play /> : <Pause />}
            {paused ? '继续刷新' : '暂停刷新'}
          </Button>
          <Button
            variant="outline"
            size="sm"
            onClick={exportCsv}
            disabled={!filtered.length}
          >
            <Download />
            导出
          </Button>
        </div>
      </div>
      <div className="filters">
        <div className="search-box">
          <Search size={15} />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="搜索交易对"
            placeholder="搜索 BTC / TSLA / XAU / 260925"
          />
        </div>
        <FilterSelect
          label="筛选周期"
          hint="选择 K 线周期；每个周期使用各自的历史基准和最低异常倍数。"
          value={timeframe}
          onChange={setTimeframe}
          items={[
            ['all', '全部（5m / 15m）'],
            ...TFS.map((x) => [x, x] as [string, string]),
          ]}
        />
        <FilterSelect
          label="筛选形态"
          hint="阳阳/阴阴用于趋势延续检查；阴阳/阳阴用于方向反转检查。"
          value={pattern}
          onChange={setPattern}
          items={[
            ['all', '全部形态'],
            ...PATTERNS.map((x) => [x, x] as [string, string]),
          ]}
        />
        <FilterSelect
          label="最低活跃度"
          hint="活跃度独立于指标，由成交额、价格结构、主动买卖、持仓与流动性组成；这里只过滤页面显示。"
          value={minScore}
          onChange={setMinScore}
          items={[
            ['0', '全部活跃度'],
            ['65', '活跃 ≥65（默认）'],
            ['80', '强活跃 ≥80'],
            ['90', '极强 ≥90'],
          ]}
        />
        <FilterSelect
          label="资产类别"
          hint="按币安官方合约元数据区分加密资产、股票、商品、指数等类别。"
          value={categoryFilter}
          onChange={setCategoryFilter}
          items={CATEGORIES}
        />
        <StrategyFilter
          selected={selectedStrategies}
          mode={strategyMode}
          onChange={updateStrategySelection}
          onModeChange={updateStrategyMode}
        />
        {view === 'history' && (
          <FilterSelect
            label="日期范围"
            value={dateFilter}
            onChange={setDateFilter}
            items={[
              ['all', '全部日期'],
              ['1', '最近 24 小时'],
              ['7', '最近 7 天'],
            ]}
          />
        )}
        <Button
          className={onlyStars ? 'star-filter active' : 'star-filter'}
          variant="ghost"
          size="icon"
          onClick={() => setOnlyStars((v) => !v)}
          aria-pressed={onlyStars}
          aria-label="仅看关注"
        >
          <Star />
        </Button>
        <Button variant="ghost" size="sm" onClick={resetFilters}>
          重置
        </Button>
      </div>
      <Table>
        <TableHeader>
          {table.getHeaderGroups().map((group) => (
            <TableRow key={group.id}>
              {group.headers.map((header) => (
                <TableHead
                  key={header.id}
                  aria-sort={
                    header.column.getIsSorted()
                      ? header.column.getIsSorted() === 'asc'
                        ? 'ascending'
                        : 'descending'
                      : 'none'
                  }
                >
                  {header.column.getCanSort() ? (
                    <button
                      className="sort-button"
                      onClick={header.column.getToggleSortingHandler()}
                    >
                      {flexRender(
                        header.column.columnDef.header,
                        header.getContext(),
                      )}
                      <ArrowDownUp
                        size={10}
                        className={
                          header.column.getIsSorted() ? 'sort-active' : ''
                        }
                      />
                    </button>
                  ) : (
                    flexRender(
                      header.column.columnDef.header,
                      header.getContext(),
                    )
                  )}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {table.getRowModel().rows.map((row) => (
            <TableRow
              key={row.original.id}
              className={selected?.id === row.original.id ? 'selected-row' : ''}
              onClick={() => setSelectedId(row.original.id)}
            >
              {row.getVisibleCells().map((cell) => (
                <TableCell key={cell.id}>
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </TableCell>
              ))}
            </TableRow>
          ))}
        </TableBody>
      </Table>
      {!filtered.length && (
        <div className="empty-state">
          <Radar size={32} />
          <h3>
            {loading
              ? '正在读取演示样本'
              : mode === 'live' && !apiOnline
                ? '等待本地监测服务'
                : source.length
                  ? '没有符合筛选条件的信号'
                  : '暂无有效异常信号'}
          </h3>
          <p>
            {dataError ||
              (source.length
                ? '调整筛选条件，或重置后查看全部信号。'
                : mode === 'live'
                  ? '网络受限、历史未就绪或未达到阈值时，不生成信号。'
                  : '演示样本加载后将在这里显示。')}
          </p>
          {source.length > 0 && (
            <Button variant="outline" onClick={resetFilters}>
              重置筛选
            </Button>
          )}
        </div>
      )}
      <div className="table-footer">
        <span>
          显示{' '}
          {filtered.length ? table.getState().pagination.pageIndex * 8 + 1 : 0}–
          {Math.min(
            (table.getState().pagination.pageIndex + 1) * 8,
            filtered.length,
          )}{' '}
          条，共 {filtered.length} 条{' '}
          <span className="muted">
            · {mode === 'demo' ? '固定演示样本' : '最新 1,000 条以内'} · UTC+8
          </span>
        </span>
        <div>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!table.getCanPreviousPage()}
            onClick={() => table.previousPage()}
            aria-label="上一页"
          >
            <ChevronLeft />
          </Button>
          <span>
            {table.getState().pagination.pageIndex + 1} /{' '}
            {Math.max(table.getPageCount(), 1)}
          </span>
          <Button
            variant="ghost"
            size="icon-sm"
            disabled={!table.getCanNextPage()}
            onClick={() => table.nextPage()}
            aria-label="下一页"
          >
            <ChevronRight />
          </Button>
        </div>
      </div>
    </section>
  );
  return (
    <div className="shell">
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            setView('overview');
          }}
          aria-label="PULSE 返回市场总览"
        >
          <div className="brand-mark">
            <Activity />
          </div>
          <div>
            PULSE<span>合约异动雷达</span>
          </div>
        </a>
        <div className="nav-caption">工作空间</div>
        <nav>
          {[
            ['overview', '市场总览', LayoutDashboard],
            ['history', '信号记录', History],
            ['universe', '行情下载与新币', Download],
            ['backtest', '历史回测', BarChart3],
            ['rules', '监测规则', SlidersHorizontal],
            ['system', '系统与推送', Radio],
          ].map(([id, label, Icon]) => {
            const I = Icon as typeof Activity;
            return (
              <Button
                key={id as string}
                variant="ghost"
                className={'nav-item ' + (view === id ? 'selected' : '')}
                onClick={() => setView(id as string)}
              >
                <I />
                {label as string}
                {id === 'overview' && <span className="nav-live-dot" />}
              </Button>
            );
          })}
        </nav>
        <div className="scope-label">当前监测范围</div>
        <div className="scope-item">
          <span className="small-dot" />
          币安 U 本位 · 仅 USDT
        </div>
        <div className="scope-tags">
          <span>加密 + 传统金融</span>
          <span>永续 + 交割</span>
          <span>5m + 15m</span>
        </div>
        <button className="scope-detail" onClick={() => setView('universe')}>
          {status.universe_count || 0} 个已发现 ·{' '}
          {Number(status.live_ready_symbols || 0)} 个实时就绪
          <ChevronRight size={13} />
        </button>
        <div className="sidebar-indicators">
          <small>正式强筛策略</small>
          <div>
            {settings.indicators.enabled_indicators.length ? (
              settings.indicators.enabled_indicators.map((id) => (
                <span key={id}>
                  {id === 'supertrend'
                    ? '超级趋势'
                    : id === 'smc'
                      ? '智慧资金结构'
                      : '指数移动平均双线'}
                </span>
              ))
            ) : (
              <span>空策略</span>
            )}
          </div>
          <button onClick={() => setView('rules')}>
            管理指标与强信号门槛 <SlidersHorizontal size={12} />
          </button>
        </div>
        <div className="sidebar-bottom">
          <div>
            <Send size={16} />
            <b>Telegram 推送设置</b>
            <span className="lock-dot" />
          </div>
          <p>在“系统与推送”中安全绑定</p>
          <div className="sidebar-line" />
          <span>只读行情 · 不自动开仓</span>
        </div>
        <button className="help-link" onClick={() => setHelp(true)}>
          <CircleHelp size={15} />
          规则说明与数据来源
          <ArrowUpRight size={12} />
        </button>
      </aside>
      <main className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            工作空间 <span>/</span> {heading[view]}
          </div>
          <div className="topbar-right">
            <span className="clock desktop-only">{clock}</span>
            <span className={'status-light ' + (connected ? 'online' : '')}>
              <i />
              {statusText}
            </span>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => setHelp(true)}
              aria-label="帮助"
            >
              <CircleHelp />
            </Button>
            <div className="avatar">N</div>
          </div>
        </header>
        <nav className="mobile-nav">
          {[
            ['overview', '总览'],
            ['history', '记录'],
            ['universe', '下载与新币'],
            ['backtest', '回测'],
            ['rules', '规则'],
            ['system', '状态/推送'],
          ].map(([v, t]) => (
            <button
              key={v}
              onClick={() => setView(v)}
              className={view === v ? 'active' : ''}
            >
              {t}
            </button>
          ))}
        </nav>
        <div className="content">
          <div className="page-heading">
            <div>
              <div className="eyebrow">BINANCE USDⓈ-M · USDT CONTRACTS</div>
              <h1>
                {view === 'backtest'
                  ? '先检验，再相信'
                  : view === 'universe'
                    ? '下载进度，实时可见'
                    : view === 'overview'
                      ? '捕捉波动，洞察异动'
                      : view === 'history'
                        ? '每一次异动，都有迹可循'
                        : view === 'rules'
                          ? '你的监测规则，清晰可见'
                          : '数据状态，一目了然'}
                <span className="title-dot">.</span>
              </h1>
              <p>
                {view === 'backtest'
                  ? '真实历史 · 严格成本 · 时间外验证，不用漂亮数字代替证据。'
                  : view === 'universe'
                    ? '仅 USDT 计价 · 加密资产与传统金融 · 永续与交割 · 新合约单独提醒。'
                    : view === 'overview'
                      ? '全市场 · 5m / 15m · 连续两根收线，发现值得关注的变化。'
                      : view === 'history'
                        ? '筛选、关注与导出，保留每个异常信号的完整依据。'
                        : view === 'rules'
                          ? '四种主要形态全部覆盖，只有完整收盘的 K 线参与判定。'
                          : '如实显示连接、历史预热与存储状态，不将演示数据混入真实行情。'}
              </p>
            </div>
            <div className="heading-actions">
              {view === 'rules' && (
                <Button onClick={saveSettings} disabled={!apiOnline || saving}>
                  {saving ? <RefreshCw className="spin" /> : <Check />}
                  保存全部规则
                </Button>
              )}
              <div className="mode-switch" aria-label="数据模式">
                <button
                  aria-pressed={mode === 'demo'}
                  className={mode === 'demo' ? 'active' : ''}
                  onClick={() => switchMode('demo')}
                >
                  演示预览
                </button>
                <button
                  aria-pressed={mode === 'live'}
                  className={mode === 'live' ? 'active' : ''}
                  onClick={() => switchMode('live')}
                >
                  实时监测
                </button>
              </div>
              <Button
                variant="outline"
                size="icon"
                onClick={() => {
                  setView('rules');
                  setDraft(settings);
                }}
                aria-label="监测设置"
              >
                <SlidersHorizontal />
              </Button>
            </div>
          </div>
          <div
            className={
              'data-banner ' + (mode === 'demo' ? 'demo-banner' : 'live-banner')
            }
          >
            <span>
              {mode === 'demo' ? (
                <BarChart3 size={15} />
              ) : connected ? (
                <Radio size={15} />
              ) : (
                <WifiOff size={15} />
              )}
            </span>
            <p>
              <b>
                {mode === 'demo'
                  ? '演示模式'
                  : connected
                    ? '实时数据'
                    : statusText}
              </b>
              {mode === 'demo'
                ? '合成 K 线经真实规则计算，仅用于功能验收，不代表市场行情。'
                : !api
                  ? '此地址用于在线验收。实时监测请打开本机看板，并运行采集服务。'
                  : !apiOnline
                    ? '请启动本地监测服务。连接恢复前，已显示的真实信号不会更新。'
                    : status.message ||
                      '等待交易所行情与足够的历史数据；不会使用演示数据补位。'}
            </p>
            {mode === 'demo' ? (
              <span className="banner-tag">{demo.length} 个规则样本</span>
            ) : (
              <Button
                variant="outline"
                size="sm"
                disabled={!apiOnline || retryBusy}
                onClick={async () => {
                  if (!api) return;
                  setRetryBusy(true);
                  try {
                    const response = await fetch(
                      api + '/api/connection/retry',
                      {
                        method: 'POST',
                        headers: { 'X-Pulse-Client': 'dashboard' },
                        signal: AbortSignal.timeout(15000),
                      },
                    );
                    if (!response.ok) throw Error();
                    notify('已继续下载与恢复连接；已有数据和下载进度保留。');
                    refresh();
                  } catch {
                    notify('连接重试未完成，请检查本地服务状态。');
                  } finally {
                    setRetryBusy(false);
                  }
                }}
              >
                <RefreshCw size={13} className={retryBusy ? 'spin' : ''} />
                {retryBusy ? '正在恢复…' : '继续下载 / 恢复连接'}
              </Button>
            )}
          </div>
          {(view === 'overview' || view === 'history') && (
            <>
              <div className="summary-strip">
                <div>
                  <span>
                    <Radar size={13} />
                    {mode === 'demo' ? '演示交易对' : '已发现合约'}
                  </span>
                  <b>
                    {mode === 'demo'
                      ? new Set(demo.map((s) => s.symbol)).size
                      : (status.universe_count ?? 0)}
                    <small>
                      {' '}
                      {mode === 'demo' ? '个合成样本' : '个 U 本位合约'}
                    </small>
                  </b>
                </div>
                <div>
                  <span>
                    <Activity size={13} />
                    {mode === 'demo' ? '演示异常信号' : '已加载异常信号'}
                  </span>
                  <b>
                    {source.length}
                    <small> 条 · 四种形态</small>
                  </b>
                </div>
                <div>
                  <span>
                    <ShieldCheck size={13} />
                    {mode === 'demo' ? '强活跃样本' : '通过全部筛选'}
                  </span>
                  <b className="mint">
                    {
                      source.filter((s) =>
                        mode === 'demo'
                          ? s.activity_score >= 80
                          : s.qualified === true &&
                            s.quality_schema_version === 2,
                      ).length
                    }
                    <small>
                      {' '}
                      {mode === 'demo' ? '活跃度 ≥80' : '条 · 待人工复核'}
                    </small>
                  </b>
                </div>
                <div>
                  <span>
                    <SlidersHorizontal size={13} />
                    异常触发门槛
                  </span>
                  <b>
                    {mode === 'live' &&
                    settings.threshold_profile === 'timeframe'
                      ? '按周期'
                      : number(
                          mode === 'demo' ? 1.5 : settings.volatility_ratio_min,
                        )}
                    <small>
                      {' '}
                      {mode === 'live' &&
                      settings.threshold_profile === 'timeframe'
                        ? '待回测验证'
                        : '× 历史基准'}
                    </small>
                  </b>
                </div>
              </div>
              {view === 'overview' && (
                <>
                  <div className="section-title">
                    <h2>四种形态，一览全局</h2>
                    <span>
                      点击形态卡片，筛选对应异常 <ArrowUpRight size={12} />
                    </span>
                  </div>
                  <div className="pattern-grid">
                    {PATTERNS.map((p, i) => (
                      <button
                        key={p}
                        aria-pressed={pattern === p}
                        className={
                          'pattern-card tone-' +
                          i +
                          (pattern === p ? ' pattern-selected' : '')
                        }
                        onClick={() => setPattern(pattern === p ? 'all' : p)}
                      >
                        <div className="pattern-top">
                          <PatternIcon pattern={p} />
                          <span className="pattern-count">
                            {patternCounts[i]}{' '}
                            <small>条{mode === 'demo' ? '样本' : '信号'}</small>
                          </span>
                        </div>
                        <h3>
                          {p}
                          <span>{DESCRIPTIONS[i]}</span>
                        </h3>
                        <p>
                          {p[0]}线 <span>→</span> {p[1]}线
                        </p>
                        <div className="pattern-bottom">
                          <span>
                            {pattern === p ? '正在筛选此形态' : '查看形态信号'}
                          </span>
                          {pattern === p ? (
                            <Check size={16} />
                          ) : (
                            <ArrowUpRight size={16} />
                          )}
                        </div>
                      </button>
                    ))}
                  </div>
                  {selected && (
                    <div className="detail-grid">
                      <section className="panel chart-panel">
                        <div className="panel-heading chart-heading">
                          <div className="selected-symbol">
                            <Coin symbol={selected.symbol} />
                            <div>
                              <h2>
                                {selected.symbol.includes('_')
                                  ? selected.symbol
                                  : asset(selected)}
                                <span> / {quote(selected)}</span>
                                <span className="tf-pill">
                                  {selected.timeframe}
                                </span>
                              </h2>
                              <small>
                                {mode === 'demo' ? '合成样本' : '信号收线快照'}{' '}
                                · {categoryLabel(selected)} ·{' '}
                                {contractLabel(selected)} ·{' '}
                                {stamp(selected.close_time, true)} UTC+8
                                {selected.daily_signal_count != null && (
                                  <span title="北京时间当日该合约全部原始异常次数，不随当前筛选变化">
                                    {' '}· 当日共 {selected.daily_signal_count} 次
                                  </span>
                                )}
                              </small>
                            </div>
                          </div>
                          <div className="chart-price">
                            <b>{priceText(selected.price)}</b>
                            <span
                              className={
                                selected.change_pct >= 0 ? 'up' : 'down'
                              }
                            >
                              {signed(selected.change_pct)}
                            </span>
                          </div>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            onClick={() => setExpanded(true)}
                            aria-label="放大 K 线图"
                          >
                            <Maximize2 />
                          </Button>
                        </div>
                        <div className="chart-meta">
                          <span>
                            <i className="small-dot" />
                            已收盘 K 线
                          </span>
                          <span>
                            两根真实波动 <b>{number(selected.v2_pct)}%</b>
                          </span>
                          <span>
                            历史基准 <b>{number(selected.baseline_v2_pct)}%</b>
                          </span>
                          <span className="signal-chart-tag">
                            {selected.level} {number(selected.volatility_ratio)}
                            ×
                          </span>
                        </div>
                        {selected.candles?.length ? (
                          <>
                            <CandleChart
                              signal={selected}
                              layers={chartLayers}
                              onToggleLayer={toggleChartLayer}
                            />
                          </>
                        ) : (
                          <div className="empty-state chart-loading">
                            <Activity size={24} />
                            <p>{detailFailure || '正在按需读取 K 线详情…'}</p>
                            {detailFailure && (
                              <Button
                                variant="outline"
                                size="sm"
                                onClick={() => setDetailRetry((v) => v + 1)}
                              >
                                重试详情
                              </Button>
                            )}
                          </div>
                        )}
                        <div className="chart-caption">
                          <span>
                            <span
                              className={
                                'pattern-tag tone-' +
                                patternIndex(selected.pattern)
                              }
                            >
                              {selected.pattern}
                            </span>{' '}
                            {selected.pattern === '阴阳' ||
                            selected.pattern === '阳阴'
                              ? '方向切换 · 关注多空分歧'
                              : '连续同向 · 关注波动扩张'}
                          </span>
                          <a
                            href="https://www.tradingview.com/"
                            target="_blank"
                            rel="noreferrer"
                          >
                            TradingView 轻量图表组件 <ExternalLink size={10} />
                          </a>
                        </div>
                      </section>
                      <section className="panel score-panel">
                        <div className="panel-heading">
                          <h2>活跃度解析</h2>
                          <button
                            aria-label="查看评分规则"
                            className="icon-button"
                            onClick={() => setHelp(true)}
                          >
                            <CircleHelp size={14} />
                          </button>
                        </div>
                        <div className="score-overview">
                          <div
                            className="score-ring"
                            style={
                              {
                                '--score':
                                  Math.min(
                                    360,
                                    (selected.activity_score /
                                      availableActivityMax(selected)) *
                                      360,
                                  ) + 'deg',
                              } as React.CSSProperties
                            }
                          >
                            <div>
                              <b>{selected.activity_score}</b>
                              <small>/ {availableActivityMax(selected)}</small>
                            </div>
                          </div>
                          <div>
                            <span className="chip mint-chip">
                              {selected.activity_level ||
                                (selected.activity_score >= 80
                                  ? '强活跃'
                                  : '活跃级')}
                            </span>
                            <p>
                              {missingActivityInputs(selected).length
                                ? `可用信息满分 ${availableActivityMax(selected)}；缺少${missingActivityInputs(selected).join('、')}，缺失项计 0 分`
                                : '五项信息完整，满分 100'}
                            </p>
                            <small>
                              波动倍数 {number(selected.volatility_ratio)}×
                            </small>
                          </div>
                        </div>
                        <div className="score-bars">
                          {SCORE_LABELS.map(([key, label, max]) => (
                            <div key={key}>
                              <div>
                                <span>
                                  {label}
                                  {key === 'oi' &&
                                    missingActivityInputs(selected).includes(
                                      '持仓变化',
                                    ) && (
                                      <em>待积累</em>
                                    )}
                                  {key === 'liquidity' &&
                                    (missingActivityInputs(selected).includes(
                                      '流动性排名',
                                    ) ||
                                      missingActivityInputs(selected).includes(
                                        '买卖价差',
                                      )) && <em>部分缺失</em>}
                                </span>
                                <b>
                                  {selected.score_breakdown[key] ?? 0}
                                  <small> / {max}</small>
                                </b>
                              </div>
                              <div className="score-bar">
                                <i
                                  style={{
                                    width:
                                      ((selected.score_breakdown[key] ?? 0) /
                                        max) *
                                        100 +
                                      '%',
                                    background:
                                      key === 'oi'
                                        ? '#8c9eec'
                                        : key === 'flow'
                                          ? '#66b8d5'
                                          : '#55cbb3',
                                  }}
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                        <div className="score-disclaimer">
                          <ShieldCheck size={12} />
                          分数衡量市场活跃度，不代表胜率
                        </div>
                      </section>
                    </div>
                  )}
                </>
              )}
              {signalTable}
              {view === 'overview' && selected && (
                <ConfirmationPanel
                  signal={selected}
                  synthetic={mode === 'demo'}
                />
              )}
              {view === 'overview' && selected && (
                <section className="panel evidence-panel">
                  <div className="panel-heading">
                    <h2>
                      触发依据{' '}
                      <span className="muted">
                        {selected.symbol} · {selected.timeframe}
                      </span>
                    </h2>
                    <a
                      target="_blank"
                      rel="noreferrer"
                      className="external-link"
                      href={
                        'https://www.binance.com/en/futures/' + selected.symbol
                      }
                    >
                      币安合约 <ExternalLink size={12} />
                    </a>
                  </div>
                  <div className="evidence-grid">
                    <div>
                      <span>两根累计成交额</span>
                      <b>
                        {compact(selected.quote_volume)}{' '}
                        <small>{quote(selected)}</small>
                      </b>
                    </div>
                    <div>
                      <span>主动买入 / 卖出</span>
                      <b>
                        {selected.taker_buy_ratio == null
                          ? '—'
                          : number(selected.taker_buy_ratio * 100, 1) +
                            '% / ' +
                            number((1 - selected.taker_buy_ratio) * 100, 1) +
                            '%'}
                      </b>
                    </div>
                    <div>
                      <span>持仓量变化</span>
                      <b
                        className={
                          (selected.oi_change_pct ?? 0) >= 0 ? 'up' : 'down'
                        }
                      >
                        {signed(selected.oi_change_pct)}
                      </b>
                    </div>
                    <div>
                      <span>买卖价差</span>
                      <b>
                        {number(selected.spread_bps, 1)} <small>bps</small>
                      </b>
                    </div>
                    <div>
                      <span>
                        {selected.liquidity_rank_scope === 'same_quote_asset'
                          ? '同报价合约成交额分位'
                          : '24h 成交额分位（历史口径）'}
                      </span>
                      <b>
                        {selected.liquidity_percentile == null
                          ? '—'
                          : '前 ' +
                            number(
                              (1 - selected.liquidity_percentile) * 100,
                              1,
                            ) +
                            '%'}
                      </b>
                    </div>
                    <div>
                      <span>历史有效窗口</span>
                      <b>
                        {selected.baseline_samples.toLocaleString()}{' '}
                        <small>组</small>
                      </b>
                    </div>
                  </div>
                  <div className="signal-warnings">
                    <span>
                      <Activity size={12} />
                      {selected.oi_structure}
                    </span>
                    {selected.warnings.map((w, i) => (
                      <span key={i}>
                        <AlertTriangle size={12} />
                        {displayWarning(selected, w)}
                      </span>
                    ))}
                    <span>代币地址：暂无可靠映射</span>
                  </div>
                </section>
              )}
            </>
          )}
          {view === 'rules' && (
            <div className="rules-layout">
              <section className="panel rules-form">
                <div className="panel-heading">
                  <h2>
                    <SlidersHorizontal size={17} />
                    异常检测设置
                  </h2>
                  <span className="chip">只影响后续信号</span>
                </div>
                <div className="form-body">
                  <ThresholdPanel
                    value={draft}
                    onChange={(v) => setDraft({ ...draft, ...v })}
                  />
                  <label className="field">
                    <span>
                      最低波动异常倍数 <small>≥1.50</small>
                    </span>
                    <Input
                      type="number"
                      min={1.5}
                      max={20}
                      step={0.1}
                      value={draft.volatility_ratio_min}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          volatility_ratio_min: Number(e.target.value),
                        })
                      }
                    />
                    <p>当前两根 NTR 之和 ÷ 历史两根 NTR 均值。</p>
                  </label>
                  <label className="field">
                    <span>监测形态</span>
                    <FilterSelect
                      label="监测模式"
                      value={draft.pattern_mode}
                      onChange={(v) => setDraft({ ...draft, pattern_mode: v })}
                      items={[
                        ['all', '全部四种主要形态'],
                        ['mixed_only', '仅方向切换：阳阴、阴阳'],
                      ]}
                    />
                  </label>
                  <label className="field">
                    <span>历史基准算法</span>
                    <FilterSelect
                      label="基准算法"
                      value={draft.baseline_method}
                      onChange={(v) =>
                        setDraft({ ...draft, baseline_method: v })
                      }
                      items={[
                        ['trimmed_mean', '去极值均值（双侧裁剪）'],
                        ['simple_mean', '普通算术平均'],
                      ]}
                    />
                    <p>默认从历史样本两端各移除 2.5%，减少极端插针的干扰。</p>
                  </label>
                  <label className="field">
                    <span>
                      每侧裁剪比例 <small>%</small>
                    </span>
                    <Input
                      type="number"
                      min={0}
                      max={10}
                      step={0.5}
                      disabled={draft.baseline_method === 'simple_mean'}
                      value={draft.trim_percent}
                      onChange={(e) =>
                        setDraft({
                          ...draft,
                          trim_percent: Number(e.target.value),
                        })
                      }
                    />
                  </label>
                  <div className="switch-field">
                    <div>
                      <b>包含十字星组合</b>
                      <p>
                        实体 ÷ 振幅 ≤5% 时标为十字星；默认只启用四种主要形态。
                      </p>
                    </div>
                    <Switch
                      checked={draft.allow_doji}
                      onCheckedChange={(v) =>
                        setDraft({ ...draft, allow_doji: v })
                      }
                      aria-label="包含十字星"
                    />
                  </div>
                  <div className="rule-fixed">
                    <ShieldCheck size={17} />
                    <div>
                      <b>始终启用的保护规则</b>
                      <p>
                        只用已收盘 K 线 · 时间连续 · 历史充分 · 排除当前两根 ·
                        缺数据不编造评分
                      </p>
                    </div>
                  </div>
                  {!apiOnline && (
                    <p className="form-notice">
                      此预览未连接本地服务，暂不能保存监测规则。实时设置需在本机看板中修改。
                    </p>
                  )}
                  <div className="form-actions">
                    <Button
                      variant="outline"
                      onClick={() => setDraft(DEFAULT_SETTINGS)}
                    >
                      恢复默认草稿
                    </Button>
                    <Button
                      onClick={saveSettings}
                      disabled={
                        !apiOnline ||
                        saving ||
                        !Number.isFinite(draft.volatility_ratio_min) ||
                        draft.volatility_ratio_min < 1.5 ||
                        draft.volatility_ratio_min > 20 ||
                        !Number.isFinite(draft.trim_percent) ||
                        draft.trim_percent < 0 ||
                        draft.trim_percent > 10
                      }
                    >
                      {saving ? <RefreshCw className="spin" /> : <Check />}
                      保存监测规则
                    </Button>
                  </div>
                  <p className="field-help">
                    演示页使用固定验收样本，修改真实规则不会重算演示或历史记录。
                  </p>
                </div>
              </section>
              <div className="rules-right">
                <IndicatorSettingsPanel
                  value={draft}
                  onChange={(v) => setDraft({ ...draft, ...v })}
                  api={api}
                  online={apiOnline}
                />
                <section className="panel">
                  <div className="panel-heading">
                    <h2>5m / 15m 历史基准</h2>
                    <Clock size={16} />
                  </div>
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>周期</TableHead>
                        <TableHead>目标历史</TableHead>
                        <TableHead>最低历史根数</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {BASELINE.map(([t, d, n]) => (
                        <TableRow key={t}>
                          <TableCell>
                            <span className="tf-pill">{t}</span>
                          </TableCell>
                          <TableCell>{d}</TableCell>
                          <TableCell>{n}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                  <p className="rule-note">
                    新币历史不足时仅采集，不生成标准异常评分。周期按 UTC
                    对齐，界面时间显示 UTC+8。
                  </p>
                </section>
                <section className="panel formula-card">
                  <h2>为什么用真实波幅？</h2>
                  <code>TR = max(H − L, |H − C前|, |L − C前|)</code>
                  <code>NTR = TR / C前</code>
                  <code>V₂ = NTR₁ + NTR₂</code>
                  <p>
                    即使一涨一跌后价格回到原位，也能捕捉途中发生的大幅波动。
                  </p>
                </section>
              </div>
            </div>
          )}
          {view === 'system' && (
            <>
              <div className="system-grid">
                {[
                  [
                    Radio,
                    '行情采集',
                    statusText,
                    connected ? '已连接官方公共行情' : '等待有效行情连接',
                  ],
                  [
                    Database,
                    '本地存储',
                    apiOnline
                      ? number(status.database?.alerts || 0, 0) + ' 条信号'
                      : '未连接',
                    apiOnline
                      ? 'SQLite 持久化 · 重启去重'
                      : '演示数据与真实数据完全分离',
                  ],
                  [
                    Clock,
                    '历史预热',
                    apiOnline
                      ? String(status.ready_pairs || 0) +
                        ' / ' +
                        String(
                          status.timeframe_total ??
                            (status.universe_count || 0) * TFS.length,
                        )
                      : '—',
                    '合约 × 2 个正式周期的基准就绪数量',
                  ],
                  [
                    Send,
                    'Telegram',
                    '在下方配置',
                    'Token 只写不回显 · 一次性链接安全绑定',
                  ],
                ].map(([Icon, title, value, desc]) => {
                  const I = Icon as typeof Activity;
                  return (
                    <section
                      className="panel system-card"
                      key={title as string}
                    >
                      <I size={20} />
                      <h3>{title as string}</h3>
                      <b>{value as string}</b>
                      <p>{desc as string}</p>
                    </section>
                  );
                })}
              </div>
              <UpdatePanel api={api} online={apiOnline} />
              <TelegramSettingsPanel api={api} online={apiOnline} />
              <section className="panel status-details">
                <div className="panel-heading">
                  <h2>运行健康状况</h2>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => {
                      refresh();
                      notify(
                        api ? '已请求更新状态' : '在线演示版不连接本机采集服务',
                      );
                    }}
                  >
                    <RefreshCw />
                    刷新状态
                  </Button>
                </div>
                <dl>
                  <div>
                    <dt>服务状态</dt>
                    <dd>{statusText}</dd>
                  </div>
                  <div>
                    <dt>连接说明</dt>
                    <dd>
                      {status.message ||
                        (!api
                          ? '私有在线预览仅提供演示；本机版本连接本地采集程序。'
                          : '尚未获取服务状态。')}
                    </dd>
                  </div>
                  <div>
                    <dt>监测范围</dt>
                    <dd>
                      {String(
                        status.scope ||
                          '币安 U_MARGINED · 仅USDT报价 · 全部资产类别及合约类型',
                      )}
                    </dd>
                  </div>
                  <div>
                    <dt>已就绪交易对</dt>
                    <dd>
                      {status.ready_symbols ?? 0} / {status.universe_count ?? 0}
                    </dd>
                  </div>
                  <div>
                    <dt>最近收到行情</dt>
                    <dd>{stamp(status.last_event_time, true)}</dd>
                  </div>
                  <div>
                    <dt>最近完整分钟收线</dt>
                    <dd>{stamp(status.last_closed_time, true)}</dd>
                  </div>
                  <div>
                    <dt>网页最近同步</dt>
                    <dd>
                      {stamp(lastSync, true)}
                      {paused ? '（仅页面刷新已暂停）' : ''}
                    </dd>
                  </div>
                  <div>
                    <dt>缓存完整 K 线</dt>
                    <dd>{number(status.database?.candles || 0, 0)} 根</dd>
                  </div>
                  <div>
                    <dt>已采集 OI 快照</dt>
                    <dd>{number(status.database?.oi_snapshots || 0, 0)} 条</dd>
                  </div>
                  <div>
                    <dt>自动交易</dt>
                    <dd>未接入；Telegram 推送在上方单独设置</dd>
                  </div>
                </dl>
              </section>
              <div className="system-note">
                <AlertTriangle size={18} />
                <p>
                  首次安装无需手动下载：程序会自动发现全部 USDT 合约并加载 5m / 15m
                  历史基准，完成后持续保存实时收盘数据。OI
                  从启动后持续积累，样本不足时明确标注并计 0
                  分。网络受限时保留可见的错误状态，不使用演示数据冒充实时结果。
                </p>
              </div>
            </>
          )}
          {view === 'universe' && (
            <>
              <DownloadPanel api={api} online={apiOnline} />
              <UniversePanel api={api} online={apiOnline} />
            </>
          )}
          {view === 'backtest' && (
            <BacktestPanel api={api} online={apiOnline} />
          )}
          <footer>
            <span>
              <span className="small-dot" />
              仅监测 · 不自动开仓 · 异常信号不构成交易建议
            </span>
            <span>
              PULSE v1.0.1 <i />
              {mode === 'demo' ? '演示数据已标识' : '只接收完整收盘 K 线'}
            </span>
          </footer>
        </div>
      </main>
      <Dialog open={help} onOpenChange={setHelp}>
        <DialogContent className="help-dialog">
          <DialogTitle>PULSE · 规则与数据说明</DialogTitle>
          <DialogDescription>
            让每个信号的来源、计算方式与限制都可追溯。
          </DialogDescription>
          <div className="help-body">
            <h3>四种情况</h3>
            <p>
              阳阳：连续向上；阴阴：连续向下；阳阴：冲高回落；阴阳：下探反弹。四类标签描述
              K 线组合，不是买卖指令。
            </p>
            <h3>触发与评分</h3>
            <p>
              仅使用连续两根已收盘 K 线。NTR
              为真实波幅除以前一根收盘价，两根之和达到历史均值的 1.5
              倍后进入评分。基准不包含当前两根。
            </p>
            <p>
              成交额 35 分、结构 20 分、主动买卖 20 分、OI 15 分、流动性 10
              分。缺失指标计 0 分，不按比例补足 100 分。活跃度不是胜率。
            </p>
            <h3>真实数据与演示</h3>
            <p>
              演示数据是固定合成样本，经过与后台相同的规则引擎。实时数据来自币安公共行情接口，不需要交易密钥。若受地区限制或历史不足，看板会如实提示。Telegram
              可在“系统与推送”页安全绑定，并自行选择推送规则。
            </p>
            <h3>开源参考</h3>
            <div className="source-links">
              <a
                href="https://github.com/tradingview/lightweight-charts"
                target="_blank"
                rel="noreferrer"
              >
                TradingView Lightweight Charts · Apache-2.0{' '}
                <ExternalLink size={12} />
              </a>
              <a
                href="https://github.com/tremorlabs/template-dashboard-oss"
                target="_blank"
                rel="noreferrer"
              >
                Tremor Dashboard OSS · 布局参考 <ExternalLink size={12} />
              </a>
              <a
                href="https://github.com/shadcn-ui/ui"
                target="_blank"
                rel="noreferrer"
              >
                shadcn/ui · 界面组件 · MIT <ExternalLink size={12} />
              </a>
              <a
                href="https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect"
                target="_blank"
                rel="noreferrer"
              >
                币安官方行情接口说明 <ExternalLink size={12} />
              </a>
            </div>
          </div>
        </DialogContent>
      </Dialog>
      <Dialog open={expanded} onOpenChange={setExpanded}>
        <DialogContent className="expanded-dialog">
          <DialogTitle>
            {selected?.symbol} · {selected?.timeframe} ·{' '}
            {mode === 'demo' ? '合成演示样本' : '信号快照'}
          </DialogTitle>
          <DialogDescription>
            拖动平移、滚轮缩放，十字光标查看价格与时间。
          </DialogDescription>
          {selected && (
            <CandleChart
              signal={selected}
              layers={chartLayers}
              onToggleLayer={toggleChartLayer}
              expanded
            />
          )}
        </DialogContent>
      </Dialog>
      {toast && (
        <div className="toast" role="status">
          <CheckCircle2 size={17} />
          {toast}
          <button aria-label="关闭提示" onClick={() => setToast('')}>
            <X size={14} />
          </button>
        </div>
      )}
    </div>
  );
}
