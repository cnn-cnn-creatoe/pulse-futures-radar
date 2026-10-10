"""Loopback-only dashboard API. Telegram is opt-in; no trading client exists."""
from __future__ import annotations
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from functools import lru_cache
import json
import logging
import os
import threading
import time
import asyncio
from typing import Any, Literal
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from .engine import EngineConfig, MONITOR_TIMEFRAMES, TIMEFRAME_MS, LOOKBACK
from .demo import demo_signals
from .store import Store
from .collector import MonitorService
from .policy import Settings, ENGINE_KEYS, THRESHOLD_PRIORS, apply_policy, migrate_saved_settings
from .indicators import indicator_catalog, indicator_context
from .custom_indicators import (
    CustomIndicatorRegistry,
    IndicatorRegistryError,
    IndicatorValidationError,
)
from .research import (MAX_RESEARCH_BARS, Research, backtest_summary_csv,
                       backtest_trades_csv)
from .history_data import parse_candles_csv, parse_candles_json, valid_contract_symbol
from .download_api import install_download_routes
from .daily_journal import DailyArchives, SimpleReplay, install_daily_routes, record_custom
from .workbench import Workbench, install_workbench_routes
from .telegram import (TelegramError, TelegramService, TokenStorageError,
                       normalize_preferences)
from .update import CURRENT_VERSION, UpdateError, UpdateService

APPLICATION_ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.getenv('PULSE_DATA_DIR', str(Path(__file__).resolve().parents[1] / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
CONFIG_FILE = DATA / 'settings.json'
def load_policy():
    if CONFIG_FILE.exists():
        saved = json.loads(CONFIG_FILE.read_text('utf-8'))
        if saved.get('baseline_method')=='winsorized_mean':
            saved['baseline_method']='trimmed_mean'
        return Settings(**migrate_saved_settings(saved))
    return Settings()
policy = load_policy()
def load_config():
    return load_policy().engine()
store = Store(DATA / 'pulse.sqlite3')
service = MonitorService(store, policy.engine())
telegram_service = TelegramService(DATA)
update_service = UpdateService(APPLICATION_ROOT)
service.notification_sink = telegram_service.enqueue
service.notification_enabled = lambda: telegram_service.safe_status()['enabled']
custom_indicators = CustomIndicatorRegistry(DATA / 'custom_indicators.json')
def freeze_transform(settings):
    snapshot = settings.model_copy(deep=True)
    specs = []
    async def transform(signal,candles,enrichment):
        def evaluate():
            result = apply_policy(signal,candles,snapshot)
            return record_custom(result,candles,specs) if specs else result
        return await asyncio.to_thread(evaluate)
    return transform
service.candidate_transform = freeze_transform(policy)
research = Research(DATA / 'backtest')
daily_archives = DailyArchives(store, DATA / 'backtest' / 'daily')
simple_replay = SimpleReplay(store, research, custom_indicators, DATA / 'backtest' / 'replays')
workbench = Workbench(store, research, custom_indicators, DATA / 'backtest' / 'workbench')
workbench.fetch_page = service.fetch_research_page
from .live_rule_signals import LiveRuleSignals
live_rules=LiveRuleSignals(store,telegram_service,lambda:policy,now=service.now)
service.minute_rule_sink=live_rules.enqueue_minute
from .smc_live import SmcLive, equity
smc_live=SmcLive(service,telegram_service,DATA / "smc-triggers.sqlite3")
telegram_service.equity_symbols=lambda: sorted(s for s,m in service._metadata.items() if equity(m))
LOG = logging.getLogger(__name__)
DATABASE_COUNTS_TTL_SECONDS = 60.0
_counts_lock = threading.Lock()
_counts_seed_lock = threading.Lock()
_counts_cache = {
    'checked_at': 0.0,
    'updated_at': None,
    'value': None,
    'refreshing': False,
    'closing': False,
    'thread': None,
}

def _publish_database_counts(value: dict) -> None:
    now = time.monotonic()
    with _counts_lock:
        _counts_cache.update(
            value=dict(value),
            checked_at=now,
            updated_at=int(time.time() * 1000),
        )

def _seed_database_counts() -> None:
    """Populate the first exact snapshot before the collector starts writing."""
    with _counts_seed_lock:
        with _counts_lock:
            if _counts_cache['value'] is not None:
                return
        _publish_database_counts(store.counts())

def _database_counts_worker() -> None:
    try:
        value = store.counts()
    except Exception as error:  # Keep the last successful diagnostic snapshot.
        LOG.warning('database count refresh failed: %s', error)
        with _counts_lock:
            _counts_cache['checked_at'] = time.monotonic()
    else:
        _publish_database_counts(value)
    finally:
        with _counts_lock:
            _counts_cache['refreshing'] = False
            _counts_cache['thread'] = None

def _schedule_database_counts_refresh() -> None:
    with _counts_lock:
        if _counts_cache['closing'] or _counts_cache['refreshing']:
            return
        _counts_cache['refreshing'] = True
        thread = threading.Thread(
            target=_database_counts_worker,
            name='database-count-refresh',
            daemon=True,
        )
        _counts_cache['thread'] = thread
        try:
            thread.start()
        except Exception as error:
            _counts_cache['refreshing'] = False
            _counts_cache['thread'] = None
            LOG.warning('database count refresh could not start: %s', error)

def database_counts():
    # A stale exact value is preferable to making every dashboard request wait
    # for a multi-million-row diagnostic scan. Lifespan normally seeds this
    # once; the synchronous fallback only covers callers that bypass lifespan.
    with _counts_lock:
        missing = _counts_cache['value'] is None
    if missing:
        _seed_database_counts()
    with _counts_lock:
        value = dict(_counts_cache['value'])
        updated_at = _counts_cache['updated_at']
        stale = time.monotonic() - _counts_cache['checked_at'] >= DATABASE_COUNTS_TTL_SECONDS
    if stale:
        _schedule_database_counts_refresh()
    return value, updated_at

def _shutdown_database_counts() -> None:
    """Fence new refreshes and wait before the shared store is closed."""
    with _counts_lock:
        _counts_cache['closing'] = True
        thread = _counts_cache['thread']
    if thread is not None:
        # A synchronous join makes it impossible for task cancellation to let
        # an in-memory test/store worker touch a connection after store.close().
        thread.join()

@lru_cache(maxsize=16)
def _cached_demo(config_json: str):
    snapshot = Settings(**migrate_saved_settings(json.loads(config_json)))
    return demo_signals(snapshot.engine(),indicator_config=snapshot.indicator_config(),quality=snapshot.quality.model_dump())

def demo_rows():
    return _cached_demo(json.dumps(policy.model_dump(), sort_keys=True))

@asynccontextmanager
async def lifespan(app):
    from .instance_lock import InstanceLock
    instance = InstanceLock(APPLICATION_ROOT / '.runtime' / 'backend-instance.lock')
    instance.acquire()
    try:
        # Seed before live collection so the first status request is immediate
        # and the initial full scan cannot delay collector writes.
        _seed_database_counts()
        daily_archives.start()
        # Telegram starts first so the live collector can hand it only events
        # created after this process is ready.  No saved alert history is read.
        await telegram_service.start_if_enabled()
        if os.getenv('PULSE_DISABLE_COLLECTOR') != '1':
            await service.start()
            smc_live.start()
            # The collector has already restored its exact startup inventory.
            # Refresh the dashboard cache independently before requests begin;
            # the scan itself continues in the background.
            download_inventory_cache.start()
        yield
    finally:
        download_inventory_cache.close()
        _shutdown_database_counts()
        await extra_periods.close()
        await smc_live.close()
        await service.stop()
        await live_rules.close()
        await telegram_service.close()
        await research.close()
        await simple_replay.close()
        await workbench.close()
        await daily_archives.close()
        store.close()
        instance.close()

app = FastAPI(title='合约推送',version=CURRENT_VERSION.removeprefix('v'),lifespan=lifespan)

@app.get('/api/smc/status')
async def smc_status(day: str | None = None):
    try:
        return smc_live.status(day)
    except ValueError:
        raise HTTPException(status_code=400, detail='日期格式应为 YYYY-MM-DD')

@app.get('/api/smc/contracts')
async def smc_contracts():
    return dict(items=[dict(symbol=s,name=m.get('baseAsset',s)) for s,m in sorted(service._metadata.items()) if equity(m)])

@app.get('/api/telegram/rule-catalog')
def telegram_rule_catalog():
    from .live_rule_signals import rule_catalog
    from .selected_period_rules import TIMEFRAMES
    return dict(indicator=rule_catalog('indicator'),quant=rule_catalog('quant'),timeframes=list(TIMEFRAMES),
        processed=live_rules.processed,dropped=live_rules.dropped,queue=live_rules.queue.qsize(),last_error=live_rules.last_error)

ALLOWED_ORIGINS = ['http://127.0.0.1:5173','http://localhost:5173']
app.add_middleware(CORSMiddleware,allow_origins=ALLOWED_ORIGINS,allow_methods=['GET','PUT','POST'],allow_headers=['Content-Type','X-Pulse-Client'])

def require_dashboard(request:Request):
    origin = request.headers.get('origin')
    if origin and origin not in ALLOWED_ORIGINS:
        raise HTTPException(403,'仅允许本机看板修改配置或启动任务')
    if request.headers.get('x-pulse-client') != 'dashboard':
        raise HTTPException(403,'缺少看板请求标记')

download_inventory_cache = install_download_routes(app,store,service,require_dashboard)
install_daily_routes(app,store,daily_archives,simple_replay,lambda: policy,require_dashboard)
install_workbench_routes(app,workbench,require_dashboard,lambda: policy,lambda symbol:service._live_klines.get(symbol))


@app.get('/api/execution-checks')
def execution_checks():
    return {'checked_at':service.now(),'items':{s:service.execution_snapshot(s) for s in list(service._metadata)}}

@app.get('/api/update/status')
def update_status(force: bool = False):
    """Check the configured private GitHub release with a short bounded cache."""
    return update_service.status(force=force)


@app.get('/api/update/progress')
def update_progress():
    return update_service.progress()


@app.post('/api/update/apply')
def apply_update(request:Request):
    """Schedule only the updater shipped at the application's fixed path."""
    require_dashboard(request)
    try:
        return update_service.schedule_update()
    except UpdateError as error:
        raise HTTPException(409,str(error))


class TelegramPreferencesInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    rule_settings:list[dict[str,Any]]|None = Field(None,max_length=512)
    timeframes:list[Literal['1m','5m','15m','30m','1h','4h','1d','1w','1M']] = Field(default_factory=lambda:['5m','15m'],min_length=1,max_length=9)
    directions:list[Literal['long','short']] = Field(default_factory=lambda:['long','short'],min_length=1,max_length=2)
    min_activity:Literal[0,35,50,65,80,90] = 80
    event_kind:Literal['anomaly','indicator','quant'] = 'anomaly'
    rules:list[str] = Field(default_factory=list,max_length=128)
    patterns:list[Literal['阳阳','阴阴','阳阴','阴阳']] = Field(default_factory=list,max_length=4)
    min_actionability:float = Field(0,ge=0,le=100,allow_inf_nan=False)
    require_book:bool = False
    signal_scope:Literal['activity','qualified'] = 'activity'
    strategies:list[Literal['wt_extreme_cross','nan_strategy1_oversold']] = Field(default_factory=list,max_length=2)
    strategy_mode:Literal['any','all'] = 'any'
    listing_enabled:bool = True
    signals_enabled:bool = False

    @model_validator(mode='after')
    def unique_choices(self):
        normalize_preferences(self.model_dump())
        if len(set(self.timeframes)) != len(self.timeframes):
            raise ValueError('周期不能重复选择')
        if len(set(self.directions)) != len(self.directions):
            raise ValueError('方向不能重复选择')
        if len(set(self.strategies)) != len(self.strategies):
            raise ValueError('策略不能重复选择')
        return self


class TelegramSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid')
    # SecretStr masks validation/debug representations. Format validation is
    # performed after parsing so a 422 response never echoes the raw token.
    bot_token:SecretStr|None = None
    clear_bot_token:bool = False
    preferences:TelegramPreferencesInput|None = None

def _telegram_response(message:str|None=None):
    value = telegram_service.safe_status()
    value['telegram_enabled'] = value['enabled']
    if message:
        value['message'] = message
    return value


@app.get('/api/telegram/settings')
def telegram_settings():
    """Return only redacted connection and subscriber state."""
    return _telegram_response()


@app.put('/api/telegram/settings')
def update_telegram_settings(body:TelegramSettingsUpdate,request:Request):
    require_dashboard(request)
    try:
        if body.bot_token is not None and body.clear_bot_token:
            raise ValueError('不能同时保存和清除 Bot Token')
        if (body.bot_token is not None or body.clear_bot_token) and telegram_service.safe_status()['connected']:
            raise ValueError('修改 Bot Token 前请先断开 Telegram 连接')
        if body.bot_token is not None:
            telegram_service.set_token(body.bot_token.get_secret_value())
        elif body.clear_bot_token:
            telegram_service.clear_token()
        if body.preferences is not None:
            telegram_service.set_preferences(body.preferences.model_dump())
    except (ValueError,TokenStorageError) as error:
        # These messages are fixed in the Telegram module and never contain
        # the submitted secret or a Telegram request URL.
        raise HTTPException(400,str(error))
    return _telegram_response('Telegram 设置已安全保存；保存本身不会发送消息。')


@app.post('/api/telegram/connect')
async def connect_telegram(request:Request):
    require_dashboard(request)
    try:
        result = await telegram_service.connect(issue_pairing=True)
    except TokenStorageError as error:
        raise HTTPException(409,str(error))
    except TelegramError as error:
        status = 409 if str(error) == '请先保存 Bot Token' else 502
        raise HTTPException(status,str(error))
    return {**result,'telegram_enabled':result['enabled'],
            'message':('Telegram 已连接；请在十分钟内打开一次性链接完成绑定。'
                       if result.get('pairing_url') else 'Telegram 已连接且会话已绑定。')}


@app.post('/api/telegram/test')
async def test_telegram(request:Request):
    require_dashboard(request)
    try:
        return {**await telegram_service.send_test(),
                'message':'测试文字已发送；它不是行情或交易信号。'}
    except TelegramError as error:
        raise HTTPException(409,str(error))


@app.post('/api/telegram/admin-menu')
async def refresh_telegram_admin_menu(request:Request):
    """Explicitly push the administrator's persistent and inline menus."""
    require_dashboard(request)
    try:
        result = await telegram_service.refresh_admin_menu()
    except TelegramError as error:
        raise HTTPException(409,str(error))
    return {**result, 'sent_at':int(time.time() * 1000),
            'message':'管理员快捷菜单已发送到 Telegram，可在输入框下方直接点击操作。'}


@app.post('/api/telegram/disconnect')
async def disconnect_telegram(request:Request):
    require_dashboard(request)
    result = await telegram_service.disconnect()
    return {**result,'telegram_enabled':result['enabled'],
            'message':'Telegram 已断开；绑定与筛选设置保留，期间事件不会补发。'}


@app.post('/api/telegram/unpair')
async def unpair_telegram(request:Request):
    require_dashboard(request)
    result = await telegram_service.unpair()
    return {**result,'telegram_enabled':result['enabled'],
            'message':'已解除 Telegram 会话绑定；重新连接可生成新的一次性链接。'}

@app.get('/api/status')
def status():
    counts, updated_at = database_counts()
    telegram_status = telegram_service.safe_status()
    return {**service.status(),'database':counts,'database_counts_updated_at':updated_at,
            'telegram_enabled':telegram_status['enabled'],'telegram':telegram_status,
            'automatic_trading':False,'live_rules':live_rules.status()}

_research_page_lock=asyncio.Lock()

@app.get('/api/research/klines')
async def research_klines(request:Request,symbol:str,interval:Literal['5m','1h','4h','1d'],
                          startTime:int=Query(ge=0),endTime:int=Query(ge=0)):
    require_dashboard(request)
    if not valid_contract_symbol(symbol) or startTime>endTime:
        raise HTTPException(400,'无效的合约或时间范围')
    if not service._running:
        raise HTTPException(503,'行情服务未运行，研究下载暂停')
    # Keep the raw Binance columns intact and serialize research traffic.
    async with _research_page_lock:
        from .collector import MarketAccessError, BinanceRateLimitError
        try:
            return await service.fetch_klines(dict(symbol=symbol,interval=interval,startTime=startTime,endTime=endTime,limit=499))
        except (MarketAccessError,BinanceRateLimitError) as exc:
            raise HTTPException(503,str(exc)) from exc


@app.get('/api/signals')
def signals(mode:Literal['live','demo']='live',limit:int=Query(10000,ge=1,le=10000),include_candles:bool=True,qualified:bool|None=None):
    if mode=='demo':
        rows = demo_rows()
        if not include_candles:
            rows = [{key:value for key,value in row.items() if key!='candles'} for row in rows]
    else:
        rows = store.list_alerts(limit, include_candles=include_candles,qualified=qualified,
                                 schema_version=2 if qualified is True else None,quote_asset='USDT')
    return {'source':mode,'items':rows[:limit],'total':len(rows),
            'telegram_enabled':telegram_service.safe_status()['enabled']}

@app.get('/api/signals/{signal_id}')
def signal_detail(signal_id:str):
    record = store.get_alert(signal_id)
    if record is None:
        raise HTTPException(404,'未找到已保存的实时信号')
    # Alerts are immutable evidence, but older records only stored the original
    # four chart arrays.  Hydrate missing display layers from closed candles at
    # or before the signal cutoff so upgraded charts remain causal and never
    # inspect future bars.  Qualification/trade-plan fields stay untouched.
    chart = (record.get('indicators') or {}).get('chart') or {}
    required_layers = {
        'supertrend', 'flips', 'ema_fast', 'ema_slow', 'levels', 'events',
        'order_blocks', 'fvgs', 'equal_levels', 'zone_bounds',
        'strong_weak', 'swing_points',
    }
    if not record.get('snapshot_expired') and not required_layers.issubset(chart):
        symbol = str(record.get('symbol') or '').upper()
        timeframe = str(record.get('timeframe') or '')
        cutoff = record.get('second_candle_close_time', record.get('close_time'))
        if symbol and timeframe in MONITOR_TIMEFRAMES and isinstance(cutoff, int):
            candles = store.get_candles(symbol, timeframe, limit=1_000, end=cutoff)
            if candles and candles[-1].close_time <= cutoff:
                try:
                    snapshot = Settings(**migrate_saved_settings(record.get('policy_snapshot') or policy.model_dump()))
                    hydrated = indicator_context(candles, snapshot.indicator_config())
                    record = {**record, 'indicators': hydrated,
                              'chart_schema_version': 2,
                              'chart_recomputed_from_closed_history': True}
                except (TypeError, ValueError):
                    # A legacy policy snapshot must never make a saved alert
                    # unreadable.  The UI will disable unavailable old layers.
                    pass
    return {'source':'live','item':record,
            'telegram_enabled':telegram_service.safe_status()['enabled']}

from .market_overview import MarketOverview
market_overview = MarketOverview(store)
from .extra_periods import ExtraPeriods
extra_periods = ExtraPeriods(store,research,service)

class PeriodSelection(BaseModel):
    timeframes: list[str] = Field(default_factory=list,max_length=10)

@app.post("/api/market-periods")
async def market_periods(body:PeriodSelection,request:Request):
    require_dashboard(request)
    try:return extra_periods.select(body.timeframes)
    except ValueError as exc:raise HTTPException(400,str(exc))

@app.get('/api/market-overview')
def market_summary():
    return market_overview.get()

@app.get('/api/universe')
def universe():
    return service.universe_status()

@app.get('/api/listings')
def listings(limit:int=Query(100,ge=1,le=1000)):
    return {'items':store.list_listing_events(limit),'snapshot':store.universe_snapshot_status(),
            'telegram_enabled':telegram_service.safe_status()['enabled'],
            'note':'仅提醒首次观察到的交易所合约状态变化；首次全量快照不误报。新币历史足够后才纳入信号评分。'}

@app.post('/api/connection/retry')
async def retry_connection(request:Request):
    require_dashboard(request)
    await service.retry_connection()
    return {'message':'已重新尝试官方行情连接，请查看系统状态。',
            'telegram_enabled':telegram_service.safe_status()['enabled']}

@app.get('/api/indicators')
def indicators():
    return {'items':indicator_catalog(),'custom_code_execution':False,'safe_expression_language':True,
            'note':'内置指标可用于实时确认；自定义指标使用受限表达式语言，先校验和回测，网页永不执行 Python、Pine 或 JavaScript。'}

class CustomIndicatorSpec(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id:str = Field(min_length=2,max_length=64,pattern=r'^[a-z][a-z0-9_]{1,63}$')
    name:str = Field(min_length=1,max_length=80)
    description:str = Field('',max_length=1000)
    long_expression:str = Field('',max_length=2000)
    short_expression:str = Field('',max_length=2000)

class CustomIndicatorSave(CustomIndicatorSpec):
    status:Literal['draft','validated'] = 'draft'

@app.get('/api/custom-indicators')
def list_custom_indicators():
    return {'items':[],'automatic_live_enable':False,'note':'研究库仅保留 WaveTrend 超买超卖交叉'}

@app.post('/api/custom-indicators/validate')
@app.post('/api/custom-indicators')
def retired_custom_indicator(request:Request):
    require_dashboard(request)
    raise HTTPException(410,'自定义指标已移除，请使用指标策略研究库')

@app.get('/api/chart/{symbol}/{timeframe}')
async def chart(symbol:str,timeframe:str,mode:Literal['live','demo']='live'):
    if timeframe not in MONITOR_TIMEFRAMES or not valid_contract_symbol(symbol.upper()):
        raise HTTPException(400,'无效的交易对或周期')
    if mode=='demo':
        row = next((s for s in demo_rows() if s['symbol']==symbol.upper() and s['timeframe']==timeframe),None)
        return {'items':row['candles'] if row else [],'source':'demo'}
    return {'items':await service.chart(symbol.upper(),timeframe),'source':'live'}

@app.get('/api/settings')
def settings():
    return {**policy.model_dump(),'lookback':{tf:LOOKBACK[tf] for tf in MONITOR_TIMEFRAMES},
            'timeframes':list(MONITOR_TIMEFRAMES),
            'telegram_enabled':telegram_service.safe_status()['enabled'],
            'threshold_priors':{tf:THRESHOLD_PRIORS[tf] for tf in MONITOR_TIMEFRAMES},
            'threshold_prior_status':'unvalidated_prior','policy_hash':policy.fingerprint()}

@app.put('/api/settings')
async def update_settings(settings:Settings,request:Request):
    global policy
    # CORS is not CSRF protection. Require our same-origin JSON client marker too.
    require_dashboard(request)
    cfg = settings.engine()
    temporary = CONFIG_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(settings.model_dump(),ensure_ascii=False,indent=2),'utf-8')
    temporary.replace(CONFIG_FILE)
    service.config = cfg
    policy = settings
    service.candidate_transform = freeze_transform(settings)
    return {**settings.model_dump(),'policy_hash':settings.fingerprint(),
            'telegram_enabled':telegram_service.safe_status()['enabled'],
            'message':'已保存。新规则用于之后的实时判定，历史信号保持原记录。'}

class HistoryDownload(BaseModel):
    model_config = ConfigDict(extra='forbid')
    symbol:str = Field(min_length=2,max_length=40)
    timeframe:Literal['5m','15m']
    months:list[str] = Field(min_length=1,max_length=12)

    @field_validator('symbol')
    @classmethod
    def canonical_contract_symbol(cls,value:str):
        value=value.upper()
        if not valid_contract_symbol(value):
            raise ValueError('合约代码须为2～40个Unicode字母、数字或下划线，不允许路径或其他特殊字符')
        return value

class BacktestRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dataset_id:str = Field(pattern=r'^[a-f0-9]{24}$')
    max_holding_bars:int = Field(5,ge=1,le=100)

@app.get('/api/history')
def history():
    return {'items':[item for item in research.datasets() if item.get('timeframe') in MONITOR_TIMEFRAMES],
            'busy':research.busy,'source':'historical',
            'telegram_enabled':telegram_service.safe_status()['enabled']}

@app.post('/api/history/download')
async def history_download(body:HistoryDownload,request:Request):
    require_dashboard(request)
    try:
        # Validate before scheduling; archive URLs are generated by our trusted
        # helper and cannot be arbitrary URLs or filesystem paths from a client.
        import re
        import calendar
        from datetime import datetime,timezone
        current = datetime.now(timezone.utc).strftime('%Y-%m')
        if any(not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',m) or m>=current for m in body.months):
            raise ValueError('请选择已经结束的月份，最近一年最多十二个月')
        estimated = sum(calendar.monthrange(int(m[:4]),int(m[5:]))[1]*86400000//TIMEFRAME_MS[body.timeframe] for m in set(body.months))
        if estimated>MAX_RESEARCH_BARS:
            raise ValueError(f'数据量超过 {MAX_RESEARCH_BARS:,} 根，请减少月份')
        return research.start('download',body.model_dump(),policy)
    except ValueError as error:
        raise HTTPException(400,str(error))

@app.post('/api/history/import')
async def history_import(request:Request,symbol:str,timeframe:str,format:Literal['csv','json']='csv'):
    require_dashboard(request)
    if timeframe not in MONITOR_TIMEFRAMES:
        raise HTTPException(400,'仅支持 5m 和 15m 历史数据')
    if research.busy:
        raise HTTPException(409,'已有研究任务运行，请等待完成后再导入')
    research.busy = True
    try:
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw)>20_000_000:
                raise HTTPException(413,'导入文件不能超过 20 MB')
        def parse_and_register():
            parser = parse_candles_csv if format=='csv' else parse_candles_json
            candles = parser(raw.decode('utf-8-sig'),symbol.upper(),timeframe)
            return research.register(candles,{'type':'user_import','verified_checksum':False})
        return await asyncio.to_thread(parse_and_register)
    except (ValueError,UnicodeError,TypeError) as error:
        raise HTTPException(400,str(error))
    finally:
        research.busy = False

@app.post('/api/backtests')
async def create_backtest(body:BacktestRequest,request:Request):
    require_dashboard(request)
    try:
        dataset = next((d for d in research.datasets() if d['id']==body.dataset_id),None)
        if dataset is None:
            raise ValueError('请先下载或导入历史数据')
        if dataset.get('timeframe') not in MONITOR_TIMEFRAMES:
            raise ValueError('当前只回测 5m 和 15m 数据')
        return research.start('backtest',body.model_dump(),policy)
    except ValueError as error:
        raise HTTPException(400,str(error))

@app.get('/api/backtests/reports')
def backtest_reports():
    return {'items':research.reports(),'source':'historical',
            'telegram_enabled':telegram_service.safe_status()['enabled']}

@app.get('/api/jobs/{job_id}')
def job_status(job_id:str):
    job = research.get_job(job_id)
    if job is None:
        raise HTTPException(404,'任务不存在')
    return job


def _completed_backtest(job_id: str):
    job = research.get_job(job_id)
    if job is None:
        raise HTTPException(404, '任务不存在')
    if job.get('kind') != 'backtest':
        raise HTTPException(400, '该任务不是回测任务')
    if job.get('state') != 'completed' or not isinstance(job.get('result'), dict):
        raise HTTPException(409, '回测尚未完成')
    return job


@app.get('/api/backtests/{job_id}/summary.csv')
def export_backtest_summary(job_id: str):
    job = _completed_backtest(job_id)
    filename = f'backtest-{job_id}-summary.csv'
    saved = research.root / f'job-{job_id}.summary.csv'
    content = saved.read_bytes() if saved.is_file() else backtest_summary_csv(job)
    return Response(content=content, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/backtests/{job_id}/trades.csv')
def export_backtest_trades(job_id: str):
    job = _completed_backtest(job_id)
    filename = f'backtest-{job_id}-trades.csv'
    saved = research.root / f'job-{job_id}.trades.csv'
    content = saved.read_bytes() if saved.is_file() else backtest_trades_csv(job)
    return Response(content=content, media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


def _saved_entry_csv(job_id: str, kind: Literal['entry-events', 'entry-summary']):
    _completed_backtest(job_id)
    saved = research.root / f'job-{job_id}.{kind}.csv'
    if not saved.is_file():
        raise HTTPException(404, '该回测没有生成双向入场点研究 CSV')
    filename = f'backtest-{job_id}-{kind}.csv'
    return Response(content=saved.read_bytes(), media_type='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/api/backtests/{job_id}/entry-events.csv')
def export_backtest_entry_events(job_id: str):
    return _saved_entry_csv(job_id, 'entry-events')


@app.get('/api/backtests/{job_id}/entry-summary.csv')
def export_backtest_entry_summary(job_id: str):
    return _saved_entry_csv(job_id, 'entry-summary')
