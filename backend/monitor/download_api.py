"""Observable historical downloads. Every displayed candle comes from saved data."""
import csv
import io
import logging
import threading
import time
from urllib.parse import quote
from fastapi import HTTPException, Query, Request, Response
from .engine import LOOKBACK, MONITOR_TIMEFRAMES, TIMEFRAME_MS
from .history_data import valid_contract_symbol
from .store import is_usdt_cached_identity


LOG = logging.getLogger(__name__)
INVENTORY_TTL_SECONDS = 8.0


class InventoryCache:
    """Serve the last exact inventory while one independent scan refreshes it."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.Lock()
        self._rows = None
        self._metadata = {}
        self._checked_at = 0.0
        self._updated_at = None
        self._refreshing = False
        self._closing = False
        self._thread = None
        self._error = None
        self._retry_after = 0.0
        self._background_cold = False

    def _claim_refresh(self) -> bool:
        with self._lock:
            if (self._closing or self._refreshing
                    or time.monotonic() < self._retry_after):
                return False
            self._refreshing = True
            return True

    def _refresh(self) -> None:
        try:
            rows = self.store.candle_inventory()
            metadata = {row['symbol']: row for row in self.store.list_universe(active_only=False)}
        except Exception as error:
            LOG.warning('download inventory refresh failed: %s', error)
            with self._lock:
                self._checked_at = time.monotonic()
                self._error = str(error)[:300]
                self._retry_after = self._checked_at + INVENTORY_TTL_SECONDS
        else:
            with self._lock:
                self._rows = list(rows)
                self._metadata = dict(metadata)
                self._checked_at = time.monotonic()
                self._updated_at = int(time.time() * 1000)
                self._error = None
                self._retry_after = 0.0
        finally:
            with self._lock:
                self._refreshing = False
                self._thread = None

    def start(self) -> None:
        """Start one background refresh without waiting for the full scan."""
        thread = threading.Thread(target=self._refresh, name='download-inventory-refresh', daemon=True)
        with self._lock:
            self._background_cold = True
            if (self._closing or self._refreshing
                    or time.monotonic() < self._retry_after):
                return
            self._refreshing = True
            self._thread = thread
        try:
            thread.start()
        except Exception as error:
            with self._lock:
                self._refreshing = False
                self._thread = None
                self._error = str(error)[:300]
                self._retry_after = time.monotonic() + INVENTORY_TTL_SECONDS
            LOG.warning('download inventory refresh could not start: %s', error)

    def get(self):
        """Return immediately when cached; cold local/test callers seed once."""
        with self._lock:
            ready = self._rows is not None
            refreshing = self._refreshing
            background_cold = self._background_cold
            stale = ready and time.monotonic() - self._checked_at >= INVENTORY_TTL_SECONDS
        if not ready and not refreshing and background_cold:
            self.start()
        elif not ready and not refreshing and self._claim_refresh():
            # Production lifespan starts a worker before requests are accepted.
            # This exact cold fallback preserves deterministic standalone use.
            self._refresh()
        elif stale:
            self.start()
        with self._lock:
            rows = list(self._rows or [])
            metadata = dict(self._metadata)
            state = {
                'ready': self._rows is not None,
                'refreshing': self._refreshing,
                'error': self._error,
            }
            return rows, self._updated_at, metadata, state

    def close(self) -> None:
        """Fence new work and wait before the shared Store is closed."""
        with self._lock:
            self._closing = True
            thread = self._thread
        if thread is not None:
            thread.join()


def install_download_routes(app, store, service, require_dashboard):
    inventory_cache = InventoryCache(store)
    app.state.download_inventory_cache = inventory_cache

    def inventory():
        # A retained-history GROUP BY can take seconds. Requests always receive
        # the last exact snapshot while a single background reader refreshes it.
        return inventory_cache.get()

    def identity(symbol, timeframe=None):
        symbol=symbol.upper()
        # Chinese names and delivery suffixes share the exact same safe identity
        # rule as chart/history routes; paths, formulas and controls stay invalid.
        if not valid_contract_symbol(symbol) or (timeframe is not None and timeframe not in TIMEFRAME_MS):
            raise HTTPException(400,'无效的合约或周期')
        return symbol

    def active_identity(symbol, timeframe=None):
        symbol=identity(symbol,timeframe)
        if timeframe is not None and timeframe not in MONITOR_TIMEFRAMES:
            raise HTTPException(400,'当前只下载和监测 5m、15m 周期')
        return symbol

    @app.get('/api/downloads')
    def downloads(symbol:str|None=None,timeframe:str|None=None,after:int|None=Query(None,ge=0)):
        if symbol:
            symbol=active_identity(symbol,timeframe)
        elif timeframe is not None and timeframe not in MONITOR_TIMEFRAMES:
            raise HTTPException(400,'无效的周期')
        saved,checked_at,cached_metadata,inventory_state=inventory()
        # Scope can exceed 5,000 contract/period pairs. Request the collector's
        # bounded maximum so real jobs are never relabelled cached.
        snapshot=service.download_status(symbol=symbol,timeframe=timeframe,limit=10000,after=after)
        scope_verified=bool(snapshot.get('summary',{}).get('scope_verified'))
        scope_symbols=set(service._metadata.copy()) if scope_verified else set()
        def included(symbol):
            return symbol in scope_symbols if scope_verified else is_usdt_cached_identity(symbol,cached_metadata.get(symbol))
        by_pair={(row['symbol'],row['timeframe']):row for row in saved}
        jobs={(row['symbol'],row['timeframe']):row for row in snapshot.get('items',[])
              if included(row['symbol']) and row['timeframe'] in MONITOR_TIMEFRAMES}
        # If discovery is temporarily unavailable, preserve the user's stored
        # inventory. Cached data is explicitly not a verified live subscription.
        for key,row in by_pair.items():
            if scope_verified:
                continue  # Old USDC/other excluded history is never an active job.
            if key[1] not in MONITOR_TIMEFRAMES:
                continue  # Other retained periods are archives, not active jobs.
            if not included(key[0]):
                continue  # Startup cache display also respects the requested USDT quote.
            if symbol and key[0]!=symbol or timeframe and key[1]!=timeframe:
                continue
            if key not in jobs:
                jobs[key]={'symbol':key[0],'timeframe':key[1],'state':'cached',
                    'bars':row['bars'],'minimum':LOOKBACK[key[1]]['minimum_bars']+3,
                    'target_bars':LOOKBACK[key[1]]['days']*86400000//TIMEFRAME_MS[key[1]]+3,
                    'progress_pct':None,'error':None,'scope_verified':False}
        items=[]
        for key,job in jobs.items():
            row=by_pair.get(key,{})
            item={**job,'stored_bars':row.get('bars',0),'stored_first_open_time':row.get('first_open_time'),
                'stored_last_close_time':row.get('last_close_time'),'stored_last_price':row.get('last_price')}
            # Candles are delivered on demand and in the bounded page-event
            # stream. Keep one current quote per symbol without reviving 1m as
            # a formal dashboard/download period.
            item.pop('latest_candle',None)
            item.pop('market_quote',None)
            if key[1]!=MONITOR_TIMEFRAMES[0]:
                item.pop('live_quote',None)
            items.append(item)
        stored=[row for row in saved if included(row['symbol']) and row['timeframe'] in MONITOR_TIMEFRAMES]
        outside=[row for row in saved if not (included(row['symbol']) and row['timeframe'] in MONITOR_TIMEFRAMES)]
        events=[event for event in snapshot.get('events',[])
                if (event.get('symbol') is None or included(event['symbol']))
                and event.get('timeframe') in (None, *MONITOR_TIMEFRAMES)]
        return {**snapshot,'items':items,'items_total':len(items),'scope_items_total':snapshot.get('items_total',len(jobs)) if scope_verified else 0,'summary':{**snapshot.get('summary',{}),
            'stored_candles':sum(row['bars'] for row in stored),'stored_pairs':len(stored),
            'stored_symbols':len({row['symbol'] for row in stored}),
            'retained_candles':sum(row['bars'] for row in saved),'retained_pairs':len(saved),
            'retained_symbols':len({row['symbol'] for row in saved}),
            'outside_scope_candles':sum(row['bars'] for row in outside),
            'outside_scope_pairs':len(outside),
            'outside_scope_symbols':len({row['symbol'] for row in outside}),
            'inventory_scope':'current_verified_scope' if scope_verified else 'unverified_usdt_cache',
            'inventory_note':'仅当前已核验的 USDT 合约 5m / 15m 计入下载与库存；其他旧周期和范围外历史保留，可按合约导出。' if scope_verified else '范围尚未核验；仅展示本地 USDT 的 5m / 15m 缓存，不表示当前监测范围；其他旧缓存保留但不列入下载。'},
            'events':events,
            'inventory_updated_at':checked_at,'inventory_refresh_seconds':INVENTORY_TTL_SECONDS,
            'inventory_ready':inventory_state['ready'],'inventory_refreshing':inventory_state['refreshing'],
            'inventory_error':inventory_state['error'],
            'source':'live','telegram_enabled':service.telegram_enabled(),'automatic_trading':False}

    @app.get('/api/downloads/{symbol}/{timeframe}')
    def detail(symbol:str,timeframe:str,limit:int=Query(20,ge=1,le=100)):
        symbol=identity(symbol,timeframe)
        data=service.download_status(symbol=symbol,timeframe=timeframe,limit=1) if timeframe in MONITOR_TIMEFRAMES else {'items':[]}
        job=next(iter(data.get('items',[])),None)
        stats=store.candle_window_stats(symbol,timeframe)
        candles=store.get_candles(symbol,timeframe,limit)
        return {'symbol':symbol,'timeframe':timeframe,'job':job,'inventory':stats,
            'candles':[c.to_dict() for c in reversed(candles)],
            'live_quote':job.get('live_quote') if job else None,
            'server_time':service.now(),'source':'live','telegram_enabled':service.telegram_enabled(),
            'note':'下方K线已收盘且已写入本机数据库；实时未收盘报价单独展示，不参与信号判断。'}

    @app.get('/api/downloads/{symbol}/{timeframe}/export')
    def export(symbol:str,timeframe:str,limit:int=Query(10000,ge=1,le=60000)):
        symbol=identity(symbol,timeframe)
        candles=store.get_candles(symbol,timeframe,limit)
        buffer=io.StringIO(newline='')
        writer=csv.writer(buffer)
        writer.writerow(['symbol','timeframe','open_time','close_time','open','high','low','close','volume','quote_volume','taker_buy_quote','trades','closed'])
        for c in candles:
            writer.writerow([c.symbol,c.timeframe,c.open_time,c.close_time,c.open,c.high,c.low,c.close,c.volume,c.quote_volume,c.taker_buy_quote,c.trades,True])
        filename=f'{symbol}-{timeframe}-saved-candles.csv'
        fallback=filename if filename.isascii() else f'candles-{timeframe}.csv'
        return Response(content='\ufeff'+buffer.getvalue(),media_type='text/csv; charset=utf-8',headers={
            'Content-Disposition':f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}',
            'X-Candle-Count':str(len(candles))})

    @app.post('/api/downloads/resume')
    async def resume(request:Request):
        require_dashboard(request)
        result=await service.retry_connection()
        return {'message':'已继续行情下载；保留已有K线，只补缺失区间。','state':result.get('state'),
            'telegram_enabled':service.telegram_enabled()}

    return inventory_cache
