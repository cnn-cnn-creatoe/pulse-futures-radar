'use client';
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownToLine,
  Check,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  WifiOff,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  CATEGORIES,
  categoryLabel,
  contractLabel,
  isPerpetual,
  isTradfi,
  matchesCategory,
  quoteOf,
  type ContractMeta,
} from './contract-meta';
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from '@/components/ui/table';

const TFS = ['5m', '15m'];
const LABELS: Record<string, string> = {
  queued: '排队等待',
  warming: '正在下载',
  downloading: '正在下载',
  ready: '已就绪',
  complete: '已下载',
  completed: '已下载',
  cached: '已有缓存',
  insufficient_history: '历史不足',
  new_insufficient: '新币历史不足',
  gap: '补齐缺口',
  stale: '等待补齐',
  error: '下载失败',
  paused: '暂停',
  blocked: '访问受限',
  connecting: '正在连接',
  stopped: '已停止',
  reconnecting: '重连中',
};
const label = (s?: string) => (s ? LABELS[s] || s : '等待状态');
const n = (v: number | null | undefined, d = 0) =>
  v == null || !Number.isFinite(v)
    ? '—'
    : v.toLocaleString('zh-CN', { maximumFractionDigits: d });
const price = (v: number | null | undefined) =>
  v == null
    ? '—'
    : n(
        v,
        Math.min(12, Math.max(2, 6 - Math.floor(Math.log10(Math.abs(v) || 1)))),
      );
const stamp = (v?: number | null, date = false) =>
  v
    ? new Intl.DateTimeFormat('zh-CN', {
        timeZone: 'Asia/Shanghai',
        ...(date ? { month: '2-digit', day: '2-digit' } : {}),
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
        hour12: false,
      }).format(new Date(v))
    : '—';
type Kline = {
  open_time: number;
  close_time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
  quote_volume: number;
  trades?: number;
  closed: boolean;
};
type Quote = Partial<Kline> & {
  price?: number;
  observed_at?: number;
  event_time?: number;
  stale?: boolean;
  connected?: boolean;
  source?: string;
};
type Job = ContractMeta & {
  symbol: string;
  timeframe: string;
  state: string;
  bars: number;
  minimum: number;
  target_bars: number;
  cached_bars?: number;
  downloaded_bars?: number;
  pages?: number;
  progress_pct: number | null;
  stage?: string;
  window_missing_bars?: number | null;
  window_complete?: boolean;
  requested_start?: number;
  requested_end?: number;
  first_open_time?: number;
  last_close_time?: number;
  last_page_at?: number;
  error?: string | null;
  reason?: string;
  latest_candle?: Kline;
  live_quote?: Quote;
  stored_bars?: number;
  stored_first_open_time?: number;
  stored_last_close_time?: number;
  stored_last_price?: number;
};
type PageEvent = {
  seq: number;
  time: number;
  type: string;
  symbol?: string;
  timeframe?: string;
  count?: number;
  message?: string;
  error?: string;
  latest_candle?: Kline;
  start?: number;
  end?: number;
  first_open_time?: number;
  last_close_time?: number;
  duration_ms?: number;
};
type Snapshot = {
  session_id: string;
  cursor: number;
  server_time: number;
  state: string;
  items: Job[];
  events: PageEvent[];
  summary: Record<string, number | string | null>;
  inventory_updated_at?: number;
  message?: string;
  truncated?: boolean;
  events_truncated?: boolean;
};
type Detail = {
  symbol: string;
  timeframe: string;
  job: Job | null;
  inventory: {
    bars: number;
    first_open_time?: number;
    last_close_time?: number;
  };
  candles: Kline[];
  live_quote: Quote | null;
  server_time: number;
};
type Coin = ContractMeta & {
  symbol: string;
  jobs: Job[];
  stored: number;
  received: number;
  target: number;
  downloaded: number;
  ready: number;
  state: string;
  last: number;
  quote?: Quote;
};

export function DownloadPanel({
  api,
  online,
}: {
  api: string | null;
  online: boolean;
}) {
  const [data, setData] = useState<Snapshot | null>(null),
    [events, setEvents] = useState<PageEvent[]>([]),
    [error, setError] = useState(''),
    [query, setQuery] = useState(''),
    [filter, setFilter] = useState('all'),
    [category, setCategory] = useState('all'),
    [contractFilter, setContractFilter] = useState('all'),
    [page, setPage] = useState(0),
    [symbol, setSymbol] = useState('BTCUSDT'),
    [tf, setTf] = useState('5m'),
    [detail, setDetail] = useState<Detail | null>(null),
    [detailError, setDetailError] = useState(''),
    [continuing, setContinuing] = useState(false),
    [notice, setNotice] = useState(''),
    [updated, setUpdated] = useState(0),
    [follow, setFollow] = useState(true);
  const cursor = useRef<{ session: string; seq: number }>({
    session: '',
    seq: 0,
  });
  const manual = useRef(false);
  useEffect(() => {
    if (!api) return;
    let ended = false,
      timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const pull = async () => {
      try {
        const suffix = cursor.current.seq ? '?after=' + cursor.current.seq : '';
        const response = await fetch(api + '/api/downloads' + suffix, {
          signal: AbortSignal.any([
            controller.signal,
            AbortSignal.timeout(12000),
          ]),
        });
        if (!response.ok) throw Error('下载状态读取失败，请确认服务已经启动。');
        const next = (await response.json()) as Snapshot;
        if (ended) return;
        const reset = next.session_id !== cursor.current.session;
        // A server restart resets its sequence. Re-fetch its recent page events
        // without the old cursor so the activity feed cannot silently go empty.
        if (reset && cursor.current.session) {
          const r = await fetch(api + '/api/downloads', {
            signal: AbortSignal.any([
              controller.signal,
              AbortSignal.timeout(12000),
            ]),
          });
          if (r.ok) {
            const fresh = (await r.json()) as Snapshot;
            next.events = fresh.events;
            next.cursor = fresh.cursor;
          }
        }
        cursor.current = { session: next.session_id, seq: next.cursor };
        setData(next);
        setUpdated(Date.now());
        setError('');
        setEvents((old) => {
          const merged = new Map<number, PageEvent>(
            (reset ? [] : old).map((e) => [e.seq, e]),
          );
          for (const event of next.events || []) {
            if (!event.timeframe || TFS.includes(event.timeframe))
              merged.set(event.seq, event);
          }
          return [...merged.values()]
            .sort((a, b) => b.seq - a.seq)
            .slice(0, 150);
        });
        if (!manual.current) {
          const active = next.items.find(
            (j) =>
              TFS.includes(j.timeframe) &&
              ['downloading', 'warming'].includes(j.state),
          );
          if (active) {
            setSymbol(active.symbol);
            setTf(active.timeframe);
          }
        }
      } catch (e) {
        if (!ended)
          setError(
            (e as Error).name === 'TimeoutError'
              ? '读取进度超时，已显示的数据会保留；不会重启下载。'
              : (e as Error).message,
          );
      } finally {
        if (!ended) timer = setTimeout(pull, 2000);
      }
    };
    pull();
    return () => {
      ended = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [api]);
  useEffect(() => {
    if (!api || !symbol) return;
    let ended = false,
      timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    setDetail(null);
    setDetailError('');
    const pull = async () => {
      try {
        const r = await fetch(
          `${api}/api/downloads/${encodeURIComponent(symbol)}/${tf}?limit=20`,
          {
            signal: AbortSignal.any([
              controller.signal,
              AbortSignal.timeout(8000),
            ]),
          },
        );
        if (!r.ok) throw Error('此币种的数据详情暂时无法读取');
        const d = (await r.json()) as Detail;
        if (!ended) {
          setDetail(d);
          setDetailError('');
        }
      } catch (e) {
        if (!ended) setDetailError((e as Error).message);
      } finally {
        if (!ended) timer = setTimeout(pull, 2000);
      }
    };
    pull();
    return () => {
      ended = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, symbol, tf]);
  const scopedItems = useMemo(
    () => (data?.items || []).filter((job) => TFS.includes(job.timeframe)),
    [data],
  );
  const coins = useMemo(() => {
    const map = new Map<string, Coin>();
    for (const job of scopedItems) {
      let coin = map.get(job.symbol);
      if (!coin) {
        coin = {
          ...job,
          symbol: job.symbol,
          jobs: [],
          stored: 0,
          received: 0,
          target: 0,
          downloaded: 0,
          ready: 0,
          state: 'queued',
          last: 0,
        };
        map.set(job.symbol, coin);
      }
      coin.jobs.push(job);
      coin.stored += job.stored_bars || 0;
      coin.received += job.downloaded_bars || 0;
      coin.target += job.target_bars || 0;
      coin.downloaded += Math.min(job.bars || 0, job.target_bars || 0);
      coin.ready += job.state === 'ready' ? 1 : 0;
      coin.last = Math.max(coin.last, job.last_page_at || 0);
      if (job.live_quote) coin.quote = job.live_quote;
    }
    for (const coin of map.values()) {
      coin.state = coin.jobs.some(
        (j) => j.state === 'downloading' || j.state === 'warming',
      )
        ? 'downloading'
        : coin.jobs.some((j) => j.state === 'error')
          ? 'error'
          : coin.jobs.some((j) => j.state === 'queued')
            ? 'queued'
            : coin.jobs.every((j) => j.state === 'ready')
              ? 'ready'
              : coin.jobs.some((j) => j.state === 'gap' || j.state === 'stale')
                ? 'gap'
                : coin.jobs.some((j) => j.state === 'insufficient_history')
                  ? 'insufficient_history'
                  : 'cached';
    }
    return [...map.values()].sort((a, b) => a.symbol.localeCompare(b.symbol));
  }, [scopedItems]);
  const filtered = useMemo(
    () =>
      coins.filter(
        (c) =>
          c.symbol.toLowerCase().includes(query.toLowerCase()) &&
          (filter === 'all' || c.state === filter) &&
          matchesCategory(c, category) &&
          (contractFilter === 'all' ||
            (contractFilter === 'perpetual'
              ? isPerpetual(c)
              : !!c.contract_type && !isPerpetual(c))),
      ),
    [coins, query, filter, category, contractFilter],
  );
  const maxPage = Math.max(0, Math.ceil(filtered.length / 20) - 1),
    currentPage = Math.min(page, maxPage),
    visible = filtered.slice(currentPage * 20, (currentPage + 1) * 20);
  const active = scopedItems.filter((j) =>
    ['warming', 'downloading'].includes(j.state),
  );
  const chosen =
    detail?.job ||
    scopedItems.find((j) => j.symbol === symbol && j.timeframe === tf);
  const live = detail?.live_quote,
    closed = detail?.candles?.[0];
  const freshQuote = live && !live.stale && live.connected !== false;
  const lastBatch = events.find(
    (e) =>
      e.type === 'page_saved' &&
      e.symbol === symbol &&
      e.timeframe === tf &&
      e.latest_candle,
  );
  const feed = follow ? events : events.filter((e) => e.symbol === symbol);
  const recentWritten = events
    .filter(
      (e) =>
        e.type === 'page_saved' &&
        e.count &&
        e.time >= (data?.server_time || Date.now()) - 60000,
    )
    .reduce((sum, e) => sum + (e.count || 0), 0);
  async function resume() {
    if (!api) return;
    setContinuing(true);
    setNotice('');
    try {
      const r = await fetch(api + '/api/downloads/resume', {
        method: 'POST',
        headers: { 'X-Pulse-Client': 'dashboard' },
        signal: AbortSignal.timeout(20000),
      });
      const d = (await r.json()) as { message?: string; detail?: string };
      if (!r.ok) throw Error(d.detail || '继续下载失败');
      setNotice(d.message || '已继续下载缺失部分');
    } catch (e) {
      setNotice((e as Error).message);
    } finally {
      setContinuing(false);
    }
  }
  function choose(s: string, t?: string) {
    manual.current = true;
    setSymbol(s);
    if (t) setTf(t);
  }
  return (
    <div className="download-workspace">
      <section className="panel download-command">
        <div>
          <span className="eyebrow">LIVE DOWNLOAD / BINANCE USDⓈ-M</span>
          <h2>
            <ArrowDownToLine size={22} />
            行情下载实况
          </h2>
          <p>
            仅 USDT 计价，涵盖全部 U
            本位加密资产、传统金融、永续与交割合约。正式历史和信号仅使用 5m、15m，逐合约只补缺失区间；旧周期数据保留但不再下载或评分。
          </p>
        </div>
        <div className="download-command-actions">
          <span
            className={'download-live-state ' + (active.length ? 'active' : '')}
          >
            <i />
            {!api
              ? '仅本机可用'
              : error
                ? '读取中断'
                : active.length
                  ? `${active.length} 个任务正在下载`
                  : label(data?.state)}
          </span>
          <Button disabled={!api || !online || continuing} onClick={resume}>
            {continuing ? <RefreshCw className="spin" /> : <Download />}
            {continuing ? '正在继续…' : '继续下载 / 重试失败'}
          </Button>
        </div>
      </section>
      {!api && (
        <div className="form-notice">
          这是在线演示地址。请打开本机看板查看真实下载；不会显示模拟下载进度。
        </div>
      )}
      {(error || notice) && (
        <div className="form-notice" role="status">
          {error || notice}
        </div>
      )}
      <div className="download-stats">
        <div>
          <small title="本地数据库保留的唯一已收盘 K 线总数；包含当前 USDT 范围以及之前下载过但已排除的旧范围缓存。">
            <Database size={13} />
            本机全部已存 K 线
          </small>
          <b>
            {n(
              (data?.summary.retained_candles ??
                data?.summary.stored_candles) as number,
            )}
          </b>
          <span>含旧范围缓存 · 唯一记录 · 约 8 秒更新</span>
        </div>
        <div>
          <small title="历史请求返回不等于窗口完整。这里要求目标时间窗的数量、第一根与最后一根边界都核验一致。">
            <Activity size={13} />
            两周期历史窗口
          </small>
          <b>
            {n(
              (data?.summary.window_complete_pairs ??
                data?.summary.completed_pairs) as number,
            )}
            <em> / {n(data?.summary.total_pairs as number)}</em>
          </b>
          <span>
            请求已结束 {n(data?.summary.request_completed_pairs as number)} 项 ·
            完整度 {n(data?.summary.progress_pct as number, 1)}%
          </span>
        </div>
        <div>
          <small title="除了历史足够，还要求最新周期连续、不过期且不在真实缺口核验中；只有这些周期会生成正式信号。">
            <ShieldCheck size={13} />
            可用于判断的周期
          </small>
          <b>
            {n(scopedItems.filter((j) => j.state === 'ready').length)}
            <em> / {n(data?.summary.total_pairs as number)}</em>
          </b>
          <span>历史不足与待下载分开显示</span>
        </div>
        <div>
          <small title="最近60秒从官方接口收到并写入的批次条数；可能覆盖已有记录，所以不等于数据库净增加。">
            <Radio size={13} />
            最近收到并写入
          </small>
          <b>
            {n(recentWritten)}
            <em> 根</em>
          </b>
          <span>最近 60 秒可见下载批次 · 非净新增</span>
        </div>
      </div>
      <div className="contract-scope-note">
        <span>
          当前 USDT 范围：{n(coins.length)} 个合约 × 2 周期 ={' '}
          {n(data?.summary.total_pairs as number)} 项，其中{' '}
          {n(coins.filter(isTradfi).length)} 个传统金融、
          {n(coins.filter((c) => !!c.contract_type && !isPerpetual(c)).length)}{' '}
          个交割合约
        </span>
        <span>
          {data?.summary.scope_verified
            ? '当前范围已核验'
            : '范围核验中，缓存不代表已开始监测'}{' '}
          · USDT 范围已存 {n(data?.summary.stored_candles as number)}{' '}
          根；历史不足、缺口或未收盘时不生成正式信号。
        </span>
      </div>
      <section className="panel active-downloads">
        <div className="panel-heading">
          <h2>
            <Download size={16} />
            当前下载任务
          </h2>
          <span className="muted">页面每 2 秒更新 · {stamp(updated)}</span>
        </div>
        <div className="active-job-grid">
          {active.length ? (
            active.map((j) => (
              <button
                key={j.symbol + j.timeframe}
                className={
                  symbol === j.symbol && tf === j.timeframe ? 'selected' : ''
                }
                onClick={() => choose(j.symbol, j.timeframe)}
              >
                <div>
                  <b>{j.symbol}</b>
                  <span>{j.timeframe}</span>
                  <RefreshCw size={12} className="spin" />
                </div>
                <p>
                  {n(j.bars)} / {n(j.target_bars)} 根{' '}
                  <small>{n(j.progress_pct, 1)}%</small>
                </p>
                <div className="download-meter">
                  <i
                    style={{
                      width:
                        Math.max(0, Math.min(100, j.progress_pct || 0)) + '%',
                    }}
                  />
                </div>
                <small>
                  已接收 {n(j.downloaded_bars || 0)} 根 · {n(j.pages || 0)} 批
                </small>
              </button>
            ))
          ) : (
            <p className="download-empty-inline">
              {data?.state === 'blocked'
                ? '官方接口拒绝访问，保留已有数据等待恢复。'
                : coins.length
                  ? '当前没有正在请求的历史批次；请查看各币状态及下方日志。'
                  : '正在读取合约范围和已有缓存，成功后自动开始下载。'}
            </p>
          )}
        </div>
      </section>
      <div className="download-main-grid">
        <section className="panel coin-downloads">
          <div className="panel-heading">
            <h2>
              各合约下载进度{' '}
              <span className="count-pill">{filtered.length}</span>
            </h2>
            <span className="muted">点击周期查看内容</span>
          </div>
          <div className="download-filters">
            <div>
              <Search size={15} />
              <Input
                value={query}
                placeholder="搜索 BTC / TSLA / XAU / 260925"
                onChange={(e) => {
                  setQuery(e.target.value);
                  setPage(0);
                }}
                aria-label="搜索下载合约"
              />
            </div>
            <select
              className="native-select"
              title="按当前监测状态筛选。历史请求完成、窗口完整与实时就绪是三种不同状态。"
              value={filter}
              onChange={(e) => {
                setFilter(e.target.value);
                setPage(0);
              }}
              aria-label="下载状态筛选"
            >
              {[
                ['all', '全部状态'],
                ['downloading', '正在下载'],
                ['queued', '排队等待'],
                ['ready', '5m / 15m 就绪'],
                ['insufficient_history', '历史不足'],
                ['gap', '缺口 / 过期'],
                ['error', '下载失败'],
                ['cached', '仅有缓存'],
              ].map(([v, l]) => (
                <option value={v} key={v}>
                  {l}
                </option>
              ))}
            </select>
            <select
              className="native-select"
              title="按币安官方 underlyingType 元数据筛选资产类别。"
              value={category}
              aria-label="下载资产类别"
              onChange={(e) => {
                setCategory(e.target.value);
                setPage(0);
              }}
            >
              {CATEGORIES.map(([value, text]) => (
                <option key={value} value={value}>
                  {text}
                </option>
              ))}
            </select>
            <select
              className="native-select"
              title="永续包含普通与传统金融永续；交割包含币安当前季度和次季度 USDT 合约。"
              value={contractFilter}
              aria-label="下载合约类型"
              onChange={(e) => {
                setContractFilter(e.target.value);
                setPage(0);
              }}
            >
              <option value="all">全部合约类型</option>
              <option value="perpetual">全部永续</option>
              <option value="delivery">交割合约</option>
            </select>
          </div>
          <div className="download-table">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>合约 / 类别 / 状态</TableHead>
                  <TableHead>已存根数</TableHead>
                  <TableHead>5m / 15m 进度</TableHead>
                  <TableHead>最新价格</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {visible.map((c) => {
                  const historical =
                    c.jobs.find((j) => j.timeframe === '5m')
                      ?.stored_last_price ??
                    c.jobs.find((j) => j.timeframe === '15m')
                      ?.stored_last_price;
                  const live =
                    c.quote && !c.quote.stale && c.quote.connected !== false;
                  return (
                    <TableRow
                      key={c.symbol}
                      className={c.symbol === symbol ? 'selected-download' : ''}
                    >
                      <TableCell>
                        <button
                          className="download-symbol"
                          onClick={() => choose(c.symbol)}
                        >
                          {c.symbol}
                        </button>
                        <small className="contract-meta">
                          {categoryLabel(c)} · {contractLabel(c)} · {quoteOf(c)}
                        </small>
                        <span className={'download-state ' + c.state}>
                          {label(c.state)}
                        </span>
                      </TableCell>
                      <TableCell>
                        <b>{n(c.stored)}</b>
                        <small>本轮接收 {n(c.received)}</small>
                      </TableCell>
                      <TableCell>
                        <div className="download-periods">
                          {TFS.map((t) => {
                            const j = c.jobs.find((x) => x.timeframe === t);
                            return (
                              <button
                                key={t}
                                className={
                                  (j?.state || 'queued') +
                                  (c.symbol === symbol && tf === t
                                    ? ' picked'
                                    : '')
                                }
                                title={`${j?.window_complete ? '历史窗口已核验完整' : label(j?.state)} · ${n(j?.bars)} / ${n(j?.target_bars)} 根${j?.target_bars === 0 ? ' · 新合约尚无已收盘的该周期K线，不补造数据' : ''}${j?.error ? ' · ' + j.error : ''}`}
                                onClick={() => choose(c.symbol, t)}
                              >
                                <span>{t}</span>
                                {j?.state === 'ready' ? (
                                  <Check size={9} />
                                ) : j?.progress_pct != null ? (
                                  <small>{n(j.progress_pct, 1)}%</small>
                                ) : (
                                  <small>—</small>
                                )}
                              </button>
                            );
                          })}
                        </div>
                      </TableCell>
                      <TableCell>
                        <b className={live ? 'up' : ''}>
                          {price(
                            live
                              ? (c.quote?.close ?? c.quote?.price)
                              : historical,
                          )}
                        </b>
                        <small>
                          {quoteOf(c)} · {live ? '当前未收盘' : '已存历史收盘'}
                        </small>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
          {!visible.length && (
            <p className="download-empty-inline">
              {coins.length
                ? '没有符合筛选的币种。'
                : '尚未取得合约范围；已保存的数据会在库存读取后显示。'}
            </p>
          )}
          <div className="download-pagination">
            <span>
              第 {currentPage + 1} / {maxPage + 1} 页 · {filtered.length} 个合约
            </span>
            <div>
              <Button
                variant="ghost"
                size="icon-sm"
                disabled={!currentPage}
                aria-label="上一页币种"
                onClick={() => setPage(currentPage - 1)}
              >
                <ChevronLeft />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                disabled={currentPage >= maxPage}
                aria-label="下一页币种"
                onClick={() => setPage(currentPage + 1)}
              >
                <ChevronRight />
              </Button>
            </div>
          </div>
        </section>
        <section className="panel download-detail">
          <div className="panel-heading">
            <h2>
              <Database size={16} />
              {symbol} <span className="tf-pill">{tf}</span>
            </h2>
            <span className={'download-state ' + (chosen?.state || 'queued')}>
              {label(chosen?.state)}
            </span>
          </div>
          <div className="download-detail-body">
            <div className="download-period-select">
              {TFS.map((t) => (
                <button
                  key={t}
                  onClick={() => choose(symbol, t)}
                  className={tf === t ? 'active' : ''}
                >
                  {t}
                </button>
              ))}
            </div>
            <div className="live-price-box">
              <div>
                <span>
                  {freshQuote ? '当前底层行情 · 内部 1m 流' : '最新已存收盘价'}
                </span>
                <strong className={freshQuote ? 'up' : ''}>
                  {price(
                    freshQuote ? (live?.close ?? live?.price) : closed?.close,
                  )}
                </strong>
                <small>
                  {freshQuote
                    ? '接收于 ' + stamp(live?.observed_at || live?.event_time)
                    : 'K线时间 ' + stamp(closed?.close_time, true)}{' '}
                  UTC+8
                </small>
              </div>
              {freshQuote ? <Radio size={25} /> : <Database size={25} />}
            </div>
            <div className="download-detail-metrics">
              <div>
                <small>本地已存</small>
                <b>{n(detail?.inventory.bars)} 根</b>
              </div>
              <div>
                <small>本周期最低历史</small>
                <b>{n(chosen?.minimum)} 根</b>
              </div>
              <div>
                <small>已接收批次</small>
                <b>{n(chosen?.pages || 0)} 批</b>
              </div>
              <div>
                <small>最近下载写入</small>
                <b>{stamp(chosen?.last_page_at)}</b>
              </div>
            </div>
            <p className="download-range">
              {chosen?.stage && (
                <>
                  {chosen.stage}
                  {chosen.window_missing_bars != null &&
                  chosen.window_missing_bars > 0
                    ? ` · 窗口尚缺 ${n(chosen.window_missing_bars)} 根`
                    : ''}
                  <br />
                </>
              )}
              下载窗口：{stamp(chosen?.requested_start, true)} →{' '}
              {stamp(chosen?.requested_end, true)}
              <br />
              本地数据：{stamp(detail?.inventory.first_open_time, true)} →{' '}
              {stamp(detail?.inventory.last_close_time, true)}
            </p>
            {chosen?.error && (
              <div className="download-error">
                <WifiOff size={14} />
                {chosen.error}
              </div>
            )}
            {detailError && <p className="form-notice">{detailError}</p>}
            {lastBatch?.latest_candle && (
              <div className="download-batch-sample">
                <b>刚下载的批次 · {n(lastBatch.count)} 根</b>
                <span>
                  {stamp(lastBatch.time)} 保存 · 本批最后一根{' '}
                  {stamp(lastBatch.latest_candle.open_time, true)}
                </span>
                <p>
                  开 {price(lastBatch.latest_candle.open)} · 高{' '}
                  {price(lastBatch.latest_candle.high)}
                  <br />低 {price(lastBatch.latest_candle.low)} · 收{' '}
                  {price(lastBatch.latest_candle.close)}
                </p>
                <small>
                  成交额 {n(lastBatch.latest_candle.quote_volume, 2)} ·
                  已收盘、已保存
                </small>
              </div>
            )}
            <div className="download-data-heading">
              <b>本地最新的真实 K 线</b>
              <a
                href={
                  api
                    ? `${api}/api/downloads/${encodeURIComponent(symbol)}/${tf}/export?limit=60000`
                    : undefined
                }
                aria-disabled={!api}
                className="download-export"
              >
                <Download size={13} />
                导出已存 CSV
              </a>
            </div>
            <div className="candle-content-table">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>开盘时间</TableHead>
                    <TableHead>开 / 高</TableHead>
                    <TableHead>低 / 收</TableHead>
                    <TableHead>
                      成交额 · {quoteOf(chosen || { symbol })}
                    </TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {detail?.candles.map((c) => (
                    <TableRow key={c.open_time}>
                      <TableCell>
                        {stamp(c.open_time, true)}
                        <small>已收盘 · 已保存</small>
                      </TableCell>
                      <TableCell>
                        {price(c.open)}
                        <small>{price(c.high)}</small>
                      </TableCell>
                      <TableCell>
                        {price(c.low)}
                        <small className={c.close >= c.open ? 'up' : 'down'}>
                          {price(c.close)}
                        </small>
                      </TableCell>
                      <TableCell>
                        {n(c.quote_volume, 2)}
                        <small>{n(c.trades)} 笔</small>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
            {!detail?.candles.length && (
              <p className="download-empty-inline">
                这个周期还没有保存的 K
                线。下载到数据后会自动出现，不使用演示数据占位。
              </p>
            )}
            <p className="field-help">
              这里显示真实数据内容；内部 1m 流只用于合成 5m / 15m，不能作为正式信号周期。信号判定只使用已收盘 K 线。显示最新 20 根，CSV 最多导出最近 60,000 根。
            </p>
          </div>
        </section>
      </div>
      <section className="panel download-log">
        <div className="panel-heading">
          <h2>
            <Activity size={16} />
            实时下载记录
          </h2>
          <button
            className={'quality-toggle ' + (follow ? 'active' : '')}
            onClick={() => setFollow((v) => !v)}
          >
            {follow ? '全部币种' : '只看 ' + symbol}
          </button>
        </div>
        <div className="download-log-list">
          {feed.slice(0, 50).map((e) => (
            <div
              key={`${data?.session_id}-${e.seq}`}
              className={
                'download-event ' +
                (e.error || e.type.includes('error') ? 'error' : '')
              }
            >
              <time>{stamp(e.time)}</time>
              <button
                onClick={() => {
                  if (e.symbol) choose(e.symbol, e.timeframe);
                }}
              >
                <b>{e.symbol || '采集服务'}</b>
                <span>{e.timeframe || ''}</span>
              </button>
              <p>
                {e.message ||
                  e.error ||
                  (e.count != null
                    ? `已接收并保存 ${n(e.count)} 根`
                    : label(e.type))}
                {e.latest_candle && (
                  <small>
                    最新收盘 {price(e.latest_candle.close)} · 高{' '}
                    {price(e.latest_candle.high)} · 低{' '}
                    {price(e.latest_candle.low)} · 成交额{' '}
                    {n(e.latest_candle.quote_volume, 2)}
                  </small>
                )}
              </p>
              <span className="event-batch">
                {e.type === 'page_saved'
                  ? '写入 ' + n(e.count) + ' 根'
                  : e.type === 'page_empty'
                    ? '空批次'
                    : e.type === 'job_started'
                      ? '恢复缓存'
                      : e.type === 'job_finished'
                        ? '任务完成'
                        : e.error || e.type.includes('error')
                          ? '失败'
                          : '状态'}
              </span>
            </div>
          ))}
        </div>
        {!feed.length && (
          <p className="download-empty-inline">
            等待新批次写入。旧数据保留在本地库存中，不会伪造为本次下载日志。
          </p>
        )}
        <div className="download-log-footer">
          {data?.events_truncated
            ? '部分较早日志已滚动移出，已存 K 线不受影响 · '
            : '保留最近批次 · '}
          错误如实显示 · 页面刷新不停止后台下载 · 已下载数据不会因重连清空
        </div>
      </section>
    </div>
  );
}
