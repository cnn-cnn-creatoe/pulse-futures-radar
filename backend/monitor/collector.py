"""Read-only Binance USD-M market collector. No keys or order side effects.

Official interface references (checked 2026-08-31):
https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
"""
from __future__ import annotations

import asyncio
import bisect
import inspect
import json
import logging
import math
import random
import time
import uuid
from collections import deque
from dataclasses import asdict
from email.utils import parsedate_to_datetime
from typing import Any, Callable

import aiohttp

from .engine import (
    Candle,
    EngineConfig,
    LOOKBACK,
    MONITOR_TIMEFRAMES,
    TIMEFRAME_MS,
    aggregate_minutes,
    analyze,
)
from .store import Store, contract_metadata_fields

LOG = logging.getLogger(__name__)
REST_BASE = "https://fapi.binance.com"
WS_BASE = "wss://fstream.binance.com/market/stream"
MINUTE = 60_000
DAY = 86_400_000
KLINE_SYMBOLS_PER_SHARD = 40
GAP_WORKERS = 8
OI_WORKERS = 8
OI_CYCLE_SECONDS = 60
CLOSED_QUEUE_CAPACITY = 20_000
ANALYSIS_QUEUE_CAPACITY = 5_000
SCAN_WORKERS = 2
CANDIDATE_QUEUE_CAPACITY = 20_000
CANDIDATE_WORKERS = 2
BOOK_SNAPSHOT_TTL_SECONDS = 5
BOOK_SNAPSHOT_RETRY_SECONDS = 1
RECOVERY_CONFIRM_WAIT_MAX_SECONDS = 10
RECOVERY_DATA_RETRY_SECONDS = 30
INDICATOR_RULES_NOT_CONFIGURED = "indicator_rules_not_configured"
CandidateTransform = Callable[[dict, list[Candle], dict], Any]
NotificationSink = Callable[[str, dict], Any]
CandidateJob = tuple[str, str, int, dict, CandidateTransform | None]
AnalysisJob = CandidateJob


def completion_percentage(completed: int, total: int) -> float:
    """Do not round a nearly complete queue to 100% before its last item."""
    return 100.0 if total > 0 and completed >= total else min(99.9, round(100 * completed / max(1, total), 1))


class MarketAccessError(RuntimeError):
    """An official endpoint denies access; never try alternate endpoints to bypass it."""


def eligible_contract(meta: dict, futures_type: str | None = None) -> bool:
    """All USDT-quoted trading UM contracts, without asset-class or tenor limits."""
    return (meta.get("status") == "TRADING" and meta.get("quoteAsset") == "USDT"
            and eligible_um_contract(meta, futures_type))


def eligible_um_contract(meta: dict, futures_type: str | None = None) -> bool:
    """Prove UM membership from metadata, not quote/margin assets or hostname.

    The migrated public routes can also expose CM. Explicit st=2 or a non-UM
    family is always rejected; quote=BTC is not a CM heuristic (e.g. ETHBTC).
    """
    if not isinstance(meta.get("symbol"), str) or not meta["symbol"]:
        return False
    symbol_type = meta.get("st")
    if isinstance(symbol_type, bool) or (symbol_type is not None and symbol_type not in (1, "1")):
        return False
    family = meta.get("futuresType") or futures_type
    if family is not None:
        return family == "U_MARGINED"
    return symbol_type in (1, "1")


def eligible_crypto_perpetual(meta: dict, futures_type: str | None = None) -> bool:
    """Legacy scope alias; now every USDT-quoted UM asset class and tenor."""
    return meta.get("quoteAsset") == "USDT" and eligible_um_contract(meta, futures_type)


def rest_candle(symbol: str, timeframe: str, row: list, server_now: int) -> Candle:
    return Candle(symbol=symbol, timeframe=timeframe, open_time=int(row[0]),
                  close_time=int(row[6]), open=float(row[1]), high=float(row[2]),
                  low=float(row[3]), close=float(row[4]), volume=float(row[5]),
                  quote_volume=float(row[7]), trades=int(row[8]),
                  taker_buy_quote=float(row[10]), closed=int(row[6]) < server_now)


def retry_after_seconds(value: str | None, default: float = 60) -> float:
    if not value:
        return default
    try:
        return max(1, float(value))
    except ValueError:
        try:
            return max(1, parsedate_to_datetime(value).timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return default


class WeightBudget:
    """Conservative shared REST budget with spacing and exchange cooldown."""
    def __init__(self, per_minute: int = 1800):
        self.per_minute = per_minute
        self.used_header = 0
        self.cooldown_until = 0.0
        self._next = 0.0
        self._lock = asyncio.Lock()
        self._history: deque[tuple[float, int]] = deque()

    async def acquire(self, weight: int) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._history and self._history[0][0] < now - 60:
                    self._history.popleft()
                wait = max(self._next, self.cooldown_until) - now
                if sum(w for _, w in self._history) + weight > self.per_minute:
                    wait = max(wait, self._history[0][0] + 60 - now)
                if wait <= 0:
                    break
                await asyncio.sleep(wait)
            self._history.append((now, weight))
            self._next = now + 60 * weight / self.per_minute


class MonitorService:
    def __init__(self, store: Store, config: EngineConfig | None = None):
        self.store = store
        self.config = config or EngineConfig()
        self.candidate_transform: CandidateTransform | None = None
        # Optional non-blocking live-event sink.  The collector never reads a
        # notification history or owns credentials; failures cannot roll back
        # persisted market evidence.
        self.notification_sink: NotificationSink | None = None
        self.notification_enabled: Callable[[], bool] | None = None
        self.universe: list[dict] = []
        self._metadata: dict[str, dict] = {}
        self._session: aiohttp.ClientSession | None = None
        self._runner: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        self._shards: list[asyncio.Task] = []
        self._running = False
        self._state = "stopped"
        self._message = "实时采集尚未启动；通知推送按独立设置运行"
        self._clock_offset = 0
        self._clock_rtt_ms = 0
        self._clock_synced_at = 0
        self._clock_sample: dict = {}
        self._refresh_requested = asyncio.Event()
        self._lifecycle_lock = asyncio.Lock()
        self._watermark = 0
        self._symbol_gate: dict[str, int] = {}
        self._warm_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._warm_pending: set[tuple[str, str]] = set()
        self._pair_state: dict[tuple[str, str], dict] = {}
        self._download_jobs: dict[tuple[str, str], dict] = {}
        self._download_events: deque[dict] = deque(maxlen=500)
        self._download_session_id = uuid.uuid4().hex
        self._download_cursor = 0
        self._live_klines: dict[str, dict] = {}
        self._closed_queue: asyncio.Queue[Candle] = asyncio.Queue(maxsize=CLOSED_QUEUE_CAPACITY)
        self._analysis_queue: asyncio.Queue[AnalysisJob] = asyncio.Queue(maxsize=ANALYSIS_QUEUE_CAPACITY)
        self._candidate_queue: asyncio.Queue[CandidateJob | tuple[str, str, int, dict]] = asyncio.Queue(
            maxsize=CANDIDATE_QUEUE_CAPACITY)
        self._gap_queue: asyncio.Queue[str] = asyncio.Queue()
        self._gap_pending: set[str] = set()
        self._gap_start: dict[str, int] = {}
        self._reconnect_fence: set[str] = set()
        self._last_minute: dict[str, int] = {}
        # Receipt progress is deliberately separate from durable progress.  It
        # keeps the health loop from mistaking a short persistence backlog for
        # a market-data gap; only _last_minute is evidence that a bar was saved.
        self._last_received_minute: dict[str, int] = {}
        self._symbol_heartbeat: dict[str, int] = {}
        self._symbol_shard: dict[str, str] = {}
        self._symbol_locks: dict[str, asyncio.Lock] = {}
        self._ticker: dict[str, dict] = {}
        self._mark: dict[str, tuple[float, int]] = {}
        self._ranks: dict[str, float] = {}
        self._rank_refreshed_at = 0.0
        self._rank_groups: dict[str, dict] = {}
        self._book_cache: dict[str, tuple[float, float, int]] = {}
        self._book_snapshot_lock = asyncio.Lock()
        self._book_snapshot_at = 0.0
        self._book_snapshot_attempt_at = 0.0
        self._book_snapshot_observed_at = 0
        self._book_snapshot_refreshes = 0
        self._book_snapshot_symbols = 0
        self._book_snapshot_invalid = 0
        self._connections: dict[str, dict] = {}
        self._connection_delays: dict[str, deque[int]] = {}
        self._errors: deque[dict] = deque(maxlen=16)
        self._budget = WeightBudget()
        self._last_event = 0
        self._last_closed = 0
        self._last_refresh = 0
        self._excluded = 0
        self._exclusion_reasons: dict[str, int] = {}
        self._unknown_underlying = 0
        self._history_jobs_done = 0
        self._oi_last_cycle = 0
        self._oi_cycle_seconds: float | None = None
        self._oi_cycle_started_at = 0
        self._oi_inflight = 0
        self._oi_cycle_samples = 0
        self._received_closed = 0
        self._analysis_dropped = 0
        self._analysis_expired = 0
        # One skipped scan can satisfy more than one safety condition (for
        # example, a proven gap also advances the symbol gate). Keep those
        # conditions separate from the cumulative job count so production
        # telemetry can distinguish deadline pressure from correct fail-closed
        # suppression without changing the 90-second policy.
        self._analysis_expired_conditions = {
            "deadline": 0, "gap_pending": 0, "symbol_gate": 0,
            "reconnect_fence": 0, "watermark": 0,
        }
        self._candidate_dropped = 0
        self._signal_count = 0
        self._qualified_count = 0
        self._listing_count = 0

    def now(self) -> int:
        return int(time.time() * 1000) + self._clock_offset

    def _spawn(self, coro: Any, name: str) -> asyncio.Task:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _error(self, context: str, exc: Exception | str) -> None:
        message = str(exc)[:300] or type(exc).__name__
        self._errors.append({"time": self.now(), "context": context, "message": message})
        LOG.warning("%s: %s", context, message)

    async def _notify(self, kind: str, event: dict) -> None:
        sink = self.notification_sink
        if sink is None:
            return
        try:
            result = sink(kind, dict(event))
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not pass a third-party exception string into logs: transport
            # errors can contain request URLs and therefore credentials.
            self._error("通知事件排队", type(error).__name__)

    def telegram_enabled(self) -> bool:
        callback = self.notification_enabled
        if callback is None:
            return False
        try:
            return bool(callback())
        except Exception:
            return False

    def _download_event(self, kind: str, symbol: str | None = None, timeframe: str | None = None, **details) -> None:
        self._download_cursor += 1
        self._download_events.append({"seq": self._download_cursor, "session_id": self._download_session_id,
            "time": self.now(), "type": kind, "symbol": symbol, "timeframe": timeframe, **details})

    def _download_update(self, symbol: str, timeframe: str, **changes) -> dict:
        pair = (symbol, timeframe)
        previous = self._download_jobs.get(pair, {})
        row = {"symbol": symbol, "timeframe": timeframe, "state": "queued", "stage": "等待历史下载",
               "bars": 0, "minimum": LOOKBACK[timeframe]["minimum_bars"] + 3,
               "target_bars": LOOKBACK[timeframe]["days"] * DAY // TIMEFRAME_MS[timeframe] + 3,
               "cached_bars": 0, "downloaded_bars": 0, "pages": 0, "progress_pct": None,
               "requested_start": None, "requested_end": None, "first_open_time": None,
               "last_close_time": None, "last_page_at": None, "latest_candle": None,
               "download_complete": False, "request_complete": False, "window_complete": False,
               "window_missing_bars": None, "confirmed_unavailable_prefix_bars": 0,
               "error": None, **previous, **changes}
        if row["requested_start"] is None or row["requested_end"] is None:
            # Startup inventory is all retained data, not yet a verified current
            # download window. Keep cached counts visible without a false 100%.
            row.update(progress_pct=None, window_complete=False, window_missing_bars=None, download_complete=False)
        else:
            target = row["target_bars"]
            # A newly listed contract can have no eligible closed candle in a
            # timeframe yet. That exact empty interval is a fully verified
            # download window, while its monitor state remains insufficient.
            # Other empty responses and ordinary missing bars stay incomplete.
            empty_complete = (target == 0 and row["requested_start"] == row["requested_end"] + 1
                              and row["bars"] == 0 and row["first_open_time"] is None
                              and row["last_close_time"] is None)
            onboard = int(self._metadata.get(symbol, {}).get("onboardDate") or 0)
            interval = TIMEFRAME_MS[timeframe]
            confirmed_prefix = int(row.get("confirmed_unavailable_prefix_bars") or 0)
            prefix_complete = (confirmed_prefix == 1 and row["requested_start"] == onboard
                               and row["first_open_time"] == row["requested_start"] + interval
                               and row["last_close_time"] == row["requested_end"]
                               and row["bars"] + confirmed_prefix == target)
            complete = (empty_complete or
                        prefix_complete or
                        (target > 0 and row["bars"] == target
                         and row["first_open_time"] == row["requested_start"]
                         and row["last_close_time"] == row["requested_end"]))
            row["window_complete"] = complete
            covered = row["bars"] + (confirmed_prefix if prefix_complete else 0)
            row["window_missing_bars"] = max(0, target - covered)
            # Rounding 4322/4323 to one decimal must not display 100% with a gap.
            row["progress_pct"] = 100.0 if complete else min(99.9, round(100 * row["bars"] / max(1, target), 1))
            row["download_complete"] = bool(row["request_complete"] and complete)
        self._download_jobs[pair] = row  # Replace snapshots rather than mutate objects served to API callers.
        return row

    def _future_slack_ms(self) -> int:
        # Small network/clock uncertainty is not stale data. Never accept an
        # unbounded future timestamp, even after a high-latency time request.
        return min(5000, max(2500, self._clock_rtt_ms // 2 + 1000))

    def _timestamp_fresh(self, timestamp: int | None, max_age: int = 90_000) -> bool:
        return timestamp is not None and -self._future_slack_ms() <= self.now() - timestamp < max_age

    def _record_connection_event(self, name: str, event_time: int, received: int) -> None:
        """Measure content delay independently of transport/last-message liveness."""
        connection = self._connections.setdefault(name, {})
        delay = received - event_time
        connection.update(last_message=received, last_event_time=event_time, event_delay_ms=delay)
        self._connection_delays.setdefault(name, deque(maxlen=256)).append(delay)

    def _connection_snapshot(self, name: str, connection: dict) -> dict:
        delays = sorted(self._connection_delays.get(name, ()))
        timestamp = connection.get("last_event_time")
        return {"name": name, **connection,
                "content_fresh": connection.get("state") == "connected" and self._timestamp_fresh(timestamp),
                "event_age_ms": self.now() - timestamp if timestamp is not None else None,
                "latency_samples": len(delays), "latency_window": 256,
                "latency_p50_ms": delays[(len(delays) - 1) // 2] if delays else None,
                "latency_p95_ms": delays[math.ceil(len(delays) * .95) - 1] if delays else None,
                "latency_max_ms": delays[-1] if delays else None}

    def _confirmed_history_now(self) -> int:
        # Midpoint synchronization has up to half an RTT of uncertainty. Use
        # its conservative lower bound for REST candle closure, never the
        # relaxed quote freshness tolerance. WebSocket closure still needs x=true.
        safety = self._clock_rtt_ms // 2 + 1000 if self._clock_synced_at else 0
        return self.now() - safety

    async def _sleep_or_refresh(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._refresh_requested.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        self._refresh_requested.clear()

    async def _sync_clock(self) -> None:
        sent = int(time.time() * 1000)
        began = time.monotonic()
        clock = await self._request("/fapi/v1/time")
        elapsed = round((time.monotonic() - began) * 1000)
        sample = self._clock_sample
        # _request measures only the actual HTTP attempt, excluding shared rate
        # budget wait/retry sleeps. The fallback supports deterministic test I/O.
        if sample.get("began", 0) >= began and sample.get("server_time") == int(clock["serverTime"]):
            midpoint, elapsed = sample["midpoint"], sample["rtt_ms"]
        else:
            midpoint = sent + elapsed / 2
        self._clock_offset = round(int(clock["serverTime"]) - midpoint)
        self._clock_rtt_ms = elapsed
        self._clock_synced_at = self.now()

    async def start(self) -> None:
        if self._running:
            return
        # A pause/resume is a fresh live session. Rebuild from persisted history
        # and never retain abandoned warmup jobs or emit queued old candidates.
        self._metadata.clear()
        self.universe = []
        self._pair_state.clear()
        self._download_session_id = uuid.uuid4().hex
        self._download_cursor = 0
        self._download_events.clear()
        self._download_jobs.clear()
        self._live_klines.clear()
        self._refresh_requested.clear()
        self._warm_pending.clear()
        self._warm_queue = asyncio.Queue()
        self._closed_queue = asyncio.Queue(maxsize=CLOSED_QUEUE_CAPACITY)
        self._analysis_queue = asyncio.Queue(maxsize=ANALYSIS_QUEUE_CAPACITY)
        self._candidate_queue = asyncio.Queue(maxsize=CANDIDATE_QUEUE_CAPACITY)
        self._gap_queue = asyncio.Queue()
        self._gap_pending.clear()
        self._gap_start.clear()
        self._reconnect_fence.clear()
        self._last_minute.clear()
        self._last_received_minute.clear()
        self._symbol_heartbeat.clear()
        self._symbol_shard.clear()
        self._symbol_gate.clear()
        self._connections.clear()
        self._connection_delays.clear()
        self._ticker.clear()
        self._ranks.clear()
        self._rank_refreshed_at = 0.0
        self._rank_groups.clear()
        self._mark.clear()
        self._book_cache.clear()
        self._book_snapshot_lock = asyncio.Lock()
        self._book_snapshot_at = 0.0
        self._book_snapshot_attempt_at = 0.0
        self._book_snapshot_observed_at = 0
        self._book_snapshot_refreshes = 0
        self._book_snapshot_symbols = 0
        self._book_snapshot_invalid = 0
        self._last_event = self._last_closed = self._last_refresh = 0
        self._received_closed = self._signal_count = self._history_jobs_done = 0
        self._analysis_dropped = 0
        self._analysis_expired = 0
        self._analysis_expired_conditions = {
            "deadline": 0, "gap_pending": 0, "symbol_gate": 0,
            "reconnect_fence": 0, "watermark": 0,
        }
        self._candidate_dropped = 0
        self._qualified_count = self._listing_count = 0
        self._excluded = self._unknown_underlying = 0
        self._exclusion_reasons.clear()
        self._oi_last_cycle = 0
        self._oi_cycle_seconds = None
        self._oi_cycle_started_at = 0
        self._oi_inflight = 0
        self._oi_cycle_samples = 0
        self._running = True
        self._state = "connecting"
        self._message = "正在连接币安公开行情，确认可监测的合约范围"
        self._watermark = self.now()
        # Display saved inventory immediately; warm workers later verify the
        # precise current lookback and request only its missing ranges.
        inventory = await asyncio.to_thread(self.store.candle_inventory)
        for row in inventory:
            if row["timeframe"] in MONITOR_TIMEFRAMES:
                self._download_update(row["symbol"], row["timeframe"], **{
                    key: value for key, value in row.items() if key not in {"symbol", "timeframe"}},
                    cached_bars=row["bars"], stage="已恢复本地库存，等待官方范围确认")
        self._download_event("session_started", count=sum(row["bars"] for row in inventory),
                             message="恢复本地库存；已保存历史不会重新清空下载")
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15), trust_env=True)
        self._runner = asyncio.create_task(self._run(), name="market-supervisor")

    async def stop(self) -> None:
        self._running = False
        tasks = list(self._tasks)
        if self._runner:
            tasks.append(self._runner)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._shards.clear()
        if self._session:
            await self._session.close()
            self._session = None
        self._state = "stopped"
        self._message = "实时采集已停止；历史信号仍可查看"

    async def retry_connection(self) -> dict:
        """Resume active work idempotently; only a stopped/dead runner restarts."""
        async with self._lifecycle_lock:
            if self._running and self._runner and not self._runner.done():
                requeued = 0
                # Read the effective snapshot so a base ``ready`` row that has
                # aged into dynamic ``stale`` is repaired immediately too.
                for (symbol, timeframe), state in self._effective_pair_states().items():
                    job = self._download_jobs.get((symbol, timeframe), {})
                    incomplete_window = job.get("request_complete") and not job.get("window_complete")
                    if (symbol in self._metadata and symbol not in self._gap_pending
                            and (symbol, timeframe) not in self._warm_pending
                            and (state.get("state") in {"error", "stale", "gap"} or incomplete_window)):
                        self._queue_warm(symbol, timeframe)
                        requeued += 1
                # Wake a sleeping failed supervisor without abandoning active
                # downloads, WebSockets, queued candidates or their watermark.
                if self._state in {"blocked", "error"}:
                    self._refresh_requested.set()
                self._download_event("resume_requested", count=requeued,
                    message="继续当前下载与连接；未重置进度，已重新排队失败任务")
                return {**self.status(), "resume_action": "continued", "requeued_pairs": requeued}
            await self.stop()
            await self.start()
            return {**self.status(), "resume_action": "started", "requeued_pairs": 0}

    async def _request(self, path: str, params: dict | None = None, weight: int = 1) -> Any:
        if not self._session:
            raise RuntimeError("Market service is not started")
        for attempt in range(5):
            await self._budget.acquire(weight)
            try:
                sent_at, began = int(time.time() * 1000), time.monotonic()
                async with self._session.get(REST_BASE + path, params=params) as response:
                    used = response.headers.get("X-MBX-USED-WEIGHT-1M")
                    if used:
                        self._budget.used_header = int(used)
                    if response.status in (403, 451):
                        raise MarketAccessError(f"币安公开行情访问被拒绝（HTTP {response.status}）；请在可合法访问币安的网络或服务器运行")
                    if response.status in (418, 429):
                        seconds = retry_after_seconds(response.headers.get("Retry-After"))
                        self._budget.cooldown_until = time.monotonic() + seconds
                        self._error("REST限速", f"HTTP {response.status}，按 Retry-After 等待 {seconds:.0f} 秒")
                        continue
                    if response.status >= 500:
                        raise aiohttp.ClientResponseError(response.request_info, response.history,
                                                          status=response.status, message="upstream unavailable")
                    response.raise_for_status()
                    data = await response.json()
                    if isinstance(data, dict) and isinstance(data.get("code"), int) and data["code"] < 0:
                        raise ValueError(f"Binance {data['code']}: {data.get('msg', '')}")
                    if path == "/fapi/v1/time" and "serverTime" in data:
                        elapsed = round((time.monotonic() - began) * 1000)
                        self._clock_sample = {"began": began, "server_time": int(data["serverTime"]),
                                              "midpoint": sent_at + elapsed / 2, "rtt_ms": elapsed}
                    return data
            except MarketAccessError:
                raise
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                if isinstance(exc, aiohttp.ClientResponseError) and 400 <= exc.status < 500:
                    raise
                if attempt == 4:
                    raise
                await asyncio.sleep(min(30, 2**attempt) + random.random())
        raise RuntimeError("币安接口持续限速，请等待冷却后重试")

    async def _run(self) -> None:
        launched = False
        while self._running:
            try:
                await self._sync_clock()
                if not launched:
                    self._watermark = self.now()
                await self._refresh_universe()
                if not launched:
                    for worker in range(6):
                        self._spawn(self._warm_worker(), f"history-{worker}")
                    for worker in range(3):
                        self._spawn(self._candle_worker(), f"candles-{worker}")
                    for worker in range(SCAN_WORKERS):
                        self._spawn(self._analysis_worker(), f"analysis-{worker}")
                    for worker in range(CANDIDATE_WORKERS):
                        self._spawn(self._candidate_worker(), f"signals-{worker}")
                    for worker in range(GAP_WORKERS):
                        self._spawn(self._recover_worker(), f"gap-recovery-{worker}")
                    self._spawn(self._oi_poller(), "open-interest")
                    self._spawn(self._health_loop(), "market-health")
                    launched = True
                self._state = "warming"
                self._message = "公开行情已连接，正在分批加载 5m / 15m 历史基准"
                await self._sleep_or_refresh(60)
            except asyncio.CancelledError:
                raise
            except MarketAccessError as exc:
                self._state = "blocked"
                self._message = str(exc)
                self._error("公开行情访问", exc)
                await self._sleep_or_refresh(600)
            except Exception as exc:
                self._state = "error"
                self._message = "币安行情暂不可用；自动重试中，不会用演示数据替代实时行情"
                self._error("行情初始化", exc)
                await self._sleep_or_refresh(30)

    async def _refresh_universe(self) -> None:
        info = await self._request("/fapi/v1/exchangeInfo")
        for rule in info.get("rateLimits", []):
            if rule.get("rateLimitType") == "REQUEST_WEIGHT" and rule.get("interval") == "MINUTE":
                self._budget.per_minute = max(60, int(rule["limit"] * 0.75))
        old = set(self._metadata)
        all_symbols = [{**m, "futuresType": m.get("futuresType") or info.get("futuresType")}
                       for m in info.get("symbols", [])]
        selected = [m for m in all_symbols if eligible_contract(m)]
        snapshot = [m for m in all_symbols if eligible_um_contract(m) and m.get("quoteAsset") == "USDT"
                    and m.get("status") in {"TRADING", "PENDING_TRADING"}]
        if not snapshot:
            raise ValueError("exchangeInfo 未返回已核验的 U_MARGINED / st=1 且USDT计价合约；不能只凭接口域名推断UM范围")
        self._excluded = len(all_symbols) - len(selected)
        self._unknown_underlying = sum(m.get("underlyingType") not in {
            "COIN", "INDEX", "STOCK", "EQUITY", "COMMODITY", "CN_EQUITY", "HK_EQUITY", "KR_EQUITY", "PREMARKET"}
            for m in selected)
        self._exclusion_reasons = {
            "not_trading": sum(m.get("status") != "TRADING" for m in all_symbols),
            "non_um_or_unverified_market": sum(not eligible_um_contract(m) for m in all_symbols),
            "not_usdt_quote": sum(m.get("quoteAsset") != "USDT" for m in all_symbols),
        }
        now = self.now()
        events = await asyncio.to_thread(self.store.reconcile_universe, snapshot, now)
        self._listing_count += len(events)
        for event in events:
            await self._notify("listing", event)
        self._metadata = {m["symbol"]: m for m in selected}
        self.universe = [{"symbol": m["symbol"], **contract_metadata_fields(m), "status": m["status"],
                          "market_type": "UM", "tick_size": self._tick(m["symbol"])}
                         for m in selected]
        self._last_refresh = self.now()
        # Public ticker provides quote-volume order for a useful first warmup.
        if not self._ticker:
            try:
                tickers = await self._request("/fapi/v1/ticker/24hr", weight=40)
                self._consume_tickers(tickers, rest=True)
            except Exception as exc:
                self._error("初始成交额排名", exc)
        ordered = sorted(self._metadata, key=lambda s: (
            s not in {"BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"},
            -float(self._ticker.get(s, {}).get("quote_volume", 0)), s))
        for symbol in ordered:
            for timeframe in MONITOR_TIMEFRAMES:
                if symbol not in old:
                    self._queue_warm(symbol, timeframe)
        if old != set(self._metadata) or not self._shards:
            for task in self._shards:
                task.cancel()
            if self._shards:
                await asyncio.gather(*self._shards, return_exceptions=True)
            self._connections.clear()
            self._connection_delays.clear()
            # Freeze this assignment until the actual metadata symbol set
            # changes. Ticker rank updates must not churn live connections.
            shards = self._balanced_kline_shards()
            self._symbol_shard = {symbol: f"kline-{i}" for i, shard in enumerate(shards) for symbol in shard}
            # Both families share the same frozen symbol partition and lifecycle.
            # Subscribe only to verified USDT-quoted UM contracts, never merged
            # UM+CM arrays. Every asset class and delivery tenor is retained.
            self._shards = [task for i, shard in enumerate(shards) for task in (
                self._spawn(self._kline_stream(shard, i), f"kline-shard-{i}"),
                self._spawn(self._market_stream(shard, i), f"market-shard-{i}"))]
        for symbol in old - set(self._metadata):
            self._symbol_heartbeat.pop(symbol, None)
            self._reconnect_fence.discard(symbol)
            self._ticker.pop(symbol, None)
            self._mark.pop(symbol, None)
            self._live_klines.pop(symbol, None)
            for timeframe in MONITOR_TIMEFRAMES:
                self._pair_state.pop((symbol, timeframe), None)
        self._refresh_volume_ranks(force=True)

    def _balanced_kline_shards(self) -> list[list[str]]:
        """Spread the most active contracts across bounded, complete shards."""
        ordered = sorted(self._metadata, key=lambda symbol: (
            -float(self._ticker.get(symbol, {}).get("quote_volume", 0)), symbol))
        count = math.ceil(len(ordered) / KLINE_SYMBOLS_PER_SHARD)
        return [ordered[index::count] for index in range(count)] if count else []

    def _tick(self, symbol: str) -> float:
        for item in self._metadata.get(symbol, {}).get("filters", []):
            if item.get("filterType") == "PRICE_FILTER":
                return float(item.get("tickSize", 1e-8))
        return 1e-8

    def _queue_warm(self, symbol: str, timeframe: str) -> None:
        if timeframe not in MONITOR_TIMEFRAMES:
            return
        pair = (symbol, timeframe)
        if pair not in self._warm_pending:
            self._warm_pending.add(pair)
            known = self._pair_state.get(pair, self._download_jobs.get(pair, {}))
            self._pair_state[pair] = {**known, "state": "queued", "bars": known.get("bars", 0),
                                      "minimum": LOOKBACK[timeframe]["minimum_bars"] + 3}
            self._download_update(symbol, timeframe, state="queued", stage="等待历史下载工作线程",
                                  error=None, download_complete=False, request_complete=False,
                                  requested_start=None, requested_end=None,
                                  confirmed_unavailable_prefix_bars=0)
            self._warm_queue.put_nowait(pair)

    async def _warm_worker(self) -> None:
        while self._running:
            symbol, timeframe = await self._warm_queue.get()
            try:
                if eligible_contract(self._metadata.get(symbol, {})):
                    await self._warm_pair(symbol, timeframe)
                    if eligible_contract(self._metadata.get(symbol, {})):
                        self._history_jobs_done += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                previous = self._pair_state.get((symbol, timeframe), {})
                self._pair_state[(symbol, timeframe)] = {**previous, "state": "error", "message": str(exc)[:180]}
                self._download_update(symbol, timeframe, state="error", stage="下载中断；已写入部分保留，可继续缺失段",
                                      error=str(exc)[:180], download_complete=False)
                self._download_event("job_error", symbol, timeframe, message=str(exc)[:180])
                self._error(f"历史加载 {symbol} {timeframe}", exc)
            finally:
                self._warm_pending.discard((symbol, timeframe))
                self._warm_queue.task_done()

    async def _fetch_range(self, symbol: str, timeframe: str, start: int, end: int) -> list[Candle]:
        if not eligible_contract(self._metadata.get(symbol, {})):
            return []
        interval = TIMEFRAME_MS[timeframe]
        cursor = start
        collected: list[Candle] = []
        job = self._download_jobs.get((symbol, timeframe))
        if not job or job.get("requested_start") is None:
            self._download_update(symbol, timeframe, state="downloading", requested_start=start, requested_end=end,
                                  target_bars=max(0, (end - start) // interval + 1), stage="请求缺失历史 K 线")
        while cursor <= end and self._running:
            if not eligible_contract(self._metadata.get(symbol, {})):
                break
            limit = min(499, (end - cursor) // interval + 1)
            page_start = cursor
            self._download_update(symbol, timeframe, state="downloading", stage=f"请求一页历史，最多 {limit} 根",
                                  page_start=page_start, page_end=end, page_requested_at=self.now())
            try:
                rows = await self._request("/fapi/v1/klines", {
                    "symbol": symbol, "interval": timeframe, "startTime": cursor,
                    "endTime": end, "limit": limit,
                }, weight=1 if limit < 100 else 2)
                if not eligible_contract(self._metadata.get(symbol, {})):
                    break  # The user may have removed this quote scope during HTTP I/O.
                candles = [rest_candle(symbol, timeframe, row, self._confirmed_history_now()) for row in rows]
                closed = [c for c in candles if c.closed]
                for candle in closed:
                    if not self._valid_candle(candle):
                        raise ValueError(f"币安返回无效K线 {symbol} {timeframe} {candle.open_time}")
                await asyncio.to_thread(self.store.save_candles, closed)
                # Exact unique SQLite inventory is read AFTER the page commit.
                # Received/written rows are a separate count and can include upserts.
                job = self._download_jobs[(symbol, timeframe)]
                stats = await asyncio.to_thread(self.store.candle_window_stats, symbol, timeframe,
                    start=job.get("requested_start"), end=job.get("requested_end"))
                job = self._download_jobs[(symbol, timeframe)]
                page_at = self.now()
                self._download_update(symbol, timeframe,
                    **{key: value for key, value in stats.items() if key not in {"symbol", "timeframe"}},
                    downloaded_bars=job.get("downloaded_bars", 0) + len(closed), pages=job.get("pages", 0) + 1,
                    last_page_at=page_at, error=None, stage=f"已写入本页 {len(closed)} 根，继续补齐缺失区间")
                previous = self._pair_state.get((symbol, timeframe), {})
                self._pair_state[(symbol, timeframe)] = {**previous, "bars": stats["bars"],
                    "last_close": stats["last_close_time"], "minimum": LOOKBACK[timeframe]["minimum_bars"] + 3,
                    "state": previous.get("state", "warming")}
                self._download_event("page_saved" if rows else "page_empty", symbol, timeframe,
                    count=len(closed), received_rows=len(rows), requested_start=page_start, requested_end=end,
                    first_open_time=closed[0].open_time if closed else None,
                    last_close_time=closed[-1].close_time if closed else None,
                    latest_candle=closed[-1].to_dict() if closed else None, stored_bars=stats["bars"],
                    message="已提交数据库；接收条数可能包含重复覆盖" if rows else "接口未返回更多历史")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._download_update(symbol, timeframe, state="error", stage="本页失败，已保存历史保留", error=str(exc)[:180])
                self._download_event("page_error", symbol, timeframe, count=0,
                    requested_start=page_start, requested_end=end, message=str(exc)[:180])
                raise
            if not rows:
                break
            collected.extend(closed)
            next_cursor = int(rows[-1][0]) + interval
            if next_cursor <= cursor:
                raise ValueError("K线回补游标没有向前推进")
            cursor = next_cursor
            if len(rows) < limit:
                break
        return collected

    async def _warm_pair(self, symbol: str, timeframe: str) -> None:
        if not eligible_contract(self._metadata.get(symbol, {})):
            return
        interval = TIMEFRAME_MS[timeframe]
        target = LOOKBACK[timeframe]["days"] * DAY // interval + 3
        end = self._confirmed_history_now() // interval * interval - 1
        start = (end + 1) - target * interval
        onboard = int(self._metadata.get(symbol, {}).get("onboardDate", 0))
        start = max(start, onboard // interval * interval)
        pair = (symbol, timeframe)
        previous = self._pair_state.get(pair, {})
        self._pair_state[pair] = {**previous, "state": "warming", "minimum": LOOKBACK[timeframe]["minimum_bars"] + 3}
        existing = await asyncio.to_thread(self.store.get_candles, symbol, timeframe, target + 1, start=start, end=end)
        if not eligible_contract(self._metadata.get(symbol, {})):
            return
        self._pair_state[pair] = {**self._pair_state[pair], "bars": len(existing)}
        self._download_update(symbol, timeframe, state="downloading", stage="已核对本地缓存，只请求缺失区间",
            bars=len(existing), cached_bars=len(existing), requested_start=start, requested_end=end,
            target_bars=max(0, (end - start) // interval + 1), baseline_target_bars=target,
            first_open_time=existing[0].open_time if existing else None,
            last_close_time=existing[-1].close_time if existing else None,
            latest_candle=existing[-1].to_dict() if existing else None, error=None,
            download_complete=False, request_complete=False, confirmed_unavailable_prefix_bars=0)
        self._download_event("job_started", symbol, timeframe, count=len(existing),
            requested_start=start, requested_end=end, message="恢复已保存缓存，仅补缺失区间")
        # Fetch only missing ranges; historical writes never enter the signal queue.
        cursor = start
        requested_start_confirmed = False
        for candle in existing:
            if candle.open_time > cursor:
                range_start = cursor
                await self._fetch_range(symbol, timeframe, range_start, candle.open_time - 1)
                requested_start_confirmed = requested_start_confirmed or range_start == start
            cursor = max(cursor, candle.open_time + interval)
        if cursor <= end:
            range_start = cursor
            await self._fetch_range(symbol, timeframe, range_start, end)
            requested_start_confirmed = requested_start_confirmed or range_start == start
        if not eligible_contract(self._metadata.get(symbol, {})):
            return
        candles = await asyncio.to_thread(self.store.get_candles, symbol, timeframe, target)
        self._set_pair_state(symbol, timeframe, candles, force=True)
        state = self._pair_state[pair]["state"]
        # Recent N bars may contain older bars outside this target window, or
        # a just-closed live bar after its end. Their monitor count must never
        # replace the exact download-window inventory or conceal a missing bar.
        stats = await asyncio.to_thread(self.store.candle_window_stats, symbol, timeframe, start=start, end=end)
        confirmed_prefix = int(bool(requested_start_confirmed and start == onboard
            and stats["first_open_time"] == start + interval and stats["last_close_time"] == end
            and stats["bars"] + 1 == max(0, (end - start) // interval + 1)))
        row = self._download_update(symbol, timeframe,
            **{key: value for key, value in stats.items() if key not in {"symbol", "timeframe"}},
            request_complete=True, confirmed_unavailable_prefix_bars=confirmed_prefix)
        stage = ("历史窗口已核验；挂牌起点前缀 1 根无官方K线" if confirmed_prefix
                 else "目标窗口已核验；当前尚无可下载的已收盘K线" if row["target_bars"] == 0
                 else "历史窗口已补齐" if row["window_complete"]
                 else "本轮请求结束，但目标窗口仍有缺失，尚未下载齐全")
        self._download_update(symbol, timeframe, stage=stage)
        self._download_event("job_finished", symbol, timeframe, count=stats["bars"], state=state,
            window_complete=row["window_complete"], missing_bars=row["window_missing_bars"],
            message=stage + "；是否可评分另看周期就绪状态")
        if timeframe == "1m" and candles:
            self._last_minute[symbol] = max(self._last_minute.get(symbol, 0), candles[-1].open_time)

    def _set_pair_state(self, symbol: str, timeframe: str, candles: list[Candle], *, force: bool = False) -> None:
        minimum = LOOKBACK[timeframe]["minimum_bars"] + 3
        tail = 0
        expected = candles[-1].open_time if candles else 0
        for candle in reversed(candles):
            if candle.open_time != expected:
                break
            tail += 1
            expected -= TIMEFRAME_MS[timeframe]
        fresh = bool(candles) and self._timestamp_fresh(candles[-1].close_time, TIMEFRAME_MS[timeframe] + 90_000)
        state = "ready" if tail == len(candles) and tail >= minimum and fresh else ("gap" if tail < len(candles) else "insufficient_history" if tail < minimum else "stale")
        pair = (symbol, timeframe)
        previous = self._pair_state.get(pair, {})
        if not force and pair in self._warm_pending:
            # Live minute ticks / reconnect repair cannot declare an unfinished
            # whole-history download complete or overwrite it as insufficient.
            self._pair_state[pair] = {**previous, "state": previous.get("state", "queued"),
                "bars": max(previous.get("bars", 0), len(candles)), "minimum": minimum}
            return
        self._pair_state[(symbol, timeframe)] = {"state": state, "bars": len(candles), "consecutive_bars": tail,
                                                "minimum": minimum, "last_close": candles[-1].close_time if candles else None,
                                                "reason": self._history_reason(symbol, timeframe, state)}
        changes = {"state": state, "monitor_bars": len(candles),
                   "monitor_last_close_time": candles[-1].close_time if candles else None,
                   "reason": self._history_reason(symbol, timeframe, state)}
        if self._download_jobs.get(pair, {}).get("requested_start") is None:
            changes.update(bars=len(candles), first_open_time=candles[0].open_time if candles else None,
                           last_close_time=candles[-1].close_time if candles else None,
                           latest_candle=candles[-1].to_dict() if candles else None)
        self._download_update(symbol, timeframe, **changes)

    def _history_reason(self, symbol: str, timeframe: str, state: str) -> str | None:
        if state != "insufficient_history":
            return None if state == "ready" else state
        onboard = int(self._metadata.get(symbol, {}).get("onboardDate") or 0)
        needed = (LOOKBACK[timeframe]["minimum_bars"] + 3) * TIMEFRAME_MS[timeframe]
        return "new_insufficient" if onboard > 0 and self.now() - onboard < needed else "insufficient_history"

    async def _kline_stream(self, symbols: list[str], index: int) -> None:
        name = f"kline-{index}"
        streams = "/".join(f"{s.lower()}@kline_1m" for s in symbols)
        attempt = 0
        connected_before = False
        while self._running:
            self._connections[name] = {**self._connections.get(name, {}), "state": "connecting",
                                       "subscriptions": len(symbols), "last_message": None, "last_event_time": None}
            try:
                assert self._session
                async with self._session.ws_connect(WS_BASE + "?streams=" + streams, autoping=True,
                                                     heartbeat=60, receive_timeout=90, max_msg_size=2**22) as ws:
                    began = time.monotonic()
                    self._connections[name]["state"] = "connected"
                    self._connections[name]["connected_at"] = self.now()
                    self._download_event("ws_connected", message=f"{name} 已连接，订阅 {len(symbols)} 个币种")
                    if connected_before:
                        self._fence_reconnected_symbols(symbols)
                    connected_before = True
                    while self._running and time.monotonic() - began < 23 * 3600:
                        msg = await asyncio.wait_for(ws.receive(), timeout=90)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            event = payload.get("data", payload)
                            received = self.now()
                            self._connections[name]["last_message"] = received
                            if not event.get("E"):
                                continue  # A transport message is not evidence of fresh market content.
                            event_time = int(event["E"])
                            self._record_connection_event(name, event_time, received)
                            if received - event_time > 90_000 or event_time - received > 10_000:
                                raise TimeoutError("WebSocket 行情时间已过期或超前")
                            self._last_event = max(self._last_event, event_time)
                            attempt = 0
                            kline = event.get("k", {})
                            symbol = event.get("s")
                            if event.get("st") not in (None, 1, "1") or symbol not in self._metadata:
                                continue
                            candle = self._consume_live_kline(event, received)
                            if candle is None:
                                continue
                            try:
                                self._closed_queue.put_nowait(candle)
                            except asyncio.QueueFull:
                                self._error("收线队列", "处理积压，已安排REST补齐，不生成积压期间旧信号")
                                self._schedule_gap(symbol, candle.open_time)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError(f"WebSocket disconnected: type={msg.type.name}, code={ws.close_code}, "
                                                  f"data={str(msg.data)[:120]}, reason={str(msg.extra)[:120]}, exception={str(ws.exception())[:120]}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._connections[name].update(state="reconnecting", last_error=str(exc)[:300],
                    last_error_at=self.now(), reconnects=self._connections[name].get("reconnects", 0) + 1)
                self._download_event("ws_error", message=f"{name}: {str(exc)[:250]}")
                self._error(name, exc)
                attempt += 1
                await asyncio.sleep(min(60, 2 ** min(attempt, 6)) + random.random())

    def _consume_live_kline(self, event: dict, received: int | None = None) -> Candle | None:
        """Keep provisional quotes in memory only; return only closed candles."""
        symbol = event.get("s")
        if symbol not in self._metadata or event.get("st") not in (None, 1, "1"):
            return None
        received = self.now() if received is None else received
        if not event.get("E"):
            return None
        event_time = int(event["E"])
        if not self._timestamp_fresh(event_time):
            return None
        kline = event.get("k", {})
        if kline.get("i", "1m") != "1m":
            return None
        candle = Candle(symbol=symbol, timeframe="1m", open_time=int(kline["t"]),
            close_time=int(kline["T"]), open=float(kline["o"]), high=float(kline["h"]),
            low=float(kline["l"]), close=float(kline["c"]), volume=float(kline["v"]),
            quote_volume=float(kline["q"]), taker_buy_quote=float(kline["Q"]),
            trades=int(kline["n"]), closed=kline.get("x") is True)
        if not self._valid_candle(candle):
            raise ValueError(f"WebSocket K线字段无效: {symbol}")
        self._symbol_heartbeat[symbol] = max(event_time, self._symbol_heartbeat.get(symbol, 0))
        if candle.closed:
            # This is transport receipt only, not persistence proof.  Health
            # may use it to avoid false stale alarms while the bounded durable
            # queue drains; gap and signal gates continue to use saved state.
            self._last_received_minute[symbol] = max(
                candle.open_time, self._last_received_minute.get(symbol, 0))
            current = self._live_klines.get(symbol)
            if current and current["open_time"] <= candle.open_time:
                self._live_klines.pop(symbol, None)
            return candle
        current = self._live_klines.get(symbol)
        if not current or event_time >= current["event_time"]:
            self._live_klines[symbol] = {**candle.to_dict(), "closed": False, "source": "websocket",
                                       "event_time": event_time, "observed_at": received,
                                       "note": "当前1分钟未收盘行情，仅展示，不写入历史或用于信号"}
        return None

    async def _market_stream(self, symbols: list[str], index: int) -> None:
        name = f"market-{index}"
        subscribed = set(symbols)
        streams = "/".join(stream for symbol in symbols for stream in (
            f"{symbol.lower()}@miniTicker", f"{symbol.lower()}@markPrice"))
        attempt = 0
        while self._running:
            self._connections[name] = {**self._connections.get(name, {}), "state": "connecting",
                "subscriptions": len(symbols) * 2, "symbols_count": len(symbols),
                "last_message": None, "last_event_time": None}
            try:
                assert self._session
                # Official default mark-price cadence is 3s when @1s is omitted.
                async with self._session.ws_connect(WS_BASE + "?streams=" + streams, autoping=True,
                                                     heartbeat=60, receive_timeout=90, max_msg_size=2**22) as ws:
                    began = time.monotonic()
                    self._connections[name].update(state="connected", connected_at=self.now())
                    self._download_event("ws_connected", message=f"{name} 已连接，订阅 {len(symbols)} 个币种的成交额/标记价格")
                    while self._running and time.monotonic() - began < 23 * 3600:
                        msg = await asyncio.wait_for(ws.receive(), timeout=90)
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            payload = json.loads(msg.data)
                            event = payload.get("data", payload)
                            received = self.now()
                            self._connections[name]["last_message"] = received
                            if (not isinstance(event, dict) or event.get("s") not in subscribed
                                    or event.get("e") not in {"24hrMiniTicker", "markPriceUpdate"} or not event.get("E")):
                                continue
                            event_time = int(event["E"])
                            self._record_connection_event(name, event_time, received)
                            if received - event_time > 90_000 or event_time - received > 10_000:
                                raise TimeoutError("WebSocket 行情时间已过期或超前")
                            attempt = 0
                            self._consume_market_event(event)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                            raise ConnectionError(f"{name} WebSocket disconnected: type={msg.type.name}, code={ws.close_code}, "
                                f"data={str(msg.data)[:120]}, reason={str(msg.extra)[:120]}, exception={str(ws.exception())[:120]}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                prior = self._connections.get(name, {})
                self._connections[name] = {**prior, "state": "reconnecting",
                    "last_error": str(exc)[:300], "last_error_at": self.now(), "reconnects": prior.get("reconnects", 0) + 1}
                self._download_event("ws_error", message=f"{name}: {str(exc)[:250]}")
                self._error(name, exc)
                attempt += 1
                await asyncio.sleep(min(60, 2 ** min(attempt, 6)) + random.random())

    def _consume_market_event(self, event: dict) -> None:
        """Dispatch by the official event type; never infer types from a URL."""
        symbol = event.get("s")
        if symbol not in self._metadata or event.get("st") not in (None, 1, "1"):
            return
        event_time = event.get("E")
        if (not isinstance(event_time, int) or isinstance(event_time, bool)
                or not self._timestamp_fresh(event_time)):
            return
        if event.get("e") == "markPriceUpdate":
            price = float(event["p"])
            if not math.isfinite(price) or price <= 0:
                return
            current = self._mark.get(symbol)
            if current is None or event_time >= current[1]:
                self._mark[symbol] = (price, event_time)
        elif event.get("e") == "24hrMiniTicker":
            self._consume_tickers([event])

    def _consume_tickers(self, rows: list[dict], rest: bool = False) -> None:
        for row in rows:
            symbol = row.get("symbol" if rest else "s")
            if symbol not in self._metadata or row.get("st") not in (None, 1, "1"):
                continue
            event_time = self.now() if rest else row.get("E")
            if not rest and (not isinstance(event_time, int) or not self._timestamp_fresh(event_time)):
                continue
            if event_time < self._ticker.get(symbol, {}).get("time", 0):
                continue
            quote_volume = float(row.get("quoteVolume" if rest else "q", 0))
            price = float(row.get("lastPrice" if rest else "c", 0))
            if not all(math.isfinite(value) for value in (quote_volume, price)) or quote_volume < 0 or price <= 0:
                continue
            if not rest and row.get("e") == "24hrMiniTicker":
                opening = float(row.get("o", 0))
                if not math.isfinite(opening) or opening <= 0:
                    continue
                change_pct = (price / opening - 1) * 100
            else:
                change_pct = float(row.get("priceChangePercent" if rest else "P", 0))
            if not math.isfinite(change_pct):
                continue
            self._ticker[symbol] = {"quote_volume": quote_volume, "price": price,
                                    "change_pct": change_pct, "time": event_time}
        self._refresh_volume_ranks(force=rest)

    def _refresh_volume_ranks(self, force: bool = False) -> None:
        # Single-symbol events arrive independently; sorting the whole universe
        # on every event would reintroduce avoidable load in the receive loop.
        now = time.monotonic()
        if not force and now - self._rank_refreshed_at < 1:
            return
        groups: dict[str, list[str]] = {}
        for symbol, meta in self._metadata.items():
            groups.setdefault(meta.get("quoteAsset") or "UNKNOWN", []).append(symbol)
        self._ranks = {}
        self._rank_groups = {}
        for quote_asset, peers in groups.items():
            fresh = [symbol for symbol in peers if self._timestamp_fresh(self._ticker.get(symbol, {}).get("time"))]
            observed = sum(symbol in self._ticker for symbol in peers)
            ready = quote_asset != "UNKNOWN" and len(peers) > 1 and len(fresh) == len(peers)
            valid_until = min(self._ticker[symbol]["time"] + 90_000 for symbol in fresh) if ready else 0
            if ready:
                ordered = sorted(fresh, key=lambda symbol: self._ticker[symbol]["quote_volume"])
                self._ranks.update({symbol: i / (len(ordered) - 1) for i, symbol in enumerate(ordered)})
            reason = ("quote_asset_missing" if quote_asset == "UNKNOWN" else "insufficient_quote_peers" if len(peers) < 2
                      else "ready" if ready else "market_ticker_coverage_incomplete")
            self._rank_groups[quote_asset] = {"checked_at": self.now(), "fresh_symbols": len(fresh),
                "total_symbols": len(peers), "peer_count": len(peers), "quote_asset": quote_asset,
                "missing_symbols": len(peers) - observed, "stale_symbols": observed - len(fresh),
                "reason": reason, "valid_until": valid_until or None, "scope": "same_quote_asset"}
        self._rank_refreshed_at = now

    def _liquidity_rank_status(self, symbol: str | None = None) -> dict:
        def group_status(quote_asset: str) -> dict:
            result = dict(self._rank_groups.get(quote_asset, {"reason": "not_initialized",
                "quote_asset": quote_asset, "scope": "same_quote_asset", "peer_count": 0}))
            ready = bool(result.get("valid_until") and self.now() < result["valid_until"])
            if result.get("reason") == "ready" and not ready:
                result["reason"] = "market_ticker_expired"
            message = ("同报价币合约成交额均新鲜，分位可用；不是全市场跨币种排名" if ready
                       else "同报价币仅1个合约，不赋流动性分位" if result["reason"] == "insufficient_quote_peers"
                       else "同报价币成交额覆盖不足或过期，暂不计流动性分位")
            return {**result, "ready": ready, "message": message}

        if symbol is not None:
            return group_status(self._metadata.get(symbol, {}).get("quoteAsset") or "UNKNOWN")
        groups = [group_status(quote_asset) for quote_asset in sorted(self._rank_groups)]
        return {"scope": "same_quote_asset", "groups": groups,
                "ready": bool(groups) and all(group["ready"] for group in groups),
                "ready_groups": sum(group["ready"] for group in groups), "total_groups": len(groups),
                "message": "流动性分位按同报价币分组，原始成交额不跨报价币混排"}

    async def _candle_worker(self) -> None:
        while self._running:
            candle = await self._closed_queue.get()
            try:
                async with self._symbol_locks.setdefault(candle.symbol, asyncio.Lock()):
                    await self._on_closed(candle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error(f"K线处理 {candle.symbol}", exc)
                self._schedule_gap(candle.symbol, candle.open_time)
            finally:
                self._closed_queue.task_done()

    async def _on_closed(self, candle: Candle) -> None:
        if candle.symbol not in self._metadata or candle.closed is not True:
            return
        if not self._valid_candle(candle):
            raise ValueError("K线价格或成交额无效")
        if candle.close_time != candle.open_time + MINUTE - 1 or candle.open_time % MINUTE:
            raise ValueError("1m K线没有对齐UTC时间边界")
        if candle.close_time > self.now() + 10_000:
            raise ValueError("K线收盘时间超前")
        last = self._last_minute.get(candle.symbol)
        if last is not None and candle.open_time <= last:
            # A reconnect can deliver a genuinely missing final minute after a
            # newer minute has already become durable.  Do not mistake that
            # late close for an ordinary duplicate: save the missing WebSocket
            # row, but keep the symbol gated until native 5m/15m REST repair
            # verifies every formal bucket it may have completed.  This exact
            # lookup runs only on reordered/duplicate closes, never on the
            # normal all-market ingest path.
            existing = await asyncio.to_thread(
                self.store.get_candles, candle.symbol, "1m", 1,
                start=candle.open_time, end=candle.open_time)
            if existing:
                return
            self._schedule_gap(candle.symbol, candle.open_time)
        if last is not None and candle.open_time > last + MINUTE:
            self._schedule_gap(candle.symbol, last + MINUTE)
        reconnect_contiguous = (candle.symbol in self._reconnect_fence
                                and (last is None or candle.open_time == last + MINUTE))
        await asyncio.to_thread(self.store.save_candles, [candle])
        self._last_minute[candle.symbol] = max(self._last_minute.get(candle.symbol, 0), candle.open_time)
        if reconnect_contiguous:
            # The first new close after reconnect is contiguous and durable;
            # release the temporary signal/live-coverage fence without REST.
            self._reconnect_fence.discard(candle.symbol)
        self._last_closed = max(self._last_closed, candle.close_time)
        self._received_closed += 1
        # Verify every formal period due at this minute before allowing any of
        # them into signal analysis.  On a fresh process ``_last_minute`` has no
        # predecessor, so a WebSocket connection that starts after the first
        # minute of a bucket cannot prove that prefix gap from raw-watermark
        # continuity alone.  A partial aggregate at the boundary is that proof:
        # gate the whole symbol and repair the native 5m/15m candle via REST.
        # Never fetch or fabricate the missing raw 1m component.
        formal_closes: list[tuple[str, Candle]] = []
        missing_formal_start: int | None = None
        for timeframe in MONITOR_TIMEFRAMES:
            interval = TIMEFRAME_MS[timeframe]
            if (candle.close_time + 1) % interval:
                continue
            if timeframe == "15m":
                # Build 15m from its three verified 5m children.  This matters
                # on the first 15m boundary after a restart: native warmup has
                # already saved the pre-restart 5m prefix, while raw 1m is
                # intentionally not backfilled. Requiring all fifteen raw rows
                # would therefore mark the whole market as gapped even though
                # the complete formal children are present.
                children = await asyncio.to_thread(
                    self.store.get_candles, candle.symbol, "5m", 3,
                    end=candle.open_time)
                closed = self._aggregate_closed_children(children, timeframe)
            else:
                minutes = await asyncio.to_thread(
                    self.store.get_candles, candle.symbol, "1m", interval // MINUTE,
                    end=candle.open_time)
                aggregated = aggregate_minutes(minutes, timeframe)
                closed = aggregated[-1] if aggregated else None
            # Every child must be present and valid; partial UTC buckets never
            # close and still enter native-period REST recovery.
            if closed is None or closed.close_time != candle.close_time:
                bucket_start = candle.close_time + 1 - interval
                missing_formal_start = (bucket_start if missing_formal_start is None
                                        else min(missing_formal_start, bucket_start))
                continue
            # Save a smaller completed period before a larger one reads it in
            # this same boundary turn. SQLite upsert keeps this idempotent.
            await asyncio.to_thread(self.store.save_candles, [closed])
            formal_closes.append((timeframe, closed))

        if missing_formal_start is not None:
            # Symbol-level deduplication, bounded workers and the shared
            # REST weight budget prevent a whole-market boundary from becoming
            # an unbounded retry storm.  _recover_worker only requests formal
            # MONITOR_TIMEFRAMES and retains this gate until data is confirmed.
            self._schedule_gap(candle.symbol, missing_formal_start)

        for timeframe, closed in formal_closes:
            if (candle.symbol, timeframe) in self._warm_pending:
                # New closed bars and complete aggregates are already saved.
                # A warmup-owned pair cannot score yet, so do not repeatedly
                # deserialize thousands of baseline bars while it downloads.
                continue
            # Advancing a verified contiguous tail is O(1).  Full history reads
            # and CPU analysis belong to the separate bounded signal workers;
            # otherwise all contracts block durable minute persistence at each
            # shared 5m/15m boundary.
            if not self._advance_pair_state(candle.symbol, timeframe, closed):
                continue
            if not self._can_alert(candle.symbol, candle.close_time):
                continue
            enrichment = self._base_enrichment(candle.symbol)
            accepted_config = EngineConfig(**asdict(self.config))
            # API replaces this callable with a closure over immutable Settings.
            # Capture it alongside the base config before analysis can yield;
            # a later settings save must not change an already accepted close.
            accepted_transform = self.candidate_transform
            # Retain compact references, not thousands of historical objects,
            # while a whole-market boundary is being persisted.
            enrichment["quote_asset"] = self._metadata[candle.symbol]["quoteAsset"]
            enrichment["engine_config_at_close"] = asdict(accepted_config)
            try:
                self._analysis_queue.put_nowait(
                    (candle.symbol, timeframe, candle.close_time, enrichment, accepted_transform))
            except asyncio.QueueFull:
                # Never block or roll back a durable candle because optional
                # analysis is saturated.  The loss is explicit in telemetry;
                # no REST gap is invented because the market data was saved.
                self._analysis_dropped += 1
                if self._analysis_dropped == 1 or self._analysis_dropped % 100 == 0:
                    self._error("信号分析队列", f"已跳过 {self._analysis_dropped} 个过期分析任务；K线已安全写入")

    def _advance_pair_state(self, symbol: str, timeframe: str, candle: Candle) -> bool:
        """Advance an already verified live tail without rereading its baseline.

        This never promotes an unverified pair.  A non-contiguous higher-period
        close is requeued for official repair and cannot enter signal analysis.
        """
        pair = (symbol, timeframe)
        if timeframe not in MONITOR_TIMEFRAMES or pair in self._warm_pending:
            return False
        previous = self._pair_state.get(pair)
        if not previous:
            return False
        interval = TIMEFRAME_MS[timeframe]
        target = LOOKBACK[timeframe]["days"] * DAY // interval + 3
        prior_close = int(previous.get("last_close") or 0)
        if previous.get("state") != "ready":
            if candle.close_time > prior_close:
                self._pair_state[pair] = {**previous,
                    "bars": min(target, int(previous.get("bars") or 0) + 1),
                    "consecutive_bars": min(target, int(previous.get("consecutive_bars") or 0) + 1),
                    "last_close": candle.close_time}
            return False
        if not prior_close or candle.close_time > prior_close + interval:
            self._pair_state[pair] = {**previous, "state": "gap", "reason": "gap",
                "last_close": max(prior_close, candle.close_time)}
            self._queue_warm(symbol, timeframe)
            return False
        if candle.close_time < prior_close:
            return False
        if candle.close_time > prior_close:
            bars = min(target, int(previous.get("bars") or 0) + 1)
            consecutive = min(target, int(previous.get("consecutive_bars") or previous.get("bars") or 0) + 1)
            self._pair_state[pair] = {**previous, "state": "ready", "bars": bars,
                "consecutive_bars": consecutive, "last_close": candle.close_time, "reason": None}
            self._download_update(symbol, timeframe, state="ready", monitor_bars=bars,
                monitor_last_close_time=candle.close_time, reason=None)
        return True

    def _can_alert(self, symbol: str, close_time: int) -> bool:
        return (close_time > max(self._watermark, self._symbol_gate.get(symbol, 0))
                and 0 <= self.now() - close_time < 90_000 and symbol not in self._gap_pending
                and symbol not in self._reconnect_fence)

    def _analysis_expiry_conditions(self, symbol: str, close_time: int) -> tuple[str, ...]:
        """Return every active fail-closed condition for one scan job.

        Conditions intentionally overlap. A gap also installs a symbol gate,
        while a sufficiently slow gap repair can additionally cross the
        deadline. Reporting each condition is what lets operators tell whether
        more scan capacity could help; the decision itself remains `_can_alert`.
        """
        age = self.now() - close_time
        conditions = []
        if not 0 <= age < 90_000:
            conditions.append("deadline")
        if symbol in self._gap_pending:
            conditions.append("gap_pending")
        if close_time <= self._symbol_gate.get(symbol, 0):
            conditions.append("symbol_gate")
        if symbol in self._reconnect_fence:
            conditions.append("reconnect_fence")
        if close_time <= self._watermark:
            conditions.append("watermark")
        return tuple(conditions)

    def _record_analysis_expired(self, symbol: str, close_time: int) -> None:
        self._analysis_expired += 1
        for condition in self._analysis_expiry_conditions(symbol, close_time):
            self._analysis_expired_conditions[condition] += 1

    @staticmethod
    def _valid_candle(candle: Candle) -> bool:
        interval = TIMEFRAME_MS.get(candle.timeframe, 0)
        prices = (candle.open, candle.high, candle.low, candle.close)
        return (bool(interval) and candle.open_time % interval == 0
                and candle.close_time == candle.open_time + interval - 1
                and all(math.isfinite(p) and p > 0 for p in prices)
                and candle.low <= min(candle.open, candle.close) <= max(candle.open, candle.close) <= candle.high
                and all(math.isfinite(v) and v >= 0 for v in (candle.volume, candle.quote_volume, candle.taker_buy_quote))
                and 0 <= candle.taker_buy_quote <= candle.quote_volume and candle.trades >= 0)

    @classmethod
    def _aggregate_closed_children(cls, candles: list[Candle], timeframe: str) -> Candle | None:
        """Aggregate one exact UTC bucket from a smaller formal period.

        Unlike raw-minute aggregation this is intentionally a single-bucket
        live helper. Missing, duplicate, mixed, misaligned or invalid children
        fail closed so a real 5m omission propagates to native REST repair.
        """
        if not candles or timeframe not in TIMEFRAME_MS:
            return None
        child_timeframe = candles[0].timeframe
        child_interval = TIMEFRAME_MS.get(child_timeframe, 0)
        interval = TIMEFRAME_MS[timeframe]
        if (not child_interval or child_interval >= interval
                or interval % child_interval):
            return None
        members = sorted(candles, key=lambda item: item.open_time)
        count = interval // child_interval
        start = members[-1].open_time // interval * interval
        symbol = members[0].symbol
        if len(members) != count or not all(
                item.symbol == symbol
                and item.timeframe == child_timeframe
                and item.closed is True
                and item.open_time == start + index * child_interval
                and item.close_time == item.open_time + child_interval - 1
                and cls._valid_candle(item)
                for index, item in enumerate(members)):
            return None
        result = Candle(
            symbol=symbol, timeframe=timeframe, open_time=start,
            close_time=start + interval - 1,
            open=members[0].open,
            high=max(item.high for item in members),
            low=min(item.low for item in members),
            close=members[-1].close,
            quote_volume=sum(item.quote_volume for item in members),
            taker_buy_quote=sum(item.taker_buy_quote for item in members),
            trades=sum(item.trades for item in members),
            volume=sum(item.volume for item in members), closed=True)
        return result if cls._valid_candle(result) else None

    def _base_enrichment(self, symbol: str) -> dict:
        ticker = self._ticker.get(symbol, {})
        rank_status = self._liquidity_rank_status(symbol)
        percentile = self._ranks.get(symbol) if rank_status["ready"] and self._timestamp_fresh(ticker.get("time")) else None
        return {**contract_metadata_fields(self._metadata.get(symbol, {})), "market_type": "UM",
                "liquidity_percentile": percentile, "liquidity_rank_status": rank_status,
                "liquidity_rank_scope": "same_quote_asset", "liquidity_peer_count": rank_status.get("peer_count", 0),
                "liquidity_quote_asset": rank_status.get("quote_asset"), "spread_bps": None,
                "oi_change_pct": None, "oi_ratio": None}

    async def _analysis_worker(self) -> None:
        """Read the full baseline and prefilter anomalies off the close path."""
        while self._running:
            job = await self._analysis_queue.get()
            try:
                # Durable minute/aggregate writes always get first access to the
                # single SQLite connection during a whole-market close burst.
                await self._closed_queue.join()
                symbol, timeframe, close_time, enrichment = job[:4]
                if not eligible_contract(self._metadata.get(symbol, {})):
                    continue
                target = LOOKBACK[timeframe]["days"] * DAY // TIMEFRAME_MS[timeframe] + 3
                candles = await asyncio.to_thread(
                    self.store.get_candles, symbol, timeframe, target, end=close_time)
                if not candles or candles[-1].close_time != close_time:
                    raise ValueError("已接受的分析K线在历史存储中缺失")
                # A scan job is not yet an accepted anomaly.  It must still be
                # timely and gap-free when first evaluated; only an anomaly
                # promoted to the candidate queue keeps the delayed-audit
                # behavior during slower OI/indicator enrichment.
                if not self._can_alert(symbol, close_time):
                    self._record_analysis_expired(symbol, close_time)
                    continue
                accepted_config = (EngineConfig(**enrichment["engine_config_at_close"])
                                   if enrichment.get("engine_config_at_close") else self.config)
                result = await asyncio.to_thread(
                    analyze, candles, accepted_config, enrichment, self._tick(symbol))
                if result.get("status") == "gap":
                    # The anchored series itself proved a real historical gap.
                    # Repair that timeframe, but never invent a raw 1m gap just
                    # because a worker was slow.
                    current = self._pair_state.get((symbol, timeframe), {})
                    if int(current.get("last_close") or 0) <= close_time:
                        self._pair_state[(symbol, timeframe)] = {
                            **current, "state": "gap", "reason": "gap"}
                        self._queue_warm(symbol, timeframe)
                    continue
                if not result.get("signal"):
                    continue
                try:
                    self._candidate_queue.put_nowait(job)
                except asyncio.QueueFull:
                    self._candidate_dropped += 1
                    if self._candidate_dropped == 1 or self._candidate_dropped % 100 == 0:
                        self._error("候选信号队列",
                            f"已跳过 {self._candidate_dropped} 个过期候选；K线与分析结果不受影响")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error("异常预筛", exc)
            finally:
                self._analysis_queue.task_done()

    async def _candidate_worker(self) -> None:
        while self._running:
            job = await self._candidate_queue.get()
            try:
                await self._closed_queue.join()
                symbol, timeframe, close_time, enrichment = job[:4]
                if not eligible_contract(self._metadata.get(symbol, {})):
                    continue
                # Four-item jobs are the legacy/internal injection interface;
                # newly collected jobs always carry an explicit frozen callable
                # (including None), so they cannot inherit a later policy.
                candidate_transform = job[4] if len(job) == 5 else self.candidate_transform
                target = LOOKBACK[timeframe]["days"] * DAY // TIMEFRAME_MS[timeframe] + 3
                candles = await asyncio.to_thread(self.store.get_candles, symbol, timeframe, target, end=close_time)
                if not candles or candles[-1].close_time != close_time:
                    raise ValueError("已接受的候选K线在历史存储中缺失")
                latest = candles[-1]
                # Freshness/gap/watermark checks happened at live acceptance.
                # Slow enrichment must not silently discard an accepted signal.
                if close_time <= self._watermark:
                    continue
                enrichment.update(await asyncio.to_thread(self.oi_enrichment, latest.symbol, latest.timeframe, latest.close_time))
                try:
                    enrichment["spread_bps"] = await self._spread(latest.symbol)
                except Exception as exc:
                    self._error(f"价差 {latest.symbol}", exc)
                accepted_config = EngineConfig(**enrichment["engine_config_at_close"]) if enrichment.get("engine_config_at_close") else self.config
                result = await asyncio.to_thread(analyze, candles, accepted_config, enrichment, self._tick(latest.symbol))
                signal = result.get("signal")
                if signal:
                    signal.update({"source": "live", "data_source": "Binance USD-M public market",
                                   **contract_metadata_fields(enrichment), "market_type": "UM",
                                   "oi_sample_count": enrichment.get("oi_sample_count", 0),
                                   "liquidity_rank_status": enrichment.get("liquidity_rank_status"),
                                   "liquidity_rank_scope": enrichment.get("liquidity_rank_scope"),
                                   "liquidity_peer_count": enrichment.get("liquidity_peer_count"),
                                   "liquidity_quote_asset": enrichment.get("liquidity_quote_asset"),
                                   "processing_delay_ms": max(0, self.now() - close_time),
                                   "delayed": self.now() - close_time >= 90_000,
                                   "telegram_sent": False})
                    # Every base alert survives qualification, including rejects
                    # and indicator failures, so the filter remains auditable.
                    signal.update({"qualified": False, "quality_schema_version": 1,
                                   "rejection_reasons": [INDICATOR_RULES_NOT_CONFIGURED]})
                    if candidate_transform:
                        try:
                            transformed = await asyncio.to_thread(candidate_transform, dict(signal), candles, dict(enrichment))
                            if inspect.isawaitable(transformed):
                                transformed = await transformed
                            if not isinstance(transformed, dict):
                                raise TypeError("candidate_transform must return a signal dictionary")
                            signal.update(transformed)
                            # The placeholder describes the absence of an
                            # evaluator, not a policy rejection. Real policy
                            # evaluation deep-copies the base signal, so remove
                            # only that inherited marker after the callback has
                            # completed successfully. Strategy explanations stay
                            # in quality_reasons; any other machine rejection
                            # codes returned by the evaluator remain intact.
                            rejection_reasons = signal.get("rejection_reasons")
                            signal["rejection_reasons"] = [
                                reason for reason in rejection_reasons
                                if reason != INDICATOR_RULES_NOT_CONFIGURED
                            ] if isinstance(rejection_reasons, list) else []
                        except Exception as exc:
                            signal.update({"qualified": False, "rejection_reasons": ["indicator_evaluation_failed"],
                                           "qualification_error": str(exc)[:180]})
                            self._error(f"指标判断 {symbol} {timeframe}", exc)
                    # Recheck after potentially slow indicator work as well as
                    # network enrichment. Delayed raw anomalies remain immutable
                    # history, but are never new actionable trade candidates.
                    delay = max(0, self.now() - close_time)
                    signal.update({"processing_delay_ms": delay, "delayed": delay >= 90_000})
                    if signal["delayed"]:
                        warning = "候选处理延迟超过90秒，仅保留历史异常，不进入可操作信号"
                        signal.update({"qualified": False, "quality_schema_version": 2})
                        for field, value in (("quality_reasons", warning), ("warnings", warning),
                                             ("rejection_reasons", "delayed_candidate")):
                            values = signal.get(field)
                            signal[field] = list(values) if isinstance(values, list) else []
                            if value not in signal[field]:
                                signal[field].append(value)
                    # Transform callbacks cannot relabel the official source or
                    # rewrite the identity accepted at candle close.
                    signal.update({"symbol": symbol, "timeframe": timeframe, "close_time": close_time,
                                   "second_candle_close_time": close_time, "source": "live", "telegram_sent": False})
                    if await asyncio.to_thread(self.store.save_alert, signal):
                        signal.update(await asyncio.to_thread(
                            self.store.daily_counts_for_alert, signal))
                        self._signal_count += 1
                        self._qualified_count += self.store.is_trade_qualified(signal)
                        await self._notify("signal", signal)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._error("信号评分保存", exc)
            finally:
                self._candidate_queue.task_done()

    async def _spread(self, symbol: str) -> float:
        """Return a fresh spread from one shared official all-symbol snapshot.

        Binance charges weight 2 for one symbol and 5 when ``symbol`` is
        omitted.  A boundary burst can contain hundreds of candidates, so a
        singleflight bulk read is both faster and materially cheaper while the
        same per-row timestamp and quote validation remains fail-closed.
        """
        now = time.monotonic()
        cached = self._book_cache.get(symbol)
        if (cached and now - cached[0] < BOOK_SNAPSHOT_TTL_SECONDS
                and self._timestamp_fresh(cached[2])):
            return cached[1]
        # A recent successful snapshot that omitted/invalidated this symbol is
        # authoritative for its TTL; do not turn one bad row into N bulk calls.
        if now - self._book_snapshot_at < BOOK_SNAPSHOT_TTL_SECONDS:
            raise ValueError("共享买卖报价中缺少有效合约")
        await self._refresh_book_snapshot()
        cached = self._book_cache.get(symbol)
        if (not cached or time.monotonic() - cached[0] >= BOOK_SNAPSHOT_TTL_SECONDS
                or not self._timestamp_fresh(cached[2])):
            raise ValueError("共享买卖报价中缺少有效合约")
        return cached[1]

    async def _refresh_book_snapshot(self) -> None:
        async with self._book_snapshot_lock:
            now = time.monotonic()
            if now - self._book_snapshot_at < BOOK_SNAPSHOT_TTL_SECONDS:
                return
            if (self._book_snapshot_attempt_at
                    and now - self._book_snapshot_attempt_at < BOOK_SNAPSHOT_RETRY_SECONDS):
                raise RuntimeError("共享买卖报价刚刚刷新失败，短暂退避")
            self._book_snapshot_attempt_at = now
            # Official request weight is 5 without a symbol (2 per symbol).
            try:
                books = await self._request("/fapi/v1/ticker/bookTicker", weight=5)
                if not isinstance(books, list):
                    raise ValueError("共享买卖报价响应格式无效")
            except asyncio.CancelledError:
                self._book_snapshot_attempt_at = time.monotonic()
                raise
            except Exception:
                # Mark failure completion, not request start.  Otherwise a slow
                # failed request can outlive the retry window and every waiter
                # behind the lock immediately launches another bulk retry.
                self._book_snapshot_attempt_at = time.monotonic()
                raise
            received_at = self.now()
            cached_at = time.monotonic()
            snapshot: dict[str, tuple[float, float, int]] = {}
            invalid = 0
            for book in books:
                try:
                    symbol = book.get("symbol")
                    if symbol not in self._metadata:
                        continue
                    bid, ask = float(book["bidPrice"]), float(book["askPrice"])
                    if not (math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask):
                        raise ValueError("买卖报价无效")
                    raw_time = book.get("time")
                    if raw_time is not None:
                        if isinstance(raw_time, bool):
                            raise ValueError("买卖报价时间无效")
                        source_time = int(raw_time)
                        if not self._timestamp_fresh(source_time):
                            raise ValueError("买卖报价时间已过期或超前")
                    else:
                        # The prior single-symbol path allowed an omitted time;
                        # receipt time remains an explicit, fresh upper bound.
                        source_time = received_at
                    spread = (ask - bid) / ((ask + bid) / 2) * 10_000
                    snapshot[symbol] = (cached_at, spread, source_time)
                except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
                    invalid += 1
            # Publish atomically only after the whole response was validated.
            self._book_cache = snapshot
            self._book_snapshot_at = cached_at
            self._book_snapshot_observed_at = received_at
            self._book_snapshot_refreshes += 1
            self._book_snapshot_symbols = len(snapshot)
            self._book_snapshot_invalid = invalid

    def _fence_reconnected_symbols(self, symbols: list[str]) -> None:
        """Block signals until each reconnected symbol proves continuity.

        Reconnecting a shard is not evidence that every symbol missed a bar.
        The next new closed candle is checked by ``_on_closed``; a continuous
        candle releases this fence, while a jump enters normal gap recovery.
        Symbols that produce no close for three minutes are still handled by
        ``_health_loop``.
        """
        fenced_at = self.now()
        for symbol in symbols:
            if symbol in self._metadata and symbol in self._last_minute:
                self._reconnect_fence.add(symbol)
                self._symbol_gate[symbol] = max(fenced_at, self._symbol_gate.get(symbol, 0))

    def _schedule_gap(self, symbol: str, start: int) -> None:
        # A proven/recovery gap supersedes the temporary reconnect fence.
        self._reconnect_fence.discard(symbol)
        self._symbol_gate[symbol] = self.now()
        self._gap_start[symbol] = min(start, self._gap_start.get(symbol, start))
        if symbol not in self._gap_pending:
            self._gap_pending.add(symbol)
            self._gap_queue.put_nowait(symbol)

    def _formal_pair_missing_start(self, symbol: str, timeframe: str,
                                   confirmed_now: int | None = None) -> int | None:
        """Return the first missing due formal bucket proved by durable raw data.

        A wall clock alone cannot distinguish a missing formal candle from a
        contract that is not listed yet or is normally not printing bars.  A
        newer *saved* 1m close can: once it has crossed the confirmed 5m/15m
        boundary, an older formal ``last_close`` is a real tail omission.  The
        receipt watermark is deliberately excluded so a busy persistence queue
        cannot trigger a whole-market REST repair storm.
        """
        if (timeframe not in MONITOR_TIMEFRAMES
                or not eligible_contract(self._metadata.get(symbol, {}))
                or (symbol, timeframe) in self._warm_pending):
            return None
        state = self._pair_state.get((symbol, timeframe), {})
        last_close = state.get("last_close")
        if (not isinstance(last_close, int) or isinstance(last_close, bool)
                or last_close <= 0):
            # Empty/newly-listed histories remain owned by bounded warmup.  Do
            # not turn their absence into an every-30-second native REST poll.
            return None
        interval = TIMEFRAME_MS[timeframe]
        confirmed_now = self._confirmed_history_now() if confirmed_now is None else confirmed_now
        expected_end = confirmed_now // interval * interval - 1
        onboard = int(self._metadata[symbol].get("onboardDate") or 0)
        if expected_end < onboard or last_close >= expected_end:
            return None
        durable_raw_open = self._last_minute.get(symbol)
        if durable_raw_open is None or durable_raw_open + MINUTE - 1 < expected_end:
            return None
        return max(last_close + 1, onboard)

    def _formal_stale_start(self, symbol: str, confirmed_now: int | None = None) -> int | None:
        """Coalesce all proved formal tail omissions into one symbol repair."""
        starts = [start for timeframe in MONITOR_TIMEFRAMES
                  if (start := self._formal_pair_missing_start(
                      symbol, timeframe, confirmed_now)) is not None]
        return min(starts) if starts else None

    def _recovery_confirmation_delay(self, timeframe: str) -> float:
        """Delay a repair when the latest theoretical bucket is in clock safety."""
        interval = TIMEFRAME_MS[timeframe]
        now = self.now()
        theoretical_end = now // interval * interval - 1
        confirmed_now = self._confirmed_history_now()
        confirmed_end = confirmed_now // interval * interval - 1
        if theoretical_end <= confirmed_end:
            return 0.0
        needed = max(1, theoretical_end + 1 - confirmed_now) / 1000
        return min(RECOVERY_CONFIRM_WAIT_MAX_SECONDS, needed + 0.05)

    async def _delayed_gap_requeue(self, symbol: str, start: int, delay: float) -> None:
        """Keep the signal gap gate closed while freeing the recovery worker."""
        try:
            await asyncio.sleep(max(0.05, delay))
            if not self._running or symbol not in self._metadata:
                self._gap_pending.discard(symbol)
                self._gap_start.pop(symbol, None)
                return
            start = min(start, self._gap_start.get(symbol, start))
            self._gap_pending.discard(symbol)
            self._schedule_gap(symbol, start)
        except asyncio.CancelledError:
            raise

    async def _recover_worker(self) -> None:
        while self._running:
            symbol = await self._gap_queue.get()
            failed = False
            delayed_retry: float | None = None
            try:
                if symbol not in self._metadata:
                    self._gap_start.pop(symbol, None)
                    continue
                start = self._gap_start.pop(symbol, self.now() - 3 * MINUTE)
                # Native period repair also fixes buckets spanning the interruption.
                for timeframe in MONITOR_TIMEFRAMES:
                    delayed_retry = self._recovery_confirmation_delay(timeframe) or None
                    if delayed_retry is not None:
                        # Do not let all recovery workers sleep through the
                        # midpoint-clock safety window. Keep this symbol gated
                        # and requeue it after the bounded delay instead.
                        self._gap_start[symbol] = min(self._gap_start.get(symbol, start), start)
                        break
                    interval = TIMEFRAME_MS[timeframe]
                    if (symbol, timeframe) in self._warm_pending:
                        # Its full warmup owns the database range right now, but
                        # that range was frozen when the job started and may end
                        # before the just-missed live bucket. Keep the symbol
                        # gated and retry after a bounded pause; never clear the
                        # proven boundary gap merely because warmup is active.
                        self._gap_start[symbol] = min(self._gap_start.get(symbol, start), start)
                        delayed_retry = RECOVERY_DATA_RETRY_SECONDS
                        break
                    # Recompute for every period: earlier REST work may cross a
                    # 5m/15m boundary or finish the clock safety window.
                    end = self._confirmed_history_now() // MINUTE * MINUTE - 1
                    range_start = max(start, end - 14 * DAY)
                    floor = range_start // interval * interval
                    closed_end = (end + 1) // interval * interval - 1
                    if floor <= closed_end:
                        await self._fetch_range(symbol, timeframe, floor, closed_end)
                    target = LOOKBACK[timeframe]["days"] * DAY // interval + 3
                    # REST I/O stays outside the lock: otherwise slow repairs
                    # can occupy all three closed-candle workers.
                    # The pending set owns this symbol's repair job, while the
                    # same lock as live processing protects its state commit.
                    async with self._symbol_locks.setdefault(symbol, asyncio.Lock()):
                        candles = await asyncio.to_thread(self.store.get_candles, symbol, timeframe, target)
                        self._set_pair_state(symbol, timeframe, candles)
                    missing_open = self._formal_pair_missing_start(symbol, timeframe)
                    if missing_open is not None:
                        # The bucket is now confirmed but the official response
                        # still did not supply it even though a durable 1m close
                        # has crossed that boundary. Keep the signal gate and
                        # retry slowly without waiting for stale maintenance.
                        retry_start = min(start, missing_open)
                        self._gap_start[symbol] = min(
                            self._gap_start.get(symbol, retry_start), retry_start)
                        delayed_retry = RECOVERY_DATA_RETRY_SECONDS
                        break
                if delayed_retry is None:
                    self._symbol_gate[symbol] = self.now()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed = True
                self._error(f"缺口回补 {symbol}", exc)
                self._gap_start[symbol] = min(self._gap_start.get(symbol, start), start)
                await asyncio.sleep(2)
            finally:
                self._gap_queue.task_done()
                if (delayed_retry is not None and not failed and self._running and symbol in self._metadata):
                    retry_start = self._gap_start.get(symbol, start)
                    self._spawn(self._delayed_gap_requeue(symbol, retry_start, delayed_retry),
                                f"gap-delay-{symbol}")
                else:
                    self._gap_pending.discard(symbol)
                if (failed or (delayed_retry is None and symbol in self._gap_start)) and self._running and symbol in self._metadata:
                    self._schedule_gap(symbol, self._gap_start[symbol])
                elif symbol not in self._metadata:
                    self._gap_start.pop(symbol, None)

    async def _sample_oi(self, symbol: str) -> None:
        self._oi_inflight += 1
        try:
            # All workers still pass through the one shared REST weight budget.
            data = await self._request("/fapi/v1/openInterest", {"symbol": symbol})
            if not eligible_contract(self._metadata.get(symbol, {})):
                return
            sampled_at = self.now()  # Observation time, never its scheduled slot.
            source_time = data.get("time")
            if (not isinstance(source_time, int) or isinstance(source_time, bool) or source_time <= 0
                    or not self._timestamp_fresh(source_time, 75_000)):
                raise ValueError("持仓量源时间缺失、过期或超前；不保存为当前样本")
            quantity = float(data["openInterest"])
            if not math.isfinite(quantity) or quantity <= 0:
                return
            mark = self._mark.get(symbol)
            price = mark[0] if mark and self._timestamp_fresh(mark[1]) else None
            await asyncio.to_thread(self.store.save_oi, {
                "symbol": symbol, "timestamp": sampled_at,
                "open_interest_quantity": quantity, "mark_price": price,
                "source_time": source_time,
            })
            self._oi_cycle_samples += 1
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._error(f"持仓量 {symbol}", exc)
        finally:
            self._oi_inflight -= 1

    async def _oi_cycle(self, symbols: list[str], began: float) -> None:
        """At most eight requests in flight; spread first dispatches over a minute."""
        jobs: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        for index, symbol in enumerate(symbols):
            jobs.put_nowait((index, symbol))

        async def worker() -> None:
            while self._running:
                try:
                    index, symbol = jobs.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    due = began + index * OI_CYCLE_SECONDS / max(1, len(symbols))
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    if self._running and symbol in self._metadata:
                        await self._sample_oi(symbol)
                finally:
                    jobs.task_done()

        workers = [asyncio.create_task(worker(), name=f"oi-sample-{index}")
                   for index in range(min(OI_WORKERS, len(symbols)))]
        try:
            await asyncio.gather(*workers)
        finally:
            for task in workers:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

    async def _oi_poller(self) -> None:
        while self._running:
            began = time.monotonic()
            self._oi_cycle_started_at = self.now()
            self._oi_cycle_samples = 0
            await self._oi_cycle(list(self._metadata), began)
            self._oi_last_cycle = self.now()
            self._oi_cycle_seconds = round(time.monotonic() - began, 1)
            await asyncio.sleep(max(1, OI_CYCLE_SECONDS - (time.monotonic() - began)))

    def oi_enrichment(self, symbol: str, timeframe: str, close_time: int) -> dict:
        """Only compare matched two-bar durations; missing boundaries stay unscored.

        Snapshots are approximate minute observations. Both boundaries must be no
        more than 75 seconds old and their lags must differ by <=20 seconds.
        Thirty prior, non-overlapping-with-current endpoints are required.
        """
        interval = TIMEFRAME_MS[timeframe]
        duration = interval * 2
        end = close_time + 1
        start = end - duration
        rows = self.store.load_oi(symbol, since=end - LOOKBACK[timeframe]["days"] * DAY - duration,
                                  until=end, limit=130_000)
        missing = {"oi_change_pct": None, "oi_ratio": None, "oi_sample_count": 0}
        # Earlier collectors stored receipt timestamps without validating the
        # exchange time. Never let delayed/legacy content become fresh merely
        # because its HTTP response arrived at a recent local time.
        by_source = {}
        for row in rows:
            source_time = row.get("source_time")
            if (isinstance(source_time, int) and not isinstance(source_time, bool) and source_time > 0
                    and -self._future_slack_ms() <= row["timestamp"] - source_time < 75_000):
                by_source.setdefault(source_time, row)  # Keep the first actual observation.
        times = sorted(by_source)
        rows = [by_source[timestamp] for timestamp in times]
        if len(rows) < 33:
            return missing

        def value(boundary: int) -> dict | None:
            index = bisect.bisect_right(times, boundary) - 1
            # Source content cannot be used before it was actually received.
            while index >= 0 and rows[index]["timestamp"] > boundary:
                index -= 1
            if index < 0 or boundary - times[index] > 75_000:
                return None
            return rows[index]

        def change(boundary: int) -> float | None:
            first, last = value(boundary - duration), value(boundary)
            if not first or not last or first["open_interest_quantity"] <= 0:
                return None
            if abs((last["source_time"] - first["source_time"]) - duration) > 20_000:
                return None
            return (last["open_interest_quantity"] / first["open_interest_quantity"] - 1) * 100

        current = change(end)
        if current is None:
            return missing
        history: list[float] = []
        boundary = start  # Historical windows cannot include the current pair.
        earliest = times[0] + duration
        while boundary >= earliest:
            item = change(boundary)
            if item is not None:
                history.append(abs(item))
            boundary -= interval
        if len(history) < 30:
            return {**missing, "oi_sample_count": len(history)}
        baseline = sum(history) / len(history)
        if baseline <= 0:
            return {**missing, "oi_sample_count": len(history)}
        return {"oi_change_pct": current, "oi_ratio": abs(current) / baseline, "oi_sample_count": len(history)}

    async def _health_loop(self) -> None:
        cycle = 0
        while self._running:
            await asyncio.sleep(30)
            cycle += 1
            confirmed_now = self._confirmed_history_now()
            for symbol, latest in list(self._last_minute.items()):
                # A valid close already received into the bounded durable queue
                # is liveness evidence, but not signal/persistence evidence.
                # This prevents processing pressure from self-amplifying into a
                # false REST repair storm while preserving actual jump checks in
                # _on_closed and QueueFull repair in _kline_stream.
                observed = max(latest, self._last_received_minute.get(symbol, 0))
                if symbol in self._metadata and self.now() - observed > 3 * MINUTE and symbol not in self._gap_pending:
                    self._schedule_gap(symbol, latest + MINUTE)
                    continue
                # Raw liveness can continue while a boundary final close was
                # delayed/reordered and its 5m/15m aggregate was never saved.
                # Scan the in-memory formal watermarks every 30 seconds; only a
                # durable raw close beyond the confirmed boundary may trigger
                # one deduplicated native-period repair for the whole symbol.
                if symbol in self._metadata and symbol not in self._gap_pending:
                    formal_start = self._formal_stale_start(symbol, confirmed_now)
                    if formal_start is not None:
                        self._schedule_gap(symbol, formal_start)
            if cycle % 20 == 0:
                # Effective state includes freshness aging; the stored base
                # state can still say ready after its latest close is stale.
                for (symbol, timeframe), state in self._effective_pair_states().items():
                    if (symbol in self._metadata and symbol not in self._gap_pending
                            and (symbol, timeframe) not in self._warm_pending
                            and state.get("state") in {"error", "stale", "gap", "insufficient_history"}):
                        self._queue_warm(symbol, timeframe)
            if cycle % 120 == 0:
                await asyncio.to_thread(self.store.prune, self.now())

    async def chart(self, symbol: str, timeframe: str, limit: int = 180) -> list[dict]:
        if timeframe not in MONITOR_TIMEFRAMES:
            raise ValueError("不支持的K线周期")
        candles = await asyncio.to_thread(self.store.get_candles, symbol.upper(), timeframe, limit)
        return [c.to_dict() for c in candles]

    def download_status(self, symbol: str | None = None, timeframe: str | None = None,
                        limit: int = 1000, after: int | None = None) -> dict:
        """Pure memory snapshot: no network, database scan, or collector mutation."""
        if timeframe is not None and timeframe not in MONITOR_TIMEFRAMES:
            raise ValueError("不支持的K线周期")
        symbol = symbol.upper() if symbol else None
        jobs = self._download_jobs.copy()
        metadata = self._metadata.copy()
        pending = self._warm_pending.copy()
        states = self._effective_pair_states()
        # Saved inventory is not proof of the current user-selected universe.
        # Before official scope verification, do not revive old non-USDT jobs.
        pairs = {(s, tf) for s in metadata for tf in MONITOR_TIMEFRAMES}
        quotes = self._live_klines.copy()
        rows = []
        for pair in sorted(pairs, key=lambda p: (p[0], TIMEFRAME_MS.get(p[1], 0))):
            s, tf = pair
            row = {"symbol": s, "timeframe": tf, "state": "queued", "stage": "等待下载任务",
                   "bars": 0, "minimum": LOOKBACK[tf]["minimum_bars"] + 3,
                   "target_bars": LOOKBACK[tf]["days"] * DAY // TIMEFRAME_MS[tf] + 3,
                   "cached_bars": 0, "downloaded_bars": 0, "pages": 0, "progress_pct": None,
                   "requested_start": None, "requested_end": None, "first_open_time": None,
                   "last_close_time": None, "last_page_at": None, "latest_candle": None,
                   "download_complete": False, "request_complete": False, "window_complete": False,
                   "window_missing_bars": None, "error": None, **jobs.get(pair, {})}
            entry = states.get(pair, {})
            if pair in pending:
                row["state"] = "downloading" if row["state"] == "downloading" else "queued" if row["state"] not in {"error"} else "error"
                row["download_complete"] = False
            elif entry.get("state") in {"ready", "insufficient_history", "gap", "stale", "error"}:
                row["state"] = entry["state"]
            row["reason"] = entry.get("reason", row.get("reason"))
            if row.get("latest_candle"):
                row["latest_candle"] = dict(row["latest_candle"])
            row.update(contract_metadata_fields(metadata.get(s, {})))
            row["market_type"] = "UM"
            quote = quotes.get(s)
            row["live_quote"] = ({**quote, "price": quote["close"], "stale": not self._timestamp_fresh(quote["event_time"]),
                                   "connected": self._symbol_live(s)} if quote else None)
            ticker = self._ticker.get(s)
            row["market_quote"] = ({**ticker, "source": "Binance 24h ticker", "stale": not self._timestamp_fresh(ticker.get("time"), 180_000)}
                                     if ticker else None)
            rows.append(row)
        filtered = [r for r in rows if (not symbol or r["symbol"] == symbol) and (not timeframe or r["timeframe"] == timeframe)]
        events = list(self._download_events)
        matched = [event.copy() for event in events if (after is None or event["seq"] > after)
                   and (not symbol or event.get("symbol") in {None, symbol})
                   and (not timeframe or event.get("timeframe") in {None, timeframe})]
        for event in matched:
            if event.get("latest_candle"):
                event["latest_candle"] = dict(event["latest_candle"])
        last_page = max((r.get("last_page_at") or 0 for r in rows), default=0)
        complete = sum(r["download_complete"] for r in rows)
        summary = {"total_pairs": len(rows), "completed_pairs": complete,
            "request_completed_pairs": sum(bool(r["request_complete"]) for r in rows),
            "window_complete_pairs": sum(bool(r["window_complete"]) for r in rows),
            "ready_pairs": sum(r["state"] == "ready" for r in rows),
            "downloading_pairs": sum(r["state"] == "downloading" for r in rows),
            "queued_pairs": sum(r["state"] == "queued" for r in rows),
            "error_pairs": sum(r["state"] == "error" for r in rows),
            "insufficient_pairs": sum(r["state"] == "insufficient_history" for r in rows),
            "cached_bars": sum(r["cached_bars"] for r in rows),
            "downloaded_bars": sum(r["downloaded_bars"] for r in rows),
            "pages_saved": sum(r["pages"] for r in rows), "last_page_at": last_page or None,
            "progress_pct": completion_percentage(complete, len(rows)),
            "lookback_days": {tf: LOOKBACK[tf]["days"] for tf in MONITOR_TIMEFRAMES},
            "history_workers": 6, "scope_verified": bool(self._last_refresh),
            "received_count_note": "downloaded_bars 是本次进程会话收到并写入的条数，可含重复覆盖；bars 是对应历史窗口的唯一库存"}
        limit = max(1, min(int(limit), 10_000))
        return {"source": "live", "session_id": self._download_session_id, "cursor": self._download_cursor,
                "server_time": self.now(), "state": self._state, "summary": summary,
                "items": filtered[:limit], "items_total": len(filtered), "truncated": len(filtered) > limit,
                "events": matched[-100:], "events_truncated": len(matched) > 100 or bool(events and after is not None and after < events[0]["seq"] - 1),
                "oldest_available_seq": events[0]["seq"] if events else None,
                "telegram_enabled": self.telegram_enabled(),
                "note": "下载范围为各周期评分所需历史窗口，并非交易所成立以来的全部历史；未收盘实时报价仅展示、不存为历史或参与评分"}

    def _effective_pair_states(self) -> dict[tuple[str, str], dict]:
        now = self.now()
        states = {}
        for symbol in self._metadata.copy():
            for timeframe in MONITOR_TIMEFRAMES:
                entry = dict(self._pair_state.get((symbol, timeframe), {"state": "queued", "bars": 0}))
                entry.setdefault("minimum", LOOKBACK[timeframe]["minimum_bars"] + 3)
                if (symbol, timeframe) in self._warm_pending:
                    entry["state"] = "warming" if self._download_jobs.get((symbol, timeframe), {}).get("state") == "downloading" else "queued"
                if entry.get("state") == "ready" and not self._timestamp_fresh(entry.get("last_close"), TIMEFRAME_MS[timeframe] + 90_000):
                    entry["state"] = "stale"
                if symbol in self._gap_pending and entry.get("state") == "ready":
                    entry["state"] = "gap"
                entry["reason"] = self._history_reason(symbol, timeframe, entry["state"])
                states[(symbol, timeframe)] = entry
        return states

    def _symbol_live(self, symbol: str) -> bool:
        return (self._running and self._state not in {"blocked", "error", "stopped", "connecting"}
                and symbol not in self._reconnect_fence
                and self._connections.get(self._symbol_shard.get(symbol, ""), {}).get("state") == "connected"
                and self._timestamp_fresh(self._symbol_heartbeat.get(symbol))
                and self._last_refresh > 0 and self.now() - self._last_refresh < 180_000)

    def _scope_counts(self) -> dict:
        metadata = list(self._metadata.values())

        def counts(field: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for meta in metadata:
                key = str(meta.get(field) or "UNKNOWN")
                result[key] = result.get(key, 0) + 1
            return dict(sorted(result.items()))

        return {"market_type": "UM", "futures_type": "U_MARGINED",
                "quote_assets": sorted({m["quoteAsset"] for m in metadata if m.get("quoteAsset")}),
                "margin_assets": sorted({m["marginAsset"] for m in metadata if m.get("marginAsset")}),
                "contract_type_counts": counts("contractType"), "underlying_type_counts": counts("underlyingType"),
                "tradfi_contracts": sum(contract_metadata_fields(m)["is_tradfi"] for m in metadata)}

    def universe_status(self) -> dict:
        """Saved scope, current subscriptions and actual live coverage are distinct."""
        states = self._effective_pair_states()
        snapshot = self.store.universe_snapshot_status()
        metadata = {m["symbol"]: m for m in self.store.list_universe()}
        # Also support a first in-flight refresh before the persisted read catches
        # up, without ever treating old saved metadata as a live subscription.
        metadata.update(self._metadata)
        items = []
        for symbol, meta in sorted(metadata.items()):
            trading = symbol in self._metadata
            heartbeat = self._symbol_heartbeat.get(symbol)
            live = trading and self._symbol_live(symbol)
            onboard = int(meta.get("onboardDate") or 0)
            periods = {}
            for timeframe in MONITOR_TIMEFRAMES:
                interval = TIMEFRAME_MS[timeframe]
                entry = dict(states.get((symbol, timeframe), {
                    "state": "not_trading" if meta.get("status") != "TRADING" else "offline",
                    "reason": "pending_listing" if meta.get("status") == "PENDING_TRADING" else "not_currently_subscribed",
                    "bars": 0, "minimum": LOOKBACK[timeframe]["minimum_bars"] + 3,
                }))
                entry["live_ready"] = live and entry["state"] == "ready"
                if entry.get("reason") == "new_insufficient" and onboard:
                    entry["estimated_earliest_ready_at"] = onboard // interval * interval + entry["minimum"] * interval
                periods[timeframe] = entry
            items.append({"symbol": symbol, **contract_metadata_fields(meta), "market_type": "UM",
                          "status": meta.get("status"), "snapshot_observed_at": snapshot["observed_at"],
                          "subscribed": trading, "heartbeat_time": heartbeat, "live": live,
                          "last_closed_time": self._last_minute[symbol] + MINUTE - 1 if symbol in self._last_minute else None,
                          "timeframes": periods,
                          "ready_timeframes": sum(v["state"] == "ready" for v in periods.values()),
                          "live_ready_timeframes": sum(v["live_ready"] for v in periods.values())})
        scope = self._scope_counts()
        return {"source": "live", "telegram_enabled": self.telegram_enabled(), "items": items, **scope,
                "summary": {**snapshot, "verified_trading_count": len(self._metadata),
                            **scope,
                            "pending_count": sum(m.get("status") == "PENDING_TRADING" for m in metadata.values()),
                            "live_heartbeat_count": sum(item["live"] for item in items),
                            "live_ready_symbols": sum(item["live_ready_timeframes"] == len(MONITOR_TIMEFRAMES) for item in items),
                            "unknown_underlying_count": self._unknown_underlying},
                "note": "范围快照仅表示已发现的合约；逐币心跳和各周期 live_ready 才表示当前实际覆盖"}

    def status(self) -> dict:
        states = self._effective_pair_states()
        symbols = {symbol for symbol, _ in states}
        ready_by_tf = {tf: sum(v.get("state") == "ready" for (s, t), v in states.items() if t == tf) for tf in MONITOR_TIMEFRAMES}
        ready_symbols = sum(all(states[(s, tf)].get("state") == "ready" for tf in MONITOR_TIMEFRAMES) for s in symbols)
        warming = sum(v.get("state") in {"queued", "warming"} for v in states.values())
        insufficient = sum(v.get("state") == "insufficient_history" for v in states.values())
        errors = sum(v.get("state") in {"error", "stale", "gap"} for v in states.values())
        live_symbols = {s for s in symbols if self._symbol_live(s)}
        live_ready_by_tf = {tf: sum(s in live_symbols and v["state"] == "ready" for (s, t), v in states.items() if t == tf)
                            for tf in MONITOR_TIMEFRAMES}
        timeframe_counts = {tf: {"total": len(symbols), "ready": ready_by_tf[tf], "live_ready": live_ready_by_tf[tf],
                                 **{kind: sum(v["state"] == kind for (s, t), v in states.items() if t == tf)
                                    for kind in ("queued", "warming", "insufficient_history", "gap", "stale", "error")},
                                 "new_insufficient": sum(v.get("reason") == "new_insufficient" for (s, t), v in states.items() if t == tf)}
                            for tf in MONITOR_TIMEFRAMES}
        snapshot, recent_listings = self.store.status_snapshot(5)
        now_monotonic = time.monotonic()
        recent_weight = sum(w for at, w in self._budget._history if at >= now_monotonic - 60)
        oi_elapsed = max(0, (self.now() - self._oi_cycle_started_at) / 1000) if self._oi_cycle_started_at and self._running else 0
        completed_pairs = sum(bool(self._download_jobs.get(pair, {}).get("download_complete")) and pair not in self._warm_pending for pair in states)
        scheduler = {"warmup_pending_pairs": len(self._warm_pending), "warmup_queue": self._warm_queue.qsize(),
                     "closed_queue": self._closed_queue.qsize(), "analysis_queue": self._analysis_queue.qsize(),
                     "candidate_queue": self._candidate_queue.qsize(),
                     "queue_capacity": self._closed_queue.maxsize,
                     "analysis_queue_capacity": self._analysis_queue.maxsize,
                     "candidate_queue_capacity": self._candidate_queue.maxsize,
                     "analysis_dropped": self._analysis_dropped,
                     "analysis_expired": self._analysis_expired,
                     "analysis_expired_conditions": dict(self._analysis_expired_conditions),
                     "candidate_dropped": self._candidate_dropped,
                     "book_snapshot_refreshes": self._book_snapshot_refreshes,
                     "book_snapshot_symbols": self._book_snapshot_symbols,
                     "book_snapshot_invalid_rows": self._book_snapshot_invalid,
                     "book_snapshot_observed_at": self._book_snapshot_observed_at or None,
                     "book_snapshot_age_ms": (round((now_monotonic - self._book_snapshot_at) * 1000)
                                              if self._book_snapshot_at else None),
                     "rest_weight_scheduled_last_minute": recent_weight,
                     "rest_budget_utilization_pct": round(recent_weight / self._budget.per_minute * 100, 1),
                     "oi_target_requests_per_minute": len(symbols),
                      "oi_cycle_overdue": oi_elapsed > 60 or (self._oi_cycle_seconds or 0) > 120,
                      "oi_current_cycle_seconds": round(oi_elapsed, 1), "history_workers": 6,
                      "analysis_workers": SCAN_WORKERS, "candidate_workers": CANDIDATE_WORKERS,
                      "gap_workers": GAP_WORKERS, "gap_queue": self._gap_queue.qsize(),
                      "oi_workers": OI_WORKERS, "oi_inflight": self._oi_inflight,
                      "oi_cycle_samples": self._oi_cycle_samples,
                      "kline_symbols_per_shard": KLINE_SYMBOLS_PER_SHARD,
                      "market_symbols_per_shard": KLINE_SYMBOLS_PER_SHARD,
                      "market_streams_per_symbol": 2,
                     "pressure": "high" if (self._closed_queue.qsize() > 2000 or self._analysis_queue.qsize() > 2000
                         or self._candidate_queue.qsize() > 2000
                         or max(oi_elapsed, self._oi_cycle_seconds or 0) > 120) else "warming" if self._warm_pending else "normal"}
        connections = [self._connection_snapshot(name, info) for name, info in self._connections.copy().items()]
        fresh_connections = sum(c["content_fresh"] for c in connections if c["name"].startswith("kline-"))
        state, message = self._state, self._message
        if state not in {"blocked", "error", "stopped", "connecting"}:
            if fresh_connections == 0:
                state, message = ("warming", "历史下载正在继续；实时K线流恢复中，请查看逐币下载进度") if self._warm_pending and self._running else ("reconnecting", "实时行情连接恢复中；已有历史数据不代表当前行情在线")
            elif warming:
                state, message = "warming", (f"历史窗口已补齐 {completed_pairs}/{len(states)}；"
                                             f"当前可监测 {sum(ready_by_tf.values())}/{len(states)} 个合约周期")
            elif (errors or self._gap_pending or fresh_connections < math.ceil(len(symbols) / KLINE_SYMBOLS_PER_SHARD)
                    or len(live_symbols) < len(symbols)):
                state, message = "degraded", "部分合约行情或历史尚未就绪；缺口期间不生成正式信号"
            else:
                state = "ready"
                message = f"实时监测运行中；{insufficient} 个合约周期历史不足，暂不评分" if insufficient else "全部合约 5m / 15m 历史已就绪，实时监测运行中"
        return {"state": state, "message": message, "source": "live", "telegram_enabled": self.telegram_enabled(),
                **self._scope_counts(),
                "universe_count": len(symbols), "ready_symbols": ready_symbols,
                "scope_snapshot": snapshot, "live_heartbeat_symbols": len(live_symbols),
                "live_ready_symbols": sum(all(states[(s, tf)]["state"] == "ready" for tf in MONITOR_TIMEFRAMES) for s in live_symbols),
                "live_ready_pairs": sum(live_ready_by_tf.values()), "timeframe_live_ready": live_ready_by_tf,
                "timeframe_counts": timeframe_counts, "scope_verified": self._last_refresh > 0,
                "metadata_stale": not self._last_refresh or self.now() - self._last_refresh >= 180_000,
                "warming_symbols": len(symbols) - ready_symbols, "timeframe_ready": ready_by_tf,
                "timeframe_total": len(states), "ready_pairs": sum(ready_by_tf.values()),
                "warming_pairs": warming, "insufficient_pairs": insufficient, "error_pairs": errors,
                "history_progress": completion_percentage(completed_pairs, len(states)),
                "history_completed_pairs": completed_pairs,
                "monitor_ready_progress": completion_percentage(sum(ready_by_tf.values()), len(states)),
                "history_jobs_done": self._history_jobs_done, "last_event_time": self._last_event or None,
                "last_closed_time": self._last_closed or None, "last_universe_refresh": self._last_refresh or None,
                "live_watermark": self._watermark or None, "server_time": self.now(),
                "clock_offset_ms": self._clock_offset, "clock_rtt_ms": self._clock_rtt_ms,
                "clock_synced_at": self._clock_synced_at or None, "timestamp_future_slack_ms": self._future_slack_ms(),
                "rest_closed_time_upper_bound": self._confirmed_history_now(),
                "download_session_id": self._download_session_id, "download_event_cursor": self._download_cursor,
                "connections": connections,
                "liquidity_rank_status": self._liquidity_rank_status(),
                "errors": list(self._errors), "rest_weight": self._budget.used_header,
                "rest_budget_per_minute": self._budget.per_minute,
                "rest_cooldown_seconds": max(0, round(self._budget.cooldown_until - time.monotonic())),
                "gap_symbols": len(self._gap_pending), "closed_queue": self._closed_queue.qsize(),
                "analysis_queue": self._analysis_queue.qsize(), "candidate_queue": self._candidate_queue.qsize(),
                "analysis_dropped": self._analysis_dropped, "analysis_expired": self._analysis_expired,
                "analysis_expired_conditions": dict(self._analysis_expired_conditions),
                "candidate_dropped": self._candidate_dropped,
                "last_received_closed_time": (max(self._last_received_minute.values()) + MINUTE - 1
                                              if self._last_received_minute else None),
                "received_closed_candles": self._received_closed,
                "live_signals_this_run": self._signal_count, "excluded_contracts": self._excluded,
                "qualified_signals_this_run": self._qualified_count,
                "new_listing_events_this_run": self._listing_count, "recent_listings": recent_listings,
                "listing_count": snapshot["listed_events"], "upcoming_listing_count": snapshot["upcoming_events"],
                "listing_poll_seconds": 60, "exclusion_reasons": self._exclusion_reasons,
                "unknown_underlying_contracts": self._unknown_underlying, "scheduler": scheduler,
                "oi_last_cycle": self._oi_last_cycle or None, "oi_cycle_seconds": self._oi_cycle_seconds,
                "oi_note": "8并发均匀调度，目标每60秒一轮；实际周期受网络/共享额度影响，至少30个同周期有效历史变化样本后才计分",
                "scope": "仅币安官方UM且USDT计价的全部合约类型与资产类别（含TradFi和交割）；TRADING实时采集，PENDING_TRADING单列待上市"}
