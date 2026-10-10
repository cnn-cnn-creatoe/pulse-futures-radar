"""Exact closed-bar preparation before a research run. No invented gap fills."""
import asyncio

from .daily_journal import local_time
from .engine import TIMEFRAME_MS
from .periods import floor_bar, ceil_bar, shift, count_bars
from .history_data import validate_series
from .store import is_usdt_cached_identity


def missing_ranges(candles, first, last, interval):
    advance = (lambda value: shift(value,interval)) if isinstance(interval,str) else (lambda value:value+interval)
    present = {c.open_time for c in candles if c.closed}
    ranges = []
    begin = None
    value = first
    previous = first
    while value <= last:
        if value not in present:
            if begin is None:
                begin = value
        elif begin is not None:
            ranges.append((begin, previous))
            begin = None
        previous = value
        value = advance(value)
    if begin is not None:
        ranges.append((begin, last))
    return ranges


class HistoricalPreparation:
    def __init__(self, store, research, fetch_page):
        self.store, self.research, self.fetch_page = store, research, fetch_page

    def series(self, symbol, tf, first, last, datasets):
        values = {c.open_time:c for c in self.store.get_candles(symbol,tf,start=first,end=last,limit=200000) if c.closed}
        for meta in datasets:
            if (meta.get("symbol"),meta.get("timeframe")) == (symbol,tf):
                imported,_ = self.research.load(meta["id"])
                for c in imported:
                    if c.closed and first <= c.open_time <= last:
                        values.setdefault(c.open_time,c)
        return sorted(values.values(),key=lambda c:c.open_time)

    async def run(self, job, begin, end):
        options = job["request"]
        datasets = self.research.datasets()
        metadata = {r["symbol"]:r for r in await asyncio.to_thread(self.store.list_universe,False)
            if is_usdt_cached_identity(r["symbol"],r)}
        if options["scope"] == "all":
            inventory = await asyncio.to_thread(self.store.candle_inventory)
            symbols = {r["symbol"] for r in inventory+datasets if is_usdt_cached_identity(r.get("symbol",""))}
            active = {s for s,r in metadata.items() if r.get("snapshot_active")}
            symbols = active if options.get('purpose')=='signals' and active else symbols | active
        else:
            symbols = set(options["symbols"])
        keys = sorted((s,tf) for s in symbols for tf in options["timeframes"])
        if not keys:
            raise ValueError("尚未取得合约列表，请等待行情连接后再试")
        if sum(max(0,(end-begin)//TIMEFRAME_MS[tf]+1) for _,tf in keys)>2_000_000:
            raise ValueError("所选区间超过 200 万根 K 线，请减少币种或缩短日期")
        job["series_keys"] = keys
        progress = {"stage":"checking","checked":0,"total":len(keys),"downloaded":0,"missing":0,"current":""}
        job["preparation"] = progress
        job["logs"].append("先检查所选区间、之前 2,020 根预热 K 线及已发生的后续观察数据；缺口补齐后再计算。")
        for symbol,tf in keys:
            if job.get("cancel_requested"):
                return False
            interval = TIMEFRAME_MS[tf]
            first = shift(floor_bar(begin,tf),tf,-2020)
            last = min(shift(floor_bar(end+1,tf),tf,options["holding_bars"]-1),
                shift(floor_bar(job["as_of"],tf),tf,-1))
            first = max(ceil_bar(0,tf),first)
            onboard = int(metadata.get(symbol,{}).get("onboardDate") or 0)
            if onboard and onboard > first:
                first = floor_bar(onboard,tf) if tf in ("1w","1M") else ceil_bar(onboard,tf)
                job["logs"].append(f"{symbol} 上市前无行情，实际历史从 {local_time(first)} 开始。")
            if first > last:
                if options.get('purpose')=='signals':
                    job['logs'].append(f'{symbol} {tf} 尚无已收盘历史，跳过该组合。')
                    progress['checked']+=1
                    continue
                raise ValueError(f"{symbol} {tf} 所选日期尚无已收盘行情，请调整日期")
            progress.update(stage="checking",current=f"{symbol} · {tf}")
            candles = await asyncio.to_thread(self.series,symbol,tf,first,last,datasets)
            gaps = missing_ranges(candles,first,last,tf)
            progress["missing"] = sum(count_bars(a,b,tf) for a,b in gaps)
            for a,b in gaps:
                cursor = a
                while cursor<=b:
                    if job.get("cancel_requested"):
                        return False
                    page_end = min(b,shift(cursor,tf,498))
                    progress.update(stage="downloading",from_time=cursor,to_time=page_end)
                    if self.fetch_page is None:
                        raise ValueError(f"{symbol} {tf} 历史不完整，行情下载服务尚未连接")
                    rows = await self.fetch_page(symbol,tf,cursor,page_end)
                    usable = [c for c in rows if c.closed and cursor<=c.open_time<=page_end
                        and c.close_time<job["as_of"] and (c.symbol,c.timeframe)==(symbol,tf)]
                    if usable:
                        validate_series(usable,now_ms=job["as_of"])
                        await asyncio.to_thread(self.store.save_candles,usable)
                        progress["downloaded"] += len(usable)
                    # Missing exchange data is actionable, not a successful download.
                    remaining = missing_ranges(usable,cursor,page_end,tf)
                    if remaining:
                        raise ValueError(f"{symbol} {tf} 在 {local_time(remaining[0][0])} 仍缺 K 线，已保存下载部分；请重试或调整日期，尚未开始回测")
                    cursor = shift(page_end,tf)
            progress["stage"] = "verifying"
            candles = await asyncio.to_thread(self.series,symbol,tf,first,last,datasets)
            if missing_ranges(candles,first,last,tf):
                raise ValueError(f"{symbol} {tf} 下载后复核仍有缺口，未开始回测")
            progress["checked"] += 1
        progress.update(stage="ready",missing=0)
        job["logs"].append(f"数据检查通过，共补存 {progress['downloaded']} 根；开始{'计算信号' if options.get('purpose')=='signals' else '回测'}。")
        return True
