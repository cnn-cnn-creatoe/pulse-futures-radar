"""Pattern-independent, causal indicator research; no orders or invented equity."""
from __future__ import annotations

import asyncio
import csv
import hashlib
from datetime import datetime
import io
import json
import time
import uuid

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .daily_journal import SimpleReplay, TZ, csv_safe, forward_outcome, local_time
from .engine import TIMEFRAME_MS, _kind
from .periods import NATIVE, floor_bar, ceil_bar, shift, count_bars
from .store import DAY_MS, is_usdt_cached_identity
from .workbench_data import HistoricalPreparation
from .community_indicators import CATALOG as COMMUNITY_CATALOG, IDS as COMMUNITY_IDS, states as community_states
from .quant_research import CATALOG as STRATEGIES, STRATEGY_INDICATORS, reversal_outcomes, signal_rows, display_signal_rows
from .portfolio import simulate as simulate_portfolio
from .script_inventory import inventory as script_inventory
from .source_browser import source_for
from pathlib import Path
from .signal_scores import historical_scores
from .nan_strategy1 import RULE_ID as NAN_ID, events as nan_events, outcomes as nan_outcomes

CATALOG = COMMUNITY_CATALOG
BUILTIN_IDS = {item["id"] for item in CATALOG}
EVENT_IDS = {item["id"] for item in CATALOG if item["kind"] == "event"}


def timestamp(value: str) -> int:
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=TZ)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        raise ValueError("请选择有效的开始和结束日期时间（北京时间）")


class RuleThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    min_activity: float = Field(0, ge=0, le=100)
    min_actionability: float = Field(0, ge=0, le=100)


class WorkbenchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    start: str
    end: str
    scope: str = Field("single", pattern="^(single|multiple|all)$")
    symbols: list[str] = Field(default_factory=lambda: ["BTCUSDT"], max_length=2000)
    timeframes: list[str] = Field(default_factory=lambda: ["5m"], min_length=1, max_length=10)
    engine: str = Field("indicator", pattern="^(indicator|quant)$")
    strategy: str | None = None
    account_mode: bool = False
    initial_capital: float = Field(10000, ge=1, le=100000000)
    margin_per_trade: float = Field(1000, ge=1, le=100000000)
    leverage: float = Field(1, ge=1, le=125)
    capital_mode: str = Field('fixed',pattern='^(fixed|equity)$')
    fill_mode: str = Field('next_open',pattern='^(next_open|signal_close)$')
    max_positions: int = Field(5, ge=1, le=100)
    maintenance_margin_pct: float = Field(0.5, ge=0.01, le=10)
    indicators: list[str] = Field(default_factory=lambda: ["wt_extreme_cross"], max_length=12)
    combination: str = Field("all", pattern="^(all|any)$")
    pattern: str = Field("none", pattern="^(none|all|阳阳|阴阴|阳阴|阴阳)$")
    patterns: list[str] | None = Field(None,max_length=4)
    holding_bars: int = Field(5, ge=1, le=100)
    fee_bps: float = Field(5, ge=0, le=100)
    slippage_bps: float = Field(3, ge=0, le=100)
    purpose: str = Field("backtest", pattern="^(backtest|signals)$")
    auto_download: bool = False
    rule_settings: dict[str, RuleThresholds] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        begin, end = timestamp(self.start), timestamp(self.end)
        if self.patterns is not None and (len(set(self.patterns))!=len(self.patterns) or any(p not in ('阳阳','阴阴','阳阴','阴阳') for p in self.patterns)):
            raise ValueError('形态只能多选阳阳、阴阴、阳阴、阴阳，不可重复')
        if self.account_mode and self.capital_mode=='fixed' and self.margin_per_trade > self.initial_capital:
            raise ValueError("每笔保证金不能超过初始资金")
        if self.account_mode and self.leverage*(self.maintenance_margin_pct/100+self.fee_bps/10000+self.slippage_bps/10000) >= 1:
            raise ValueError("当前杠杆下保证金不足以覆盖维持保证金及成交成本，请降低杠杆")
        if not 0 < end - begin <= 1098 * DAY_MS:
            raise ValueError("结束时间须晚于开始时间，单次最多三年（1098天）")
        if any(tf not in NATIVE for tf in self.timeframes) or len(set(self.timeframes)) != len(self.timeframes):
            raise ValueError("请选择支持的 K 线周期，不可重复")
        self.symbols = list(dict.fromkeys(s.strip().upper() for s in self.symbols if s.strip()))
        if self.scope != "all" and any(not is_usdt_cached_identity(s) for s in self.symbols):
            raise ValueError("请输入 USDT 合约代码，例如 BTCUSDT；交割合约请保留日期后缀")
        if self.scope == "single" and len(self.symbols) != 1:
            raise ValueError("单币种模式请选择一个币种")
        if self.scope == "multiple" and len(self.symbols) < 2:
            raise ValueError("多币种模式请至少选择两个币种")
        if "1s" in self.timeframes and (self.scope == "all" or len(self.symbols)>5 or end-begin>86400000):
            raise ValueError("1 秒周期单次最多 5 个合约、1 天，请缩小范围以控制下载量")
        if not self.indicators:
            raise ValueError("请选择至少一个指标；空形态表示不限制 K 线颜色，不表示没有选币条件")
        if self.engine != "indicator" or set(self.indicators) - BUILTIN_IDS or self.strategy:
            raise ValueError('请选择研究库中已接入的指标策略')
        if NAN_ID in self.indicators and len(self.indicators)!=1:
            raise ValueError('楠策略有独立持仓状态，请单独选择研究，不与其他入场规则混合')
        if set(self.rule_settings) - set(self.indicators):
            raise ValueError('评分设置只能属于已选择的指标策略')
        if len(set(self.indicators)) != len(self.indicators):
            raise ValueError("指标不能重复")
        if self.engine == "quant":
            if self.strategy not in STRATEGY_INDICATORS:
                raise ValueError("请选择已接入的量化策略")
            self.indicators = STRATEGY_INDICATORS[self.strategy][:]
            self.pattern = "none"
            self.combination = "all"
        return self


def rule_overlays(candles,start,end,rules):
    return []

def catalogue(registry):
    return list(CATALOG)


def segments(candles):
    """Reset recursive indicators across missing bars; never bridge a gap."""
    result = []
    for c in candles:
        if not c.closed:
            continue
        if not result or c.open_time != result[-1][-1].close_time + 1:
            result.append([])
        result[-1].append(c)
    return result


def prepare_signal_series(candles,settings,ids):
    if set(ids) - BUILTIN_IDS:
        raise ValueError('指标策略已移除，请重新选择 WaveTrend 超买超卖交叉')
    return {(chunk[0].symbol,chunk[0].timeframe,chunk[0].open_time):
        dict(additional=community_states(chunk,ids)) for chunk in segments(candles)}


def signal_stream(candles, settings, specs, options, cancelled=lambda: False, prepared=None):
    """Vector recurrences are causal. SMC and custom expressions use prefixes only."""
    if options.get('engine') == 'quant' or specs:
        raise ValueError('该指标策略已移除，请重新选择 WaveTrend 超买超卖交叉')
    ids = options["indicators"]
    start, end = timestamp(options["start"]), timestamp(options["end"])
    if NAN_ID in ids:
        if ids!=[NAN_ID]:raise ValueError('楠策略请单独运行')
        for chunk in segments(candles):yield from nan_events(chunk,start,end,cancelled)
        return
    if prepared is None:prepared=prepare_signal_series(candles,settings,ids)
    for chunk in segments(candles):
        data=prepared[(chunk[0].symbol,chunk[0].timeframe,chunk[0].open_time)]
        additional = data['additional']
        previous = {"long": None, "short": None}
        previous_ema = None
        for index, c in enumerate(chunk):
            if cancelled():
                return
            if c.close_time > end:
                break
            state = dict(additional[index]) if additional is not None else {}
            if c.close_time < start - TIMEFRAME_MS[c.timeframe]:
                continue
            pattern = None
            if index:
                a, b = _kind(chunk[index - 1], 1e-12), _kind(c, 1e-12)
                pattern = a + b if a in ("阳", "阴") and b in ("阳", "阴") else "十字 / 其他"
            for side, direction in (("long", "up"), ("short", "down")):
                values = {key: None if key not in state else state[key] in (direction,"both") for key in ids}
                passed = all(v is True for v in values.values()) if options["combination"] == "all" else any(v is True for v in values.values())
                ready = all(v is not None for v in values.values()) if options["combination"] == "all" else any(v is not None for v in values.values())
                event = any(key in EVENT_IDS and value is True for key, value in values.items())
                # No artificial event on initial warm-up or after a data gap.
                trigger = ready and passed and (event or previous[side] is False)
                previous[side] = passed if ready else None
                if not trigger or not start <= c.close_time <= end:
                    continue
                if options["pattern"] != "none" and (pattern not in ("阳阳", "阴阴", "阳阴", "阴阳") or options["pattern"] not in ("all", pattern)):
                    continue
                if options.get('engine')!='quant' and options.get('patterns') and pattern not in options['patterns']:
                    continue
                yield {"id": f"{c.symbol}:{c.timeframe}:{c.close_time}:{side}", "symbol": c.symbol,
                    "timeframe": c.timeframe, "close_time": c.close_time, "price": c.close, "direction": side,
                    "pattern": pattern, "segment_start":chunk[0].open_time, "signal_kind": "指标触发点" if event else "筛选入选点（非原生买卖）",
                    "indicator_results": values}


class Workbench(SimpleReplay):
    fetch_page = None

    @staticmethod
    def summary(job):
        return {key: value for key, value in job.items() if key not in {"items", "builtin_code_snapshot"}}

    async def execute(self, job, settings, specs):
        try:
            if job["request"]["purpose"] == "backtest" or job['request'].get('auto_download'):
                preparation = asyncio.create_task(HistoricalPreparation(self.store,self.research,self.fetch_page).run(
                    job,timestamp(job["request"]["start"]),min(timestamp(job["request"]["end"]),job["as_of"])))
                try:
                    while not preparation.done() and not job.get("cancel_requested"):
                        await asyncio.wait({preparation},timeout=.25)
                    if job.get("cancel_requested"):
                        preparation.cancel()
                        await asyncio.gather(preparation,return_exceptions=True)
                        prepared = False
                    else:
                        prepared = await preparation
                finally:
                    if not preparation.done():
                        preparation.cancel()
                        await asyncio.gather(preparation,return_exceptions=True)
                if not prepared:
                    job.update(state="cancelled",message="已停止，已下载数据保留")
                    return
            else:
                job["logs"].append("实时筛选只读取本地行情；缺失历史在行情下载中补充，自动刷新不触发批量 REST 补数。")
            if not job.get("cancel_requested"):
                await asyncio.to_thread(self.work,job,settings,specs)
            elif job["state"] == "running":
                job.update(state="cancelled",message="已停止，已保存数据保留")
        except Exception as error:
            job.update(state="failed",message=str(error)[:500])
            job["logs"].append(str(error)[:500])
        await asyncio.to_thread(self.research._atomic_write,self.root/f'{job["id"]}.json',json.dumps(job,ensure_ascii=False,allow_nan=False))

    def get(self, ident):
        in_memory = ident in self.jobs
        job = super().get(ident)
        if job and not in_memory and job["state"] == "running":
            job.update(state="failed", message="上次研究因服务退出而中断，请重新运行")
        return job

    def start(self, request, settings):
        if any(job["state"] == "running" for job in self.jobs.values()):
            raise ValueError("已有指标研究运行中，请等待完成或停止后再运行")
        specs = []
        for key in request.indicators:
            if key in BUILTIN_IDS:
                continue
            spec = self.registry.get(key)
            if not spec or spec.get("status") != "validated":
                raise ValueError(f"指标 {key} 尚未校验保存")
            specs.append(spec)
        ident = uuid.uuid4().hex
        names = {s["id"]: s["name"] for s in catalogue(self.registry)}
        job = dict(id=ident, state="running", processed=0, total=0, created_at=int(time.time() * 1000),
            request=request.model_dump(), as_of=int(time.time()*1000), policy_snapshot=settings.model_dump(), custom_indicator_snapshot=specs,
            indicator_names=[names[k] for k in request.indicators], logs=["已冻结本次指标代码和参数；只读取已收盘历史 K 线。"])
        job["builtin_code_snapshot"] = {name: (Path(__file__).parent/name).read_text("utf-8") for name in ("workbench.py","indicators.py","community_indicators.py","quant_research.py","portfolio.py","daily_journal.py","periods.py","signal_scores.py","nan_strategy1.py")}
        job["builtin_code_hash"] = hashlib.sha256(json.dumps(job["builtin_code_snapshot"],sort_keys=True).encode()).hexdigest()
        if request.engine == "quant":
            job["indicator_names"] = [next(s["name"] for s in STRATEGIES if s["id"] == request.strategy)]
        self.jobs[ident] = job
        task = asyncio.create_task(self.execute(job, settings.model_copy(deep=True), specs))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.summary(job)

    def work(self, job, settings, specs):
        options = job["request"]
        as_of = job.get("as_of",int(time.time()*1000))
        start, end = timestamp(options["start"]), min(timestamp(options["end"]),as_of)
        inventory = self.store.candle_inventory(None if options["scope"] == "all" else options["symbols"])
        inventory = [r for r in inventory if r["timeframe"] in options["timeframes"] and is_usdt_cached_identity(r["symbol"])]
        # Imported/downloaded monthly data can also be read directly, without
        # copying it into the live database or changing live collection.
        datasets = [d for d in self.research.datasets() if d.get("timeframe") in options["timeframes"] and is_usdt_cached_identity(d.get("symbol", ""))
            and (options["scope"] == "all" or d.get("symbol") in options["symbols"])]
        keys = job.get("series_keys") or sorted({(r["symbol"], r["timeframe"]) for r in inventory + datasets})
        if not keys:
            raise ValueError("所选币种尚无本地 K 线，请先在行情下载中下载或导入历史数据")
        estimated = sum(max(0, (end - start) // TIMEFRAME_MS[tf] + 1) for _, tf in keys)
        if estimated > 2_000_000:
            raise ValueError("本次范围超过 200 万根待检查 K 线，请缩短日期或减少币种后运行")
        job["total"] = len(keys)
        job["logs"].append(f"读取 {len(keys)} 组币种 / 周期；全部币种指本地保存过的合约，包含已保留的退市历史。")
        selected, coverage, account_market = [], [], {}
        for symbol, tf in keys:
            if job.get("cancel_requested"):
                job.update(state="cancelled", message="已停止；未生成完整结果")
                return
            interval = TIMEFRAME_MS[tf]
            begin = max(0,shift(floor_bar(start,tf),tf,-2020))
            stop = min(shift(floor_bar(end,tf),tf,options["holding_bars"]+1)-1,as_of)
            candles = self.store.get_candles(symbol, tf, limit=200000, start=begin, end=stop)
            merged = {c.open_time: c for c in candles if c.closed and c.close_time <= stop}
            for meta in datasets:
                if (meta["symbol"], meta["timeframe"]) == (symbol, tf):
                    imported, _ = self.research.load(meta["id"])
                    for c in imported:
                        if c.closed and begin <= c.open_time and c.close_time <= stop:
                            merged.setdefault(c.open_time, c)
            candles = sorted(merged.values(), key=lambda c: c.open_time)
            if options.get("account_mode") and options["purpose"] != "signals":
                account_market[(symbol,tf)] = [c for c in candles if c.open_time >= start and c.close_time <= end]
            in_range = [c for c in candles if start <= c.close_time <= end]
            expected = count_bars(ceil_bar(start+1,tf),floor_bar(end+1,tf),tf)
            gaps = sum(max(0, count_bars(a.open_time,b.open_time,tf)-2) for a, b in zip(in_range, in_range[1:]))
            coverage.append(dict(symbol=symbol, timeframe=tf, bars=len(in_range), expected=expected,
                missing=max(0, expected - len(in_range)), internal_gaps=gaps,
                first= in_range[0].close_time if in_range else None, last=in_range[-1].close_time if in_range else None,
                warmup=sum(c.close_time < start for c in candles),
                sha256=hashlib.sha256(json.dumps([c.to_dict() for c in candles],sort_keys=True).encode()).hexdigest()))
            job["current"] = f"{symbol} · {tf}"
            signals = signal_stream(candles, settings, specs, options, lambda: job.get("cancel_requested", False))
            if options['purpose']=='signals':
                selected.extend(signal_rows(signals,options.get('strategy') if options['engine']=='quant' else None))
                signals=[]
            elif options['indicators']==[NAN_ID]:
                outcomes=nan_outcomes(list(signals),[c for c in candles if c.close_time<=end],options)
                if options.get('patterns'):outcomes=[r for r in outcomes if r['pattern'] in options['patterns']]
                selected.extend(outcomes);signals=[]
            elif options["engine"] == "quant":
                outcomes=reversal_outcomes(list(signals), [c for c in candles if c.close_time <= end], options)
                # Entry selection must never remove the original exit event.
                # Compute native exits on the full stream before filtering rows.
                if options.get('patterns'):
                    outcomes=[r for r in outcomes if r.get('pattern') in options['patterns']]
                selected.extend(outcomes)
                signals = []
            for signal in signals:
                if job.get("cancel_requested"):
                    job.update(state="cancelled", message="已停止；未生成完整结果")
                    return
                row = forward_outcome(signal, candles, options)
                row.update(signal_kind=signal["signal_kind"], indicator_results=signal["indicator_results"])
                selected.append(row)
            if job.get("cancel_requested"):
                job.update(state="cancelled", message="已停止；未生成完整结果")
                return
            job["processed"] += 1
        present = {k[0] for k in keys}
        missing_symbols = sorted(set(options["symbols"]) - present) if options["scope"] != "all" else []
        selected.sort(key=lambda r: (r["signal_time"], r["symbol"], r["timeframe"], r["direction"]))
        thresholds=options.get('rule_settings',{}).get(options['indicators'][0],{})
        if thresholds.get('min_activity',0)>0 or thresholds.get('min_actionability',0)>0:
            from .rule_library import passes_scores
            filtered=[]
            for row in selected:
                if job.get('cancel_requested'):
                    job.update(state='cancelled',message='已停止');return
                scores=historical_scores(self.store,row['symbol'],row['timeframe'],row['signal_time'],row['direction'],settings.model_dump_json(),int(time.time()//60),self.research)
                if str(row.get('action','')).startswith('close_') or passes_scores(scores,thresholds):filtered.append({**row,**scores})
            selected=filtered
        complete = [r for r in selected if r["net_return_pct"] is not None]
        returns = [r["net_return_pct"] for r in complete]
        positive = sum(v for v in returns if v > 0)
        negative = -sum(v for v in returns if v < 0)
        metrics = dict(selected=len(selected), completed=len(complete), pending=len(selected)-len(complete),
            win_rate=sum(v > 0 for v in returns)/len(returns) if returns else None,
            average_return_pct=sum(returns)/len(returns) if returns else None,
            profit_factor=positive/negative if negative else None,
            wins=sum(v > 0 for v in returns), losses=sum(v < 0 for v in returns),
            bars=sum(r["bars"] for r in coverage), missing_bars=sum(r["missing"] for r in coverage),
            account_return=None, max_drawdown=None, sharpe=None)
        job.update(items=selected, coverage=coverage, missing_symbols=missing_symbols, metrics=metrics,
            basis="逐根收盘确认，下一根开盘模拟进入，固定观察期结束；每条信号独立研究，可能重叠，不是账户收益。信号日期范围以收盘时间为准，后续结果可延伸至范围之外。",
            evidence=[
                "已保存：本次指标代码、参数、币种范围、数据覆盖及逐条后续结果。",
                "已计入：双边手续费、固定滑点；缺失 K 线不跨越计算。",
                "尚缺：资金费率历史、标记价格、盘口深度与成交约束、完整退市样本。",
                "尚未验证：样本外、滚动检验、参数稳定性、不同市况与成本压力测试。",
                "账户收益 / 回撤 / 夏普：待量化策略定义资金、仓位、出场和风控后计算。"],
            state="completed", completed_at=int(time.time() * 1000))
        job["logs"].append(f"完成：{len(selected)} 条入选记录，{len(complete)} 条后续结果；未完成结果保留并排除收益统计。")
        if options["engine"] == "quant":
            job["basis"] = "反向条件平仓反手：收盘确认，下一根开盘模拟成交。每组币种 / 周期单独持仓，不叠加；末尾未平仓单保留，不当作盈利。不同组尚未分配共享资金，汇总不是账户收益。"
            job["evidence"] = ["已计入双边手续费及滑点；未计资金费率、交易数量精度和盘口成交约束。", "策略无固定止损；仅供研究，不可据此直接实盘。", "未进行完整账户资金分配，账户收益、回撤、夏普不填造。"]
        if options['purpose']=='signals':
            job['basis']='只记录已收盘的入选、开仓和平仓条件；未下单，未计算回测收益。策略仓位状态从所选开始时间为空仓起算。'
            job['evidence']=['只使用已收盘K线；未完成K线不确认信号。','策略开平信号不是账户实际持仓或委托。']
        if options.get('strategy') in ('hammers_stars','threecommas_default'):
            job['basis']='原版条件且空仓才入场，下一根开盘模拟；固定原生ATR止损和1R止盈，不按反向条件平仓。触价时间以本根收盘确认标记，非真实毫秒成交时间。'
            job['evidence']=['OHLC默认先走距开盘更近的一端；等距且同时触及止盈止损时保守按止损优先，逐笔标明。','资金数量使用页面设置覆盖原始定仓；手续费与固定滑点使用页面设置。','已计成本，不包含资金费率和真实逐笔成交回放。']
        if options['indicators']==[NAN_ID] and options['purpose']!='signals':
            job['basis']='楠策略原代码：WT转折先平后开，未添加止盈止损；按选择的成交与资金口径模拟。'
            job['evidence']=['信号逐根复现原代码默认参数；盘中预览不能由历史OHLC还原。','本金、杠杆、仓位比例、费用是回测假设，原indicator脚本未定义。','未计资金费率；期末未平仓按市值估值，不强制虚构平仓。']
        if options.get("account_mode") and options["purpose"] != "signals":
            if any(c["internal_gaps"] or c["missing"] for c in coverage) or missing_symbols:
                raise ValueError("账户回测所选行情有缺口，请补齐数据后再运行")
            job["portfolio"] = simulate_portfolio(selected,account_market,options,start,end,lambda:job.get("cancel_requested",False))
            metrics.update(account_return=job["portfolio"]["return_pct"],max_drawdown=job["portfolio"]["max_drawdown_pct"])
            job["basis"] = job["portfolio"]["model"]
            job["evidence"] = job["portfolio"]["limitations"]
            job["logs"].append(f"账户模拟：已平仓 {job['portfolio']['closed']} 笔，持仓 {job['portfolio']['open']} 笔，跳过 {job['portfolio']['skipped']} 笔。")
        if options.get('patterns') and options['engine']=='quant':
            job['basis']+=' 形态仅筛选入场记录；退出仍采用原策略完整条件，不添加平仓规则。' if options['purpose']!='signals' else ' 形态仅筛选显示记录，不改变原策略仓位流。'
        # Store immutable signal evidence once per rule version, independent of
        # rerun date ranges, observation period and repeatedly refreshed results.
        rule = {"indicators":sorted(options["indicators"]),"combination":options["combination"],"pattern":options["pattern"],
            "rule_settings":options.get("rule_settings",{}),"patterns":options.get("patterns"),"policy":settings.model_dump(),"custom":specs,"engine":options["engine"],"strategy":options.get("strategy"),"builtin_code_hash":job.get("builtin_code_hash")}
        digest = hashlib.sha256(json.dumps(rule,sort_keys=True).encode()).hexdigest()[:24]
        names = dict(zip(options["indicators"],job["indicator_names"]))
        self.store.save_indicator_events([{ "id":row["id"]+":"+digest, "symbol":row["symbol"], "timeframe":row["timeframe"],
            "close_time":row["signal_time"], "price":row["signal_price"], "pattern":row["pattern"], "direction":row["direction"],
            "signal_kind":row["signal_kind"], "record_origin":"指标研究重算（不是当时实时告警）", "policy_snapshot":settings.model_dump(),
            "policy_hash":digest, "custom_indicator_snapshot":specs, "research_settings":rule,
            "available_confirmations":[{"id":key,"name":names[key],"passed":value} for key,value in row["indicator_results"].items()]
        } for row in selected])
        job["logs"].append("入选证据已按信号日期加入每日 CSV 归档队列；同规则版本重复扫描不重复记数。")
        self.export(job)

    def export(self, job):
        fields = {"symbol":"币种", "timeframe":"周期", "signal_time":"信号确认时间_北京时间", "signal_kind":"信号性质",
            "direction":"方向", "pattern":"形态", "signal_price":"信号价格", "entry_time":"模拟入场时间_北京时间", "entry_price":"模拟入场价",
            "exit_time":"模拟退出时间_北京时间", "exit_price":"模拟退出价", "net_return_pct":"扣费后收益百分比",
            "best_move_pct":"最大有利百分比", "worst_move_pct":"最大不利百分比", "status":"状态", "indicator_results":"指标结果",
            "account_status":"账户执行状态", "quantity":"模拟数量", "margin":"保证金USDT", "notional":"名义金额USDT",
            "account_entry_time":"账户入场时间_北京时间", "account_exit_time":"账户退出时间_北京时间",
            "account_entry_price":"账户入场价", "account_exit_price":"账户退出价", "account_pnl":"账户已实现净盈亏USDT", "account_unrealized":"账户浮动盈亏USDT", "account_fees":"账户手续费USDT"}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=[*fields.values(), "本次设置", "指标代码快照", "参数快照"])
        writer.writeheader()
        for row in job["items"]:
            exported = {label: csv_safe(local_time(row.get(key)) if key.endswith("_time") else row.get(key)) for key, label in fields.items()}
            exported.update({"本次设置":csv_safe(job["request"]), "指标代码快照":csv_safe(job["custom_indicator_snapshot"]), "参数快照":csv_safe(job["policy_snapshot"])})
            writer.writerow(exported)
        self.research._atomic_write(self.root / f'{job["id"]}.csv', "\ufeff" + buffer.getvalue())


def install_workbench_routes(app, service, require_dashboard, get_settings, live_snapshot=None):
    @app.get('/api/workbench/intrabar')
    def intrabar(symbol:str,timeframe:str,engine:str,rule:str):
        if timeframe not in NATIVE or timeframe=='1s' or not is_usdt_cached_identity(symbol):raise HTTPException(422,'请选择有效合约和周期')
        from .intrabar import evaluate
        try:return evaluate(service.store,live_snapshot(symbol) if live_snapshot else None,get_settings(),symbol,timeframe,engine,rule,int(time.time()*1000))
        except ValueError as exc:raise HTTPException(422,str(exc))
    @app.get('/api/workbench/shortcuts')
    def shortcut_catalog():
        from .shortcut_rules import options,selected
        return dict(options=options(),items=selected())

    @app.put('/api/workbench/shortcuts')
    async def shortcut_save(request:Request):
        require_dashboard(request)
        from .shortcut_rules import save
        try:return dict(items=save((await request.json()).get('ids')))
        except ValueError as exc:raise HTTPException(422,str(exc))

    @app.get('/api/workbench/score-validation')
    def score_validation():
        path=Path(__file__).resolve().parents[1]/'data/score-validation/f96aacb6528b453eca1d2a0c.json'
        if not path.exists():return {'available':False}
        report=json.loads(path.read_text('utf8'))
        return {'available':True,**{k:report.get(k) for k in ('dataset','sample_count','protocol','findings','summary','structure_plan_check','limitations')}}

    @app.get("/api/workbench/catalog")
    def catalog():
        return {"indicators": catalogue(service.registry), "strategies": STRATEGIES}

    @app.get("/api/workbench/script-inventory")
    def source_inventory():
        return script_inventory()

    @app.get("/api/workbench/source")
    def rule_source(rule_id: str):
        try:
            return source_for(rule_id, service.registry)
        except ValueError as error:
            raise HTTPException(404, str(error))

    @app.get("/api/workbench/contracts")
    def contracts():
        values = {r["symbol"]:dict(symbol=r["symbol"],name=r.get("baseAsset") or r["symbol"],
            category=r.get("underlyingType") or "",active=r.get("snapshot_active",False))
            for r in service.store.list_universe(False) if is_usdt_cached_identity(r["symbol"],r)}
        return {"items":sorted(values.values(),key=lambda r:r["symbol"])}

    @app.post("/api/workbench/runs")
    async def start(body: WorkbenchRequest, request: Request):
        require_dashboard(request)
        try:
            return service.start(body, get_settings())
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/workbench/runs")
    def history():
        disk = {p.stem for p in service.root.glob("*.json")}
        jobs = [service.get(key) for key in disk | service.jobs.keys()]
        return {"items": sorted([dict(id=j["id"], state=j["state"], created_at=j["created_at"], request=j["request"], indicator_names=j["indicator_names"]) for j in jobs if j and j.get('request',{}).get('engine')=='indicator' and set(j.get('request',{}).get('indicators',[])) and set(j['request']['indicators'])<=BUILTIN_IDS], key=lambda j:j["created_at"], reverse=True)}

    @app.get("/api/workbench/active-tasks")
    def active_tasks():
        return {"items":[{key:j.get(key) for key in ("id","request","indicator_names","created_at","processed","total","current","preparation","cancel_requested")}
                         for j in service.jobs.values() if j.get("state") == "running"]}

    @app.get("/api/workbench/runs/{ident}")
    def result(ident: str, offset: int=Query(0, ge=0), limit: int=Query(50, ge=1, le=500), symbol: str="", order: str=Query("asc", pattern="^(asc|desc)$")):
        job = service.get(ident)
        if not job:
            raise HTTPException(404, "研究记录不存在")
        if job.get('request',{}).get('engine')!='indicator' or not set(job.get('request',{}).get('indicators',[])) or bool(set(job['request']['indicators'])-BUILTIN_IDS):
            raise HTTPException(410,'此研究使用的指标策略已移除，请重新运行 WaveTrend 研究')
        rows = [{**r, "row_index": i} for i, r in enumerate(job.get("items", [])) if not symbol or r["symbol"] == symbol]
        if job['request'].get('purpose')=='signals' and job['request']['engine']=='quant':rows=display_signal_rows(rows,job['request'].get('strategy'))
        if job['request'].get('purpose')=='signals' and job['request'].get('patterns'):
            rows=[r for r in rows if str(r.get('action','')).startswith('close_') or r.get('pattern') in job['request']['patterns']]
        rows.sort(key=lambda r: r["signal_time"], reverse=order == "desc")
        visible=rows[offset:offset+limit]
        if job['request'].get('purpose')=='signals':
            config=json.dumps(job.get('policy_snapshot') or get_settings().model_dump())
            visible=[{**r,**historical_scores(service.store,r['symbol'],r['timeframe'],r['signal_time'],r['direction'],config,int(time.time()//60),service.research)} for r in visible]
        return {**service.summary(job), "items":visible, "total_items":len(rows)}

    def chart_tick_size(symbol):
        metadata=next((m for m in service.store.list_universe(False) if m.get('symbol')==symbol),{})
        for item in metadata.get('filters',[]):
            if item.get('filterType')=='PRICE_FILTER':
                try:
                    value=float(item.get('tickSize',0))
                    if 0<value<float('inf'):return value
                except (TypeError,ValueError):pass
        return None

    @app.get('/api/workbench/signal-score')
    def signal_score(symbol:str,timeframe:str,signal_time:int,direction:str,run_id:str=''):
        if not is_usdt_cached_identity(symbol) or timeframe not in NATIVE or direction not in ('long','short','record') or signal_time<0:
            raise HTTPException(422,'无效信号参数')
        job=service.get(run_id) if run_id else None
        if run_id and not job:raise HTTPException(404,'原信号记录不存在')
        config=json.dumps(job.get('policy_snapshot') if job and job.get('policy_snapshot') else get_settings().model_dump())
        return dict(symbol=symbol,timeframe=timeframe,signal_time=signal_time,direction=direction,
                    **historical_scores(service.store,symbol,timeframe,signal_time,direction,config,int(time.time()//60),service.research))

    @app.get('/api/workbench/market-chart')
    def market_chart(symbol:str,timeframe:str='5m',signal_time:int|None=None,engine:str='indicator',rule:str='wt_extreme_cross',history_days:int=30):
        if not is_usdt_cached_identity(symbol) or timeframe not in NATIVE:raise HTTPException(422,'无效合约或周期')
        valid_rules={i['id']:i['name'] for i in catalogue(service.registry) if i.get('kind')=='event'} if engine=='indicator' else {i['id']:i['name'] for i in STRATEGIES} if engine=='quant' else {}
        if engine=='anomaly':valid_rules={'two_k':'两 K 异常 · 列表多空候选'}
        if rule not in valid_rules:raise HTTPException(422,'请选择已接入的买卖指标或策略')
        now=int(time.time()*1000);at=min(signal_time or now,now)
        day=(at+8*3600000)//86400000*86400000-8*3600000
        if not 1 <= history_days <= 60:raise HTTPException(422,'历史范围应为1至60天')
        days=history_days
        start=max(0,day-(days-1)*86400000);end=min(day+86400000-1,now)
        bars=service.store.get_candles(symbol,timeframe,start=start,end=end,limit=100000)
        warmup=service.store.get_candles(symbol,timeframe,end=start-1,limit=2020)
        if engine=='anomaly':
            from .telegram import signal_direction
            by_close={c.close_time:c for c in bars if c.closed and c.close_time<=end}
            marks=[]
            for alert in service.store.chart_alerts(symbol,timeframe,start,end):
                stamp=alert.get('second_candle_close_time',alert.get('close_time'));bar=by_close.get(stamp)
                direction=signal_direction(alert)
                if bar is None or direction not in ('long','short'):continue
                marks.append(dict(time=stamp,score_time=stamp,price=bar.close,
                    action='candidate_'+direction,pattern=alert.get('pattern'),
                    label='两 K 异常 · '+('多向候选' if direction=='long' else '空向候选')+' · 原始列表记录，非指标买卖或成交',
                    selected=signal_time is not None and stamp==at))
            if signal_time is None and marks:marks[-1]['selected']=True
            return dict(tick_size=chart_tick_size(symbol),row=dict(symbol=symbol,timeframe=timeframe,signal_time=at),
                candles=[c.to_dict() for c in bars if c.closed and c.close_time<=end],markers=marks,live=True,start=start,end=end,
                note='箭头对应原始两 K 异常列表的收盘时间和方向；多空候选不等于指标触发、入场或成交。')
        calculation_start=warmup[0].open_time if engine=='quant' and warmup else start
        options=dict(start=local_time(calculation_start),end=datetime.fromtimestamp(end/1000,TZ).isoformat(),indicators=STRATEGY_INDICATORS[rule] if engine=='quant' else [rule],combination='all',pattern='none',engine=engine,strategy=rule if engine=='quant' else None)
        events=signal_stream(warmup+bars,get_settings(),[],options)
        rows=signal_rows(events,rule if engine=='quant' else None)
        marks=[]
        for r in rows:
            if r['signal_time']<start:continue
            kind=r['signal_kind']
            action='close_long' if kind=='卖出平多' else 'close_short' if kind=='买回平空' else 'buy' if r['direction']=='long' else 'sell'
            marks.append(dict(time=r['signal_time'],price=r['signal_price'],action=action,pattern=r.get('pattern'),label=valid_rules[rule]+' · '+kind+' · '+r['action_basis']+' · 非成交记录',selected=False))
        exact=next((m for m in marks if m['time']==at),None)
        if signal_time is not None and exact:
            exact['selected']=True
        elif signal_time is not None:
            anchor=next((c for c in bars if c.open_time<=at<=c.close_time and c.closed),None)
            if anchor:
                label='列表记录定位 · 同时有当前规则信号，可点击买卖箭头查看' if exact else '列表记录定位 · 当前规则在此时没有买卖信号'
                marks.append(dict(time=at,price=anchor.close,action='record',label=label,selected=True,score_time=anchor.close_time))
        elif marks:marks[-1]['selected']=True
        overlays=rule_overlays([c for c in warmup+bars if c.closed],start,end,[rule])
        return dict(overlays=overlays,tick_size=chart_tick_size(symbol),row=dict(symbol=symbol,timeframe=timeframe,signal_time=at),candles=[c.to_dict() for c in bars if c.closed and c.close_time<=end],markers=marks,live=True,start=start,end=end,note=valid_rules[rule]+' · 本地已收盘条件；箭头为信号价，非实际成交；缺口不跨越计算。')

    @app.post("/api/workbench/runs/{ident}/stop")
    def stop(ident: str, request: Request):
        require_dashboard(request)
        job = service.get(ident)
        if not job:
            raise HTTPException(404, "研究记录不存在")
        if job.get('request',{}).get('engine')!='indicator' or not set(job.get('request',{}).get('indicators',[])) or bool(set(job['request']['indicators'])-BUILTIN_IDS):
            raise HTTPException(410,'此研究使用的指标策略已移除，请重新运行 WaveTrend 研究')
        if job.get("state") == "running":
            job["cancel_requested"] = True
        return {"requested":True}

    @app.get("/api/workbench/runs/{ident}/results.csv")
    def export(ident: str):
        job = service.get(ident)
        if not job or job["state"] != "completed":
            raise HTTPException(404, "研究尚未完成")
        return FileResponse(service.root/f"{ident}.csv", filename=f"indicator-{ident}.csv", media_type="text/csv; charset=utf-8")

    @app.get("/api/workbench/runs/{ident}/chart")
    def chart(ident: str, row: int=Query(0, ge=0), refresh: bool=False, timeframe: str|None=None, rule: str|None=None):
        job = service.get(ident)
        if not job or row >= len(job.get("items", [])):
            raise HTTPException(404, "请先选择已完成研究中的一条记录")
        item = dict(job["items"][row])
        live = job['request'].get('purpose') == 'signals'
        if timeframe is not None or rule is not None:
            if not live:raise HTTPException(422,'历史成交图不能切换策略或周期；请重新回测')
            options=dict(job['request'])
            if timeframe is not None:
                if timeframe not in NATIVE or timeframe=='1s':raise HTTPException(422,'请选择 1 分钟及以上的支持周期')
                item['timeframe']=timeframe
            if rule is not None:
                if options['engine']=='quant':
                    if rule not in STRATEGY_INDICATORS:raise HTTPException(422,'请选择已接入的量化策略')
                    options.update(strategy=rule,indicators=STRATEGY_INDICATORS[rule][:])
                else:
                    if rule not in {i['id'] for i in catalogue(service.registry) if i.get('kind')=='event'}:raise HTTPException(422,'请选择已接入的买卖信号指标')
                    options.update(indicators=[rule],combination='all')
            job={**job,'request':options}
            refresh=True
        if job.get("portfolio"):
            if item.get("account_entry_time") is None:
                raise HTTPException(422,"这条信号未在账户中成交，请查看账户执行状态")
            item.update(entry_time=item.get("account_entry_time"),entry_price=item.get("account_entry_price"),
                exit_time=item.get("account_exit_time"),exit_price=item.get("account_exit_price"),status=item.get("account_status"),net_return_pct=item.get("account_return_pct"))
        interval = TIMEFRAME_MS[item["timeframe"]]
        day = 86400000
        signal_day = (item["signal_time"] + 8*3600000)//day*day - 8*3600000
        days = {"1m":1,"3m":1,"5m":1,"15m":3,"30m":7,"1h":14,"2h":30,"4h":60,"6h":90,"8h":120,"12h":180,"1d":365,"3d":730,"1w":1095,"1M":3650}.get(item["timeframe"],7)
        start = max(0, signal_day - (days-1)*day)
        end = min(signal_day+day-1, int(time.time()*1000) if live and refresh else job["completed_at"])
        if live and refresh:end=min(end,timestamp(job['request']['end']))
        if item.get("exit_time"):
            end = min(max(end,shift(floor_bar(item["exit_time"],item["timeframe"]),item["timeframe"])-1),job["completed_at"])
        if job["request"].get("purpose") != "signals":
            end = min(end,timestamp(job["request"]["end"]))
        if count_bars(start,end,item["timeframe"]) > 200000:
            raise HTTPException(422, "持仓图表超过 20 万根 K 线，请缩短研究日期后查看，交易明细仍保留。")
        curves_selected=False
        read_start=max(0,shift(floor_bar(start,item['timeframe']),item['timeframe'],-2020)) if (live and refresh) or curves_selected else start
        bars = service.store.get_candles(item["symbol"], item["timeframe"], start=read_start, end=end, limit=200000)
        merged = {c.open_time:c for c in bars}
        for meta in service.research.datasets():
            if (meta.get("symbol"),meta.get("timeframe")) == (item["symbol"],item["timeframe"]):
                imported, _ = service.research.load(meta["id"])
                for c in imported:
                    if read_start <= c.open_time <= end:
                        merged.setdefault(c.open_time,c)
        complete_bars=[c for c in sorted(merged.values(),key=lambda c:c.open_time) if c.closed and c.close_time<=end]
        candles = [c.to_dict() for c in complete_bars if c.open_time>=start]
        note = "收盘确认，下一根开盘模拟成交；退出由反向策略条件触发。" if job["request"]["engine"] == "quant" else "信号在收盘后确认；模拟入场使用下一根开盘。图上退出为固定观察期结束，不是指标卖点。"
        if job.get("portfolio"):
            note += " 图上成交来自共享账户模拟；未成交的信号不会显示虚构买卖点。"
            if str(item.get("account_status", "")).startswith("风险强平"):
                note = "本笔由 OHLC 近似风险强平退出，时间为发现触及阈值的 K 线收盘（或跳空开盘）；不是交易所真实强平记录。"
        if NAN_ID in job['request']['indicators']:note='楠策略原代码相反转折平仓；信号价与模拟成交价分开显示。'
        markers = []
        def mark(r, time, value, action, label):
            if time is not None and value is not None and start <= time <= end:
                score_time=r.get('exit_signal_time') if label=='模拟退出' else r.get('signal_time')
                markers.append(dict(time=time,price=value,action=action,label=label,score_time=score_time,selected=r.get("id")==item.get("id")))
        if live:
            note = "收盘确认信号，未下单。仅显示本地已有的完整 K 线；缺失部分不会补造。"
            chart_rows=job.get('items',[])
            if job['request']['engine']=='quant':chart_rows=display_signal_rows(chart_rows,job['request'].get('strategy'))
            if refresh:
                from .policy import migrate_saved_settings
                settings=type(get_settings())(**migrate_saved_settings(job.get('policy_snapshot') or get_settings().model_dump()))
                events=signal_stream(complete_bars,settings,job.get('custom_indicator_snapshot',[]),job['request'])
                chart_rows=signal_rows(events,job['request'].get('strategy') if job['request']['engine']=='quant' else None)
            thresholds=job['request'].get('rule_settings',{}).get(job['request']['indicators'][0],{})
            if thresholds.get('min_activity',0)>0 or thresholds.get('min_actionability',0)>0:
                from .rule_library import passes_scores
                config=json.dumps(job.get('policy_snapshot') or get_settings().model_dump())
                chart_rows=[r for r in chart_rows if str(r.get('action','')).startswith('close_') or passes_scores(historical_scores(service.store,r['symbol'],r['timeframe'],r['signal_time'],r['direction'],config,int(time.time()//60),service.research),thresholds)]
            for r in chart_rows:
                if (r["symbol"],r["timeframe"]) != (item["symbol"],item["timeframe"]):
                    continue
                kind = r.get("signal_kind","")
                action = "close_long" if "平多" in kind else "close_short" if "平空" in kind else "buy" if r["direction"] == "long" else "sell"
                if job["request"]["engine"] != "quant" and NAN_ID not in job["request"]["indicators"] and action.startswith("close"):
                    continue
                mark(r,r["signal_time"],r["signal_price"],action,kind+' · '+r.get('action_basis','历史记录，需重新运行核对委托规则')+' · 非成交记录')
            if not any(m['selected'] for m in markers):
                exact=next((m for m in markers if m['time']==item['signal_time']),None)
                if exact:exact['selected']=True
                else:
                    anchor=next((c for c in complete_bars if c.open_time<=item['signal_time']<=c.close_time),None)
                    if anchor:markers.append(dict(time=item['signal_time'],price=anchor.close,action='record',label='原记录定位 · 当前规则或周期在此时没有买卖信号',score_time=anchor.close_time,selected=True))
        else:
            mark(item,item.get("entry_time"),item.get("entry_price"),"buy" if item.get("direction", "long") == "long" else "sell","模拟入场")
            if job["request"]["engine"] == "quant" or NAN_ID in job["request"]["indicators"]:
                mark(item,item.get("exit_time"),item.get("exit_price"),"close_long" if item.get("direction", "long") == "long" else "close_short","模拟退出")
        overlays=rule_overlays(complete_bars,start,end,job['request'].get('indicators',[]))
        return {"overlays":overlays,"tick_size":chart_tick_size(item["symbol"]),"row":item,"candles":candles,"note":note,"markers":markers,"live":live,"start":start,"end":end}
