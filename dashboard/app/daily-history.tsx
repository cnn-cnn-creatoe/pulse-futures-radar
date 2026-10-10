'use client';
import {passesStatisticsStrategy} from './strategy-statistics';
import { useCallback, useEffect, useState } from 'react';
import { Dialog, DialogContent, DialogTitle, DialogDescription } from '@/components/ui/dialog';
import './daily-history.css';
import {ActionabilityCell,ActivityCell,type Actionability} from './score-cells';
import {positionView, type SignalQuality} from './monitor-panels';
import {MarketChartButton} from './market-chart-button';

export const beijingDay = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 10);
export const beijingTime = (value?: number | null) => value == null ? '—' :
  new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false });
const num = (value?: number | null, suffix = '') => value == null ? '—' : `${value.toFixed(2)}${suffix}`;
type DailySignal = SignalQuality & {actionability?:Actionability;score_available_max?:number;
  id: string; symbol: string; timeframe: string; close_time: number; pattern: string;
  price: number; activity_score: number; volatility_ratio: number; change_pct: number; relative_volume: number;
  confirmations?: { id: string; name: string; passed: boolean }[];
  available_confirmations?: { id: string; name: string; passed: boolean }[];
};

function DailySignalContext({ signal }: { signal: DailySignal }) {
  const side = signal.pattern === '阳阳' || signal.pattern === '阴阳' ? 'long'
    : signal.pattern === '阴阴' || signal.pattern === '阳阴' ? 'short' : 'wait';
  const prefix = signal.qualified === true && signal.quality_schema_version === 2 ? '强筛' : '指标';
  const position = positionView(signal);
  return <>
    <td><span className={'pattern-tag tone-' + Math.max(0, ['阳阳', '阴阴', '阳阴', '阴阳'].indexOf(signal.pattern))}
      title={(signal.quality_reasons || []).join('；')}>{signal.pattern}</span></td>
    <td><span className={'decision-tag ' + side} title="信号当时的候选方向；是否适合入场请看位置判断">
      {side === 'long' ? `${prefix}多向候选` : side === 'short' ? `${prefix}空向候选` : '方向待定'}</span></td>
    <td><span className={`position-pill ${position.status}`}
      title={`${position.context}：${position.reason}；${position.assessment?.uses_closed_bars_only ? '仅使用信号时已经收盘的K线' : '旧记录未补算'}`}>
      <b>{position.title}</b><small>{position.context}</small></span></td>
  </>;
}

export function SymbolDayHistory({ api, target, onClose }: {
  api: string | null; target: { symbol: string; date: string } | null; onClose: () => void;
}) {
  const [items, setItems] = useState<DailySignal[]>([]);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(0);
  useEffect(() => {
    if (!api || !target) return;
    const controller = new AbortController();
    let cursor = 0;
    let rows: DailySignal[] = [];
    let busy = false;
    async function load() {
      if (busy) return;
      busy = true;
      try {
        let more = true;
        while (more && !controller.signal.aborted) {
          const r = await fetch(`${api}/api/daily-records?date=${target!.date}&symbol=${encodeURIComponent(target!.symbol)}&after=${cursor}`, { signal: controller.signal });
          if (!r.ok) throw Error('当天记录读取失败，请稍后重试');
          const data = await r.json() as {items: DailySignal[]; cursor: number; has_more: boolean};
          rows = [...rows, ...data.items]; cursor = data.cursor; more = data.has_more;
        }
        if (!controller.signal.aborted) { setItems(rows.filter(passesStatisticsStrategy).sort((a, b) => a.close_time - b.close_time)); setError(''); }
      } catch (e) { if (!controller.signal.aborted) setError((e as Error).message); }
      finally { busy = false; if (!controller.signal.aborted) setLoading(false); }
    }
    const initial = setTimeout(() => { setItems([]); setPage(0); setError(''); setLoading(true); void load(); }, 0);
    const timer = setInterval(load, 5000);
    return () => { controller.abort(); clearInterval(timer); clearTimeout(initial); };
  }, [api, target]);
  return <Dialog open={!!target} onOpenChange={(open) => { if (!open) onClose(); }}>
    <DialogContent className="daily-symbol-dialog">
      <DialogTitle>{target?.symbol} · {target?.date} 超买超卖策略记录</DialogTitle>
      <DialogDescription>北京时间 00:00–24:00，仅包含 WaveTrend 超买超卖交叉确认通过的信号；按出现时间从早到晚排列。</DialogDescription>
      <div className="daily-toolbar"><b>{loading ? '正在读取全部记录…' : `当日共 ${items.length} 次`}</b><span>5m：{items.filter(s => s.timeframe === '5m').length} 次 · 15m：{items.filter(s => s.timeframe === '15m').length} 次</span></div>
      {error && <p role="alert" className="form-notice">{error}</p>}
      <div className="daily-table-scroll"><table className="daily-table daily-signal-table"><colgroup>{[3,7,4,6,9,13,7,6,6,6,7,19,7].map((width, i) => <col key={i} style={{width: `${width}%`}}/>)}</colgroup><thead><tr>
        {['次数', '出现时间', '周期', 'K 线形态', '候选方向', '位置判断', '当时价格', '活跃度', '异常倍数', '两根涨跌', '相对成交额','可做度','图表'].map(h => <th key={h}>{h}</th>)}
      </tr></thead><tbody>{items.slice(page * 50, (page + 1) * 50).map((s, i) => <tr key={s.id}>
        <td>{page * 50 + i + 1}</td><td title={beijingTime(s.close_time)}>{new Date(s.close_time).toLocaleTimeString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false})}</td><td>{s.timeframe}</td><DailySignalContext signal={s}/>
        <td>{s.price}</td><td><ActivityCell score={s.activity_score} max={s.score_available_max}/></td><td>{num(s.volatility_ratio, '×')}</td><td>{num(s.change_pct, '%')}</td><td>{num(s.relative_volume, '×')}</td>
        <td><ActionabilityCell value={s.actionability} api={api} symbol={s.symbol}/></td><td><MarketChartButton api={api} symbol={s.symbol} timeframe={s.timeframe} time={s.close_time} label="显示图表" initialRule="indicator:wt_extreme_cross"/></td>
      </tr>)}</tbody></table></div>
      {!loading && !items.length && !error && <p>这一天还没有该币种通过超买超卖交叉确认的记录。</p>}
      <div className="daily-toolbar"><button onClick={() => setPage(p => p - 1)} disabled={!page}>上一页</button><span>第 {page + 1} / {Math.max(1, Math.ceil(items.length / 50))} 页 · 全部记录均可翻页查看</span><button disabled={(page + 1) * 50 >= items.length} onClick={() => setPage(p => p + 1)}>下一页</button></div>
    </DialogContent>
  </Dialog>;
}

type Archive = { date: string; records: number; symbols: number; filename: string; ready: boolean; current: boolean; saved_records: number };
type Dataset = { id: string; label: string; symbol: string; timeframe: string; bars: number };
type Indicator = { id: string; name: string; status?: string; long_expression?: string; short_expression?: string };
type ReplayRow = { id: string; symbol: string; timeframe: string; signal_time: number; signal_price: number; direction: string;
  activity: number; entry_time: number | null; entry_price: number | null; exit_time: number | null; exit_price: number | null;
  net_return_pct: number | null; price_change_pct: number | null; best_move_pct: number | null; worst_move_pct: number | null; status: string };
type ReplayJob = { id: string; state: string; processed: number; total: number; message?: string; items?: ReplayRow[]; total_items?: number;
  basis?: string; indicator_names: string[]; request: { combination: string; holding_bars: number; min_activity: number; fee_bps: number; slippage_bps: number; date?: string; source: string };
  metrics?: { candidates: number; selected: number; completed: number; pending: number; missing_indicator: number; win_rate: number | null; average_return_pct: number | null } };
const builtinIndicators: Indicator[] = [{ id: 'supertrend', name: '超级趋势' }, { id: 'smc', name: 'SMC 市场结构' }, { id: 'ema', name: 'EMA 双均线' }];

export function DailyBacktestPanel({ api, online }: { api: string | null; online: boolean }) {
  const [archives, setArchives] = useState<Archive[]>([]), [datasets, setDatasets] = useState<Dataset[]>([]);
  const [custom, setCustom] = useState<Indicator[]>([]), [source, setSource] = useState('daily');
  const [date, setDate] = useState(beijingDay), [dataset, setDataset] = useState('');
  const [indicators, setIndicators] = useState<string[]>([]), [combination, setCombination] = useState('all');
  const [hold, setHold] = useState(5), [activity, setActivity] = useState(0), [fee, setFee] = useState(5), [slip, setSlip] = useState(3);
  const [error, setError] = useState(''), [working, setWorking] = useState(false), [job, setJob] = useState<ReplayJob | null>(null);
  const [page, setPage] = useState(0), [target, setTarget] = useState<{ symbol: string; date: string } | null>(null);
  const [importSymbol, setImportSymbol] = useState('BTCUSDT'), [importTf, setImportTf] = useState('5m');
  const [firstMonth, setFirstMonth] = useState(''), [lastMonth, setLastMonth] = useState(''), [downloadJob, setDownloadJob] = useState<{ id: string; state: string; message?: string } | null>(null);
  const read = useCallback(async <T,>(path: string): Promise<T> => {
    const r = await fetch(api + path, { signal: AbortSignal.timeout(30000) });
    if (!r.ok) throw Error('读取失败，请检查本地服务后重试');
    return r.json() as Promise<T>;
  }, [api]);
  const reload = useCallback(async () => {
    if (!api || !online) return;
    const values = await Promise.all([read<{items: Archive[]; archive_error: string}>('/api/daily-archives'), read<{items: Dataset[]}>('/api/history'), read<{items: Indicator[]}>('/api/custom-indicators')]);
    setArchives(values[0].items); setDatasets(values[1].items); setCustom(values[2].items);
    setDataset(d => d || values[1].items[0]?.id || '');
    setError(values[0].archive_error ? '每日CSV保存失败：' + values[0].archive_error : '');
  }, [api, online, read]);
  useEffect(() => {
    if (!api || !online) return;
    const initial = setTimeout(() => { reload().catch(e => setError(e.message)); }, 0);
    const timer = setInterval(() => reload().catch(e => setError(e.message)), 30000);
    const restore = localStorage.getItem('pulse-simple-replay');
    if (restore) read<ReplayJob>('/api/replays/' + restore).then(setJob).catch(() => {});
    return () => { clearInterval(timer); clearTimeout(initial); };
  }, [api, online, read, reload]);
  useEffect(() => {
    if (!api || !job?.id) return;
    let active = true;
    async function load() { try {
      const next = await read<ReplayJob>(`/api/replays/${job!.id}?offset=${page * 50}&limit=50`);
      if (active) setJob(next);
    } catch (e) { if (active) setError((e as Error).message); } }
    void load();
    const timer = job.state === 'running' ? setInterval(load, 2000) : undefined;
    return () => { active = false; clearInterval(timer); };
  }, [api, job?.id, job?.state, page, read]);
  useEffect(() => {
    if (!api || downloadJob?.state !== 'running') return;
    let active = true;
    const timer = setInterval(async () => {
      try { const next = await read<{id: string; state: string; message: string; result: Dataset}>('/api/jobs/' + downloadJob.id); if (!active) return;
        setDownloadJob(next); if (next.state === 'completed') { await reload(); setDataset(next.result.id); }
      } catch (e) { if (active) setError((e as Error).message); }
    }, 2000);
    return () => { active = false; clearInterval(timer); };
  }, [api, downloadJob?.id, downloadJob?.state, read, reload]);
  const busy = working || job?.state === 'running';
  async function start() {
    setWorking(true); setError(''); setPage(0);
    try {
      const r = await fetch(api + '/api/replays', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pulse-Client': 'dashboard' },
        body: JSON.stringify({ source, date: source === 'daily' ? date : null, dataset_id: source === 'dataset' ? dataset : null,
          indicators, combination, min_activity: activity, holding_bars: hold, fee_bps: fee, slippage_bps: slip }) });
      const next = await r.json() as ReplayJob & {detail?: unknown};
      if (!r.ok) throw Error(typeof next.detail === 'string' ? next.detail : '请检查日期、指标和数值范围');
      setJob(next); localStorage.setItem('pulse-simple-replay', next.id);
    } catch (e) { setError((e as Error).message); } finally { setWorking(false); }
  }
  async function upload(file: File) {
    if (file.size > 20000000) { setError('文件不能超过20 MB'); return; }
    setWorking(true); setError('');
    try {
      const r = await fetch(`${api}/api/history/import?symbol=${encodeURIComponent(importSymbol)}&timeframe=${importTf}&format=${file.name.endsWith('.json') ? 'json' : 'csv'}`, {
        method: 'POST', headers: { 'Content-Type': 'text/plain', 'X-Pulse-Client': 'dashboard' }, body: await file.text() });
      const data = await r.json() as Dataset & {detail?: unknown}; if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '导入失败');
      await reload(); setDataset(data.id); setSource('dataset');
    } catch (e) { setError((e as Error).message); } finally { setWorking(false); }
  }
  async function download() {
    setWorking(true); setError('');
    try {
      const months: string[] = []; let current = firstMonth;
      while (current && current <= lastMonth && months.length < 13) {
        months.push(current); const [y, m] = current.split('-').map(Number);
        current = `${m === 12 ? y + 1 : y}-${String(m === 12 ? 1 : m + 1).padStart(2, '0')}`;
      }
      if (!months.length || months.length > 12) throw Error('请选择1到12个已经结束的月份');
      const r = await fetch(api + '/api/history/download', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Pulse-Client': 'dashboard' },
        body: JSON.stringify({ symbol: importSymbol, timeframe: importTf, months }) });
      const data = await r.json() as {id: string; state: string; message?: string; detail?: unknown}; if (!r.ok) throw Error(typeof data.detail === 'string' ? data.detail : '下载请求失败');
      setDownloadJob(data);
    } catch (e) { setError((e as Error).message); } finally { setWorking(false); }
  }
  const metrics = job?.metrics;
  return <div className="daily-page">
    <section className="panel daily-section">
      <div className="daily-toolbar"><div><h2>每日记录 · 一天一个 CSV</h2><p>北京时间零点开始新的一天，最近 30 天明细每分钟自动保存。更早信号保留摘要；回测更早日期时，请在下方下载或导入历史 K 线。</p></div><button onClick={() => reload().catch(e => setError(e.message))}>刷新列表</button></div>
      <p className="daily-note">同一份 CSV 包含所有币种、全部原始异常、空策略、各内置指标和组合结果，以及自定义指标结果与参数快照。未记录的指标留空，不编造历史。</p>
      <div className="daily-table-scroll"><table className="daily-table"><thead><tr><th>日期（北京时间）</th><th>全部出现次数</th><th>币种数</th><th>保存状态</th><th>每日 CSV</th><th>操作</th></tr></thead><tbody>
        {archives.map(a => <tr key={a.date}><td>{a.date}{a.current ? ' · 今天' : ''}</td><td>{a.records.toLocaleString()}</td><td>{a.symbols}</td><td>{a.ready ? (a.current ? '持续记录中' : '已保存') : '正在更新CSV'}</td><td><a href={`${api}/api/daily-archives/${a.date}.csv`} download>{a.filename} ↓</a></td><td><button onClick={() => { setDate(a.date); setSource('daily'); document.getElementById('simple-replay-settings')?.scrollIntoView({ behavior: 'smooth' }); }}>用这一天回测</button></td></tr>)}
      </tbody></table></div>
      {!archives.length && <p>{online ? '暂无每日记录，监测产生异常后会自动保存。' : '请连接本地监测服务。'}</p>}
    </section>
    <section className="panel daily-section" id="simple-replay-settings">
      <h2>选数据 → 选指标 → 看之后的结果</h2>
      <fieldset disabled={busy} className="daily-fieldset">
        <div className="daily-form-grid"><label>1. 历史数据来源<select value={source} onChange={e => setSource(e.target.value)}><option value="daily">每天保存的全部异常记录</option><option value="dataset">历史 K 线数据（逐根重算）</option></select></label>
        {source === 'daily' ? <label>选择日期<input type="date" value={date} max={beijingDay()} onChange={e => setDate(e.target.value)} /></label> : <label>选择已保存的数据<select value={dataset} onChange={e => setDataset(e.target.value)}><option value="">请选择历史数据</option>{datasets.map(d => <option value={d.id} key={d.id}>{d.label} · {d.bars.toLocaleString()} 根</option>)}</select></label>}</div>
        <p className="daily-note">{source === 'daily' ? '每日记录覆盖当天实际监测到的候选；内置指标按出现时保存的结果筛选，你写的指标会在当时的历史K线上重新计算。' : '从历史K线重新找两K异常，再用本次选择的指标筛选。计算时不读取后面的K线；历史持仓量和盘口缺失项不会补造。'}</p>
        <h3>2. 选择你的指标</h3><p>不选指标就是“空策略”；选一个测试单项，选多个测试组合。</p>
        <div className="daily-indicators"><button type="button" aria-pressed={!indicators.length} className={!indicators.length ? 'active' : ''} onClick={() => setIndicators([])}>空策略 · 不加指标</button>
          {[...builtinIndicators, ...custom].map(i => <label key={i.id} className={'daily-indicator ' + (indicators.includes(i.id) ? 'active' : '')}>
            <input type="checkbox" checked={indicators.includes(i.id)} disabled={!!i.status && i.status !== 'validated'} onChange={e => setIndicators(old => e.target.checked ? [...old, i.id] : old.filter(id => id !== i.id))} />
            <span>{i.name}{i.status === 'draft' ? '（草稿，需先校验）' : i.status ? ' · 我写的指标' : ''}</span>
          </label>)}
        </div>
        {!custom.length && <p className="daily-note">这里还没有已保存的自定义指标。在“监测规则”中编写、校验并保存后，会出现在这里。</p>}
        {custom.filter(i => indicators.includes(i.id)).map(i => <details key={i.id}><summary>{i.name} · 查看本次代码</summary><pre>多：{i.long_expression || '未设置'}{'\n'}空：{i.short_expression || '未设置'}</pre></details>)}
        <div className="daily-form-grid"><label>组合条件<select value={combination} onChange={e => setCombination(e.target.value)}><option value="all">所选指标全部通过（且）</option><option value="any">任意一个指标通过（或）</option></select></label><label>最低活跃度<input type="number" min={0} max={100} value={activity} onChange={e => setActivity(Number(e.target.value))} /><small>0 表示不按活跃度过滤</small></label><label>出现后观察几根 K 线<input type="number" min={1} max={100} value={hold} onChange={e => setHold(Number(e.target.value))} /><small>{hold} 根：5m 为 {hold * 5} 分钟，15m 为 {hold * 15} 分钟</small></label></div>
        <details><summary>手续费与滑点</summary><div className="daily-form-grid"><label>单边手续费（bps）<input type="number" min={0} max={100} step="0.1" value={fee} onChange={e => setFee(Number(e.target.value))} /></label><label>单边滑点（bps）<input type="number" min={0} max={100} step="0.1" value={slip} onChange={e => setSlip(Number(e.target.value))} /></label></div><p>1 bps = 0.01%。多空按信号方向分别计算。</p></details>
      </fieldset>
      <p className="daily-note">第一版按每条候选独立观察：下一根开盘进入，第 {hold} 根收盘结束，计算扣除双边手续费和滑点后的结果。暂不模拟止盈止损、资金分配和资金费；这不是账户累计收益。</p>
      <button className="daily-primary" disabled={!api || !online || busy || (source === 'dataset' && !dataset)} onClick={start}>{busy ? '正在回测…' : '开始回测这组指标'}</button>
      {job?.state === 'running' && <output>正在逐条检查：{job.processed.toLocaleString()} / {job.total.toLocaleString()}，完成后结果会保存在本地。</output>}
    </section>
    {error && <p className="form-notice" role="alert">{error}</p>}
    {job?.state === 'failed' && <p className="form-notice" role="alert">回测失败：{job.message}</p>}
    {job?.state === 'completed' && metrics && <section className="panel daily-section">
      <div className="daily-toolbar"><div><h2>3. 这组策略选中了哪些币</h2><p>本次策略：{job.indicator_names.join(job.request.combination === 'all' ? ' + ' : ' 或 ') || '空策略'} · 活跃度 ≥{job.request.min_activity} · 观察 {job.request.holding_bars} 根 K 线</p></div><a className="daily-download" href={`${api}/api/replays/${job.id}/results.csv`} download>下载全部结果 CSV ↓</a></div>
      <p className="daily-note">{job.basis}</p>
      <div className="daily-metrics"><div><span>选中的记录 / 原始异常</span><b>{metrics.selected.toLocaleString()} / {metrics.candidates.toLocaleString()}</b></div><div><span>已有后续结果</span><b>{metrics.completed.toLocaleString()}</b></div><div><span>扣费后正收益比例</span><b>{num(metrics.win_rate == null ? null : metrics.win_rate * 100, '%')}</b></div><div><span>每条平均扣费后收益</span><b>{num(metrics.average_return_pct, '%')}</b></div></div>
      <p>后续数据不足或缺口：{metrics.pending} 条，尚未计入收益统计。因所选指标数据不足未入选：{metrics.missing_indicator} 条。</p>
      <div className="daily-table-scroll"><table className="daily-table"><thead><tr>{['币种 / 周期', '出现时间（北京时间）', '方向', '出现时价格', '入场价（含滑点）', '结束价（含滑点）', '价格涨跌', '按方向扣费后收益', '最大有利 / 不利', '结果'].map(h => <th key={h}>{h}</th>)}</tr></thead><tbody>{job.items?.map((r, i) => <tr key={r.id || `${r.symbol}:${r.signal_time}:${i}`}>
        <td>{job.request.source === 'daily' ? <button className="daily-link" onClick={() => setTarget({ symbol: r.symbol, date: job.request.date! })}>{r.symbol}</button> : r.symbol}<small>{r.timeframe}</small></td><td>{beijingTime(r.signal_time)}</td><td>{r.direction === 'long' ? '多' : r.direction === 'short' ? '空' : '中性'}</td><td>{r.signal_price}</td><td title={beijingTime(r.entry_time)}>{r.entry_price == null ? '—' : Number(r.entry_price.toPrecision(8))}</td><td title={beijingTime(r.exit_time)}>{r.exit_price == null ? '—' : Number(r.exit_price.toPrecision(8))}</td><td>{num(r.price_change_pct, '%')}</td><td className={r.net_return_pct == null ? '' : r.net_return_pct >= 0 ? 'daily-positive' : 'daily-negative'}>{num(r.net_return_pct, '%')}</td><td>{num(r.best_move_pct, '%')} / {num(r.worst_move_pct, '%')}</td><td>{r.status}</td>
      </tr>)}</tbody></table></div>
      {!metrics.selected && <p>这组指标没有选出候选。可以减少指标、改为“任一通过”，或将最低活跃度设为 0 再比较。</p>}
      <div className="daily-toolbar"><button disabled={!page} onClick={() => setPage(p => p - 1)}>上一页</button><span>第 {page + 1} / {Math.max(1, Math.ceil((job.total_items || 0) / 50))} 页 · 共 {job.total_items || 0} 条</span><button disabled={(page + 1) * 50 >= (job.total_items || 0)} onClick={() => setPage(p => p + 1)}>下一页</button></div>
    </section>}
    <details className="panel daily-section"><summary>补充历史 K 线：下载 / 导入</summary><p>已有每日记录可直接回测。需要测试更早的行情时，在这里准备 K 线数据；每日候选 CSV 无需重复导入。</p>
      <div className="daily-form-grid"><label>合约<input value={importSymbol} onChange={e => setImportSymbol(e.target.value.toUpperCase())} /></label><label>周期<select value={importTf} onChange={e => setImportTf(e.target.value)}><option>5m</option><option>15m</option></select></label><label>导入 K 线 CSV / JSON<input type="file" accept=".csv,.json" disabled={busy || !online} onChange={e => { const f = e.target.files?.[0]; if (f) void upload(f); e.target.value = ''; }} /></label><label>开始月份<input type="month" value={firstMonth} onChange={e => setFirstMonth(e.target.value)} /></label><label>结束月份<input type="month" value={lastMonth} onChange={e => setLastMonth(e.target.value)} /></label></div>
      <button disabled={busy || !online || downloadJob?.state === 'running'} onClick={download}>下载币安历史 K 线</button>{downloadJob && <p>{downloadJob.state === 'completed' ? '下载完成，已加入上面的历史数据选项。' : downloadJob.message}</p>}
    </details>
    <SymbolDayHistory api={api} target={target} onClose={() => setTarget(null)} />
  </div>;
}
