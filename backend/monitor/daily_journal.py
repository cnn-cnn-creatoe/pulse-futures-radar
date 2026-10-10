"""Daily evidence archives and a deliberately small, causal selection replay."""
from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timezone, timedelta
import csv
import hashlib
import io
from itertools import combinations, chain
import json
import logging
from pathlib import Path
import threading
import time
import uuid
from typing import Literal

from fastapi import HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .backtest import _causal_anomaly_prefilter
from .custom_indicators import evaluate_indicator
from .periods import shift
from .engine import LOOKBACK, TIMEFRAME_MS, analyze
from .history_data import validate_series
from .indicators import evaluate_candidate
from .policy import Settings
from .store import DAY_MS, alert_direction
from .retention import RETENTION_DAYS

TZ = timezone(timedelta(hours=8))
BUILTINS = {"wt_extreme_cross": "WaveTrend · 超买超卖交叉", "nan_strategy1_oversold": "楠策略1超买超卖"}
LOG = logging.getLogger(__name__)


def day_start(day: str) -> int:
    try:
        value = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=TZ)
        if value.strftime("%Y-%m-%d") != day:
            raise ValueError()
        return int(value.timestamp() * 1000)
    except (ValueError, TypeError):
        raise ValueError("日期格式应为 YYYY-MM-DD")


def local_time(value) -> str:
    return datetime.fromtimestamp(value / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S") if value is not None else ""


def confirmations(signal: dict) -> dict:
    return {item["id"]: item.get("passed") for item in
            (signal.get("available_confirmations") or signal.get("confirmations") or [])}


def record_custom(signal, candles, specs):
    """Record independently; these research rules never alter live qualification."""
    results = {}
    for spec in specs:
        result = evaluate_indicator(spec, candles)
        direction = alert_direction(signal)
        results[spec["id"]] = {**result, "name": spec["name"],
            "passed": result.get(direction, False) if result["ready"] and result["signal"] != "conflict" else None,
            "spec_hash": hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()[:16]}
    return {**signal, "custom_indicator_results": results, "custom_indicator_snapshot": specs}


def csv_safe(value):
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return "" if value is None else value


def archive_row(signal):
    passes = confirmations(signal)
    names = list(BUILTINS)
    row = {"日期": local_time(signal["close_time"])[:10], "出现时间_北京时间": local_time(signal["close_time"]),
        "记录ID": signal.get("id"), "交易对": signal["symbol"], "周期": signal["timeframe"],
        "记录来源": signal.get("record_origin", "实时两K异常"), "信号性质": signal.get("signal_kind", "异常候选"),
        "方向": {"long": "多", "short": "空"}.get(alert_direction(signal), "中性"),
        "形态": signal.get("pattern"), "价格": signal.get("price"),
        "活跃度": signal.get("activity_score"), "活跃度可用满分": signal.get("score_available_max"),
        "异常倍数": signal.get("volatility_ratio"), "两根涨跌%": signal.get("change_pct"),
        "相对成交额": signal.get("relative_volume"), "成交额": signal.get("quote_volume"),
        "持仓变化%": signal.get("oi_change_pct"), "买卖价差bps": signal.get("spread_bps"),
        "空策略": "不适用（独立指标入选）" if signal.get("record_origin") else "通过",
        "原强筛结果": "未评估" if signal.get("record_origin") else "通过" if signal.get("qualified") else "未通过"}
    for size in range(1, 4):
        for ids in combinations(names, size):
            values = [passes.get(key) for key in ids]
            row[" + ".join(BUILTINS[key] for key in ids)] = (
                "未记录" if any(value is None for value in values) else "通过" if all(values) else "未通过")
    row.update({"指标逐项结果JSON": signal.get("available_confirmations") or signal.get("confirmations"),
        "自定义指标结果JSON": signal.get("custom_indicator_results"),
        "自定义指标代码快照JSON": signal.get("custom_indicator_snapshot"),
        "监测参数快照JSON": signal.get("policy_snapshot"), "参数版本": signal.get("policy_hash"),
        "分项得分JSON": signal.get("score_breakdown"), "研究设置JSON":signal.get("research_settings"), "完整记录JSON": signal})
    return {key: csv_safe(value) for key, value in row.items()}


class DailyArchives:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.last_error = ""
        self.stopping = asyncio.Event()
        self.task = None

    def listing(self):
        today = local_time(int(time.time() * 1000))[:10]
        oldest = local_time(int(time.time() * 1000) - RETENTION_DAYS * DAY_MS)[:10]
        rows = []
        days = {r["date"]:dict(r) for r in self.store.alert_days()}
        for extra in self.store.indicator_days():
            day = days.setdefault(extra["date"],dict(date=extra["date"],records=0,symbols=0))
            day["records"] += extra["records"]
            day["symbols"] = self.store.combined_day_symbols(day_start(extra["date"]))
        for day in sorted(days.values(),key=lambda r:r["date"],reverse=True):
            if day['date'] < oldest:
                continue
            path = self.root / f'{day["date"]}.csv'
            meta = self.root / f'{day["date"]}.meta.json'
            try:
                saved = json.loads(meta.read_text("utf-8")) if meta.exists() else {}
            except (ValueError, OSError):
                saved = {}
            rows.append({**day, "filename": path.name, "saved_records": saved.get("records", 0),
                "saved_at": saved.get("saved_at"), "current": day["date"] == today,
                "ready": path.exists() and saved.get("records") == day["records"] and saved.get("schema") == 2})
        return {"items": rows, "timezone": "Asia/Shanghai", "archive_error": self.last_error}

    def export(self, day):
        start = day_start(day)
        if start + DAY_MS <= int(time.time() * 1000) - RETENTION_DAYS * DAY_MS:
            raise ValueError("该日期已超过 30 天明细保留期，请下载历史行情进行回测。")
        with self._lock:
            path = self.root / f"{day}.csv"
            temp = path.with_suffix(".csv.tmp")
            count = 0
            writer = None
            try:
                with temp.open("w", encoding="utf-8-sig", newline="") as stream:
                    for signal in chain(self.store.iter_day_alerts(start, metadata=True), self.store.iter_indicator_day(start)):
                        row = archive_row(signal)
                        if writer is None:
                            writer = csv.DictWriter(stream, fieldnames=list(row), lineterminator="\n")
                            writer.writeheader()
                        writer.writerow(row)
                        count += 1
                    if writer is None:
                        stream.write("日期,出现时间_北京时间,记录ID,交易对,周期\n")
                temp.replace(path)
                meta = path.with_suffix(".meta.json")
                meta_temp = meta.with_suffix(".tmp")
                meta_temp.write_text(json.dumps({"schema": 2, "records": count, "saved_at": int(time.time() * 1000)}), "utf-8")
                meta_temp.replace(meta)
                return path
            finally:
                if temp.exists():
                    temp.unlink()

    def sync(self):
        try:
            self.prune_files()
            for day in self.listing()["items"]:
                if not day["ready"]:
                    self.export(day["date"])
            self.last_error = ""
        except Exception as error:
            self.last_error = str(error)[:300]
            LOG.exception("daily archive failed; database evidence remains intact")

    def prune_files(self):
        """Only remove dated generated CSVs and their metadata, never user datasets."""
        cutoff = int(time.time() * 1000) - RETENTION_DAYS * DAY_MS
        with self._lock:
            for path in self.root.glob('????-??-??.csv'):
                try:
                    expired = day_start(path.stem) + DAY_MS <= cutoff
                except ValueError:
                    continue
                if expired:
                    path.unlink()
                    path.with_suffix('.meta.json').unlink(missing_ok=True)

    async def run(self):
        while not self.stopping.is_set():
            await asyncio.to_thread(self.sync)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass

    def start(self):
        self.stopping.clear()
        self.task = asyncio.create_task(self.run())

    async def close(self):
        self.stopping.set()
        if self.task:
            await self.task


class ReplayRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    source: Literal["daily", "dataset"] = "daily"
    date: str | None = None
    dataset_id: str | None = Field(None, pattern=r"^[a-f0-9]{24}$")
    indicators: list[str] = Field(default_factory=list, max_length=12)
    combination: Literal["all", "any"] = "all"
    min_activity: float = Field(0, ge=0, le=100)
    holding_bars: int = Field(5, ge=1, le=100)
    fee_bps: float = Field(5, ge=0, le=100)
    slippage_bps: float = Field(3, ge=0, le=100)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source == "daily":
            day_start(self.date)
        elif not self.dataset_id:
            raise ValueError("请选择历史 K 线数据")
        if len(set(self.indicators)) != len(self.indicators):
            raise ValueError("不能重复选择指标")
        return self


def forward_outcome(signal, candles, options):
    """Each candidate is one independent observation, including pending outcomes."""
    close = signal["close_time"]
    interval = TIMEFRAME_MS[signal["timeframe"]]
    future = [c for c in candles if c.open_time > close][:options["holding_bars"]]
    result = {"id": signal.get("id"), "symbol": signal["symbol"], "timeframe": signal["timeframe"],
        "signal_time": close, "signal_price": signal.get("price"), "pattern": signal.get("pattern"),
        "direction": alert_direction(signal), "activity": signal.get("activity_score"),
        "volatility_ratio": signal.get("volatility_ratio"), "status": "等待后续K线",
        "entry_time": None, "entry_price": None, "exit_time": None, "exit_price": None,
        "net_return_pct": None, "price_change_pct": None, "best_move_pct": None, "worst_move_pct": None}
    if result["direction"] == "neutral":
        return {**result, "status": "无明确方向"}
    expected = close + 1
    for candle in future:
        if candle.open_time != expected or not candle.closed or candle.close_time != shift(candle.open_time,signal["timeframe"]) - 1:
            return {**result, "status": "后续K线有缺口"}
        expected = shift(expected,signal["timeframe"])
    if future:
        result.update(entry_time=future[0].open_time, entry_price=future[0].open)
    if len(future) < options["holding_bars"]:
        return result
    side = 1 if result["direction"] == "long" else -1
    slip, fee = options["slippage_bps"] / 10000, options["fee_bps"] / 10000
    entry = future[0].open * (1 + side * slip)
    exit_price = future[-1].close * (1 - side * slip)
    raw = (future[-1].close / future[0].open - 1) * 100
    net = (side * (exit_price - entry) / entry - fee * (entry + exit_price) / entry) * 100
    favorable = max(c.high for c in future) if side == 1 else min(c.low for c in future)
    adverse = min(c.low for c in future) if side == 1 else max(c.high for c in future)
    return {**result, "status": "已完成", "entry_price": entry, "exit_price": exit_price,
        "exit_time": future[-1].close_time, "price_change_pct": raw, "net_return_pct": net,
        "best_move_pct": max(0, side * (favorable - entry) / entry * 100),
        "worst_move_pct": min(0, side * (adverse - entry) / entry * 100)}


def rule_pass(signal, prefix, ids, combination, specs, *, saved=False, settings=None):
    builtins = [key for key in ids if key in BUILTINS]
    results = confirmations(signal)
    if builtins and not saved:
        evaluated = evaluate_candidate(signal, prefix, settings.indicator_config(), settings.quality.model_dump())
        results = confirmations(evaluated)
    for spec in specs:
        result = evaluate_indicator(spec, prefix)
        results[spec["id"]] = (result.get(alert_direction(signal), False)
            if result["ready"] and result["signal"] != "conflict" else None)
    selected = {key: results.get(key) for key in ids}
    passed = not ids or (all(v is True for v in selected.values()) if combination == "all"
                         else any(v is True for v in selected.values()))
    return passed, selected


class SimpleReplay:
    def __init__(self, store, research, registry, root):
        self.store, self.research, self.registry = store, research, registry
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.jobs = {}
        self.tasks = set()

    def start(self, request, settings):
        if any(job["state"] == "running" for job in self.jobs.values()):
            raise ValueError("已有回测运行中，请等待完成")
        specs = []
        for key in request.indicators:
            if key in BUILTINS:
                continue
            spec = self.registry.get(key)
            if not spec or spec.get("status") != "validated":
                raise ValueError(f"指标 {key} 未校验或不存在，请先在监测规则中校验并保存")
            specs.append(spec)
        if request.source == "dataset":
            if not any(d["id"] == request.dataset_id for d in self.research.datasets()):
                raise ValueError("历史数据不存在")
        ident = uuid.uuid4().hex
        job = {"id": ident, "state": "running", "processed": 0, "total": 0,
            "created_at": int(time.time() * 1000), "request": request.model_dump(),
            "policy_snapshot": settings.model_dump(), "custom_indicator_snapshot": specs,
            "indicator_names": [BUILTINS.get(key) or next(s["name"] for s in specs if s["id"] == key)
                                for key in request.indicators]}
        self.jobs[ident] = job
        task = asyncio.create_task(self.execute(job, settings.model_copy(deep=True), specs))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return self.summary(job)

    @staticmethod
    def summary(job):
        return {key: value for key, value in job.items() if key != "items"}

    def get(self, ident):
        if len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident):
            return None
        if ident not in self.jobs:
            path = self.root / f"{ident}.json"
            if path.exists():
                self.jobs[ident] = json.loads(path.read_text("utf-8"))
        return self.jobs.get(ident)

    async def execute(self, job, settings, specs):
        try:
            await asyncio.to_thread(self.work, job, settings, specs)
        except Exception as error:
            LOG.exception("selection replay failed")
            job.update(state="failed", message=str(error)[:300])
        self.research._atomic_write(self.root / f'{job["id"]}.json', json.dumps(job, ensure_ascii=False, allow_nan=False))

    def work(self, job, settings, specs):
        options = job["request"]
        selected = []
        missing = 0
        observed = 0

        def accept(signal, prefix, future, saved):
            nonlocal missing, observed
            observed += 1
            if signal.get("activity_score", 0) < options["min_activity"]:
                return
            passed, values = rule_pass(signal, prefix, options["indicators"], options["combination"],
                                       specs, saved=saved, settings=settings)
            if not passed:
                missing += any(value is None for value in values.values())
                return
            row = forward_outcome(signal, future, options)
            row["indicator_results"] = values
            selected.append(row)

        if options["source"] == "daily":
            start = day_start(options["date"])
            groups = defaultdict(list)
            for signal in self.store.iter_day_alerts(start):
                groups[(signal["symbol"], signal["timeframe"])].append(signal)
            job["total"] = sum(len(rows) for rows in groups.values())
            for (symbol, tf), signals in groups.items():
                interval = TIMEFRAME_MS[tf]
                candles = self.store.get_candles(symbol, tf, limit=6000,
                    start=start - 7 * DAY_MS - 3 * interval,
                    end=min(start + DAY_MS + options["holding_bars"] * interval, int(time.time() * 1000)))
                for signal in sorted(signals, key=lambda s: s["close_time"]):
                    prefix = [c for c in candles if c.close_time <= signal["close_time"]][-2020:]
                    if specs and (not prefix or prefix[-1].close_time != signal["close_time"]):
                        prefix = []
                    accept(signal, prefix, candles, True)
                    job["processed"] += 1
            job["basis"] = "当天已保存的全部异常；内置指标用当时快照，自定义指标用本次代码在该时刻之前的K线上计算。"
        else:
            candles, meta = self.research.load(options["dataset_id"])
            if meta.get("timeframe") not in ("5m", "15m"):
                raise ValueError("仅支持5m和15m历史数据")
            if validate_series(candles)["gaps"]:
                raise ValueError("历史K线有缺口，请先补齐或换一份数据")
            timeframe = candles[0].timeframe
            warmup = LOOKBACK[timeframe]["minimum_bars"] + 2
            if len(candles) <= warmup:
                raise ValueError(f"历史太短：至少需要 {warmup + 1} 根K线才能生成候选")
            config = settings.engine()
            prefilter = _causal_anomaly_prefilter(candles, config, warmup)
            window = LOOKBACK[timeframe]["days"] * DAY_MS // TIMEFRAME_MS[timeframe] + 3
            job["total"] = len(candles) - warmup
            job["dataset"] = meta
            for index in range(warmup, len(candles)):
                job["processed"] += 1
                if index != len(candles) - 1 and prefilter.get(index) != "analyze":
                    continue
                prefix = candles[max(0, index - window + 1):index + 1]
                signal = analyze(prefix, config).get("signal")
                if signal:
                    accept(signal, prefix, candles[index + 1:index + options["holding_bars"] + 1], False)
            job["basis"] = "历史K线逐根重放，使用本次指标及监测参数；仅用出现时刻之前的数据计算。K线不含同期持仓量和盘口，活跃度缺失项不补造。"
        selected.sort(key=lambda row: (row["signal_time"], row["symbol"], row["timeframe"]))
        completed = [row for row in selected if row["net_return_pct"] is not None]
        job.update(items=selected,
            metrics={"candidates": observed, "selected": len(selected), "completed": len(completed),
                "pending": len(selected) - len(completed), "missing_indicator": missing,
                "win_rate": sum(row["net_return_pct"] > 0 for row in completed) / len(completed) if completed else None,
                "average_return_pct": sum(row["net_return_pct"] for row in completed) / len(completed) if completed else None})
        # This ledger is the result: no hidden truncation, every selected symbol is exported.
        headers = {"symbol": "交易对", "timeframe": "周期", "signal_time": "出现时间_北京时间",
            "direction": "方向", "signal_price": "出现时价格", "activity": "活跃度", "volatility_ratio": "异常倍数",
            "entry_time": "入场时间_北京时间", "entry_price": "入场价_含滑点",
            "exit_time": "观察结束_北京时间", "exit_price": "结束价_含滑点", "price_change_pct": "价格涨跌%",
            "net_return_pct": "按方向扣费后收益%", "best_move_pct": "期间最大有利变动%",
            "worst_move_pct": "期间最大不利变动%", "status": "结果状态", "indicator_results": "指标结果JSON"}
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(buffer, fieldnames=[*headers.values(), "本次策略", "本次设置JSON", "自定义代码JSON", "监测参数JSON"])
        writer.writeheader()
        for row in selected:
            exported = {label: csv_safe(local_time(row[key]) if key.endswith("_time") else row.get(key)) for key, label in headers.items()}
            exported.update({"本次策略": " + ".join(job["indicator_names"]) or "空策略",
                "本次设置JSON": json.dumps(options, ensure_ascii=False), "自定义代码JSON": json.dumps(specs, ensure_ascii=False),
                "监测参数JSON": json.dumps(settings.model_dump(), ensure_ascii=False)})
            writer.writerow(exported)
        self.research._atomic_write(self.root / f'{job["id"]}.csv', "\ufeff" + buffer.getvalue())
        job.update(state="completed", completed_at=int(time.time() * 1000))

    async def close(self):
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


def install_daily_routes(app, store, archives, replay, get_settings, require_dashboard):
    @app.get("/api/daily-records")
    def daily_records(date: str, symbol: str | None = None, after: int = Query(0, ge=0),
                      limit: int = Query(2000, ge=1, le=5000)):
        try:
            start = day_start(date)
        except ValueError as error:
            raise HTTPException(400, str(error))
        return {**store.day_alert_page(start, symbol=symbol, after=after, limit=limit), "date": date}

    @app.get("/api/daily-archives")
    def daily_archives():
        return archives.listing()

    @app.get("/api/daily-archives/{date}.csv")
    def daily_csv(date: str):
        try:
            day_start(date)
            path = archives.export(date)
            return FileResponse(path, media_type="text/csv; charset=utf-8", filename=path.name)
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.post("/api/replays")
    async def start_replay(body: ReplayRequest, request: Request):
        require_dashboard(request)
        try:
            return replay.start(body, get_settings())
        except ValueError as error:
            raise HTTPException(400, str(error))

    @app.get("/api/replays/{ident}")
    def replay_result(ident: str, offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        job = replay.get(ident)
        if not job:
            raise HTTPException(404, "回测记录不存在")
        items = job.get("items", [])
        return {**replay.summary(job), "items": items[offset:offset + limit], "total_items": len(items)}

    @app.get("/api/replays/{ident}/results.csv")
    def replay_csv(ident: str):
        job = replay.get(ident)
        if not job or job["state"] != "completed":
            raise HTTPException(404, "回测尚未完成")
        return FileResponse(replay.root / f"{ident}.csv", media_type="text/csv; charset=utf-8", filename=f"replay-{ident}.csv")
