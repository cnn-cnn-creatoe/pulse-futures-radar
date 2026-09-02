"""Resumable official-archive backtests for the latest 12 completed months.

This batch runner is deliberately separate from the live SQLite collector.  It
keeps a manifest after every verified monthly archive and every completed
symbol/timeframe, so a stopped all-universe run resumes instead of starting
again.  It never sends Telegram messages or places orders.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import time

from .backtest import run_backtest
from .engine import MONITOR_TIMEFRAMES
from .history_data import download_binance_month, valid_contract_symbol, validate_series
from .policy import Settings
from .research import backtest_summary_csv, backtest_trades_csv, compact_backtest_result


def month_range(first: str, last: str) -> list[str]:
    if not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", first or "") or not re.fullmatch(
            r"20\d{2}-(0[1-9]|1[0-2])", last or ""):
        raise ValueError("月份须使用 YYYY-MM")
    year, month = map(int, first.split("-"))
    end_year, end_month = map(int, last.split("-"))
    result = []
    while (year, month) <= (end_year, end_month):
        result.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year, month = year + 1, 1
        if len(result) > 12:
            raise ValueError("最近一年最多 12 个完整月")
    if not result:
        raise ValueError("结束月份不能早于开始月份")
    current = datetime.now(timezone.utc).strftime("%Y-%m")
    if result[-1] >= current:
        raise ValueError("只能使用当前 UTC 月之前的完整月份")
    return result


def _atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
    temporary.replace(path)


class YearBatch:
    def __init__(self, root: str | Path, symbols: list[str], timeframes: list[str],
                 months: list[str], settings: Settings | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.archives = self.root / "archives"
        normalized = []
        for symbol in symbols:
            symbol = symbol.strip().upper()
            if not valid_contract_symbol(symbol):
                raise ValueError(f"无效合约代码：{symbol}")
            if symbol not in normalized:
                normalized.append(symbol)
        if not normalized:
            raise ValueError("至少需要一个合约代码")
        if not timeframes or any(value not in MONITOR_TIMEFRAMES for value in timeframes):
            raise ValueError("周期只允许 5m、15m")
        if not 1 <= len(months) <= 12 or months != sorted(set(months)):
            raise ValueError("月份需按顺序提供且不得重复，最多 12 个")
        self.symbols, self.timeframes, self.months = normalized, list(dict.fromkeys(timeframes)), months
        self.settings = settings or Settings()
        self.manifest_path = self.root / "batch.manifest.json"
        self.manifest = self._load_manifest()

    def _load_manifest(self):
        identity = {"version": 1, "symbols": self.symbols, "timeframes": self.timeframes,
                    "months": self.months, "policy_hash": self.settings.fingerprint()}
        if self.manifest_path.exists():
            saved = json.loads(self.manifest_path.read_text("utf-8"))
            if any(saved.get(key) != value for key, value in identity.items()):
                raise ValueError("现有批次清单的标的、周期、月份或策略不同，请使用新的输出目录")
            return saved
        items = {f"{symbol}:{timeframe}": {"symbol": symbol, "timeframe": timeframe,
                  "state": "pending", "downloaded_months": [], "updated_at": None}
                 for symbol in self.symbols for timeframe in self.timeframes}
        manifest = {**identity, "state": "pending", "created_at": int(time.time() * 1000),
                    "updated_at": int(time.time() * 1000), "items": items}
        _atomic_json(self.manifest_path, manifest)
        return manifest

    def _save(self):
        self.manifest["updated_at"] = int(time.time() * 1000)
        states = [item["state"] for item in self.manifest["items"].values()]
        self.manifest["state"] = ("completed" if states and all(value == "completed" for value in states)
                                  else "running")
        _atomic_json(self.manifest_path, self.manifest)

    def run(self):
        self.manifest["state"] = "running"
        self._save()
        for key, item in self.manifest["items"].items():
            if item.get("state") == "completed":
                print(f"[跳过] {key} 已完成", flush=True)
                continue
            try:
                self._run_item(key, item)
            except Exception as error:
                item.update(state="failed", error=str(error)[:500], updated_at=int(time.time() * 1000))
                self._save()
                print(f"[失败] {key}: {error}", flush=True)
                continue
        self._save()
        return self.manifest

    def _run_item(self, key, item):
        symbol, timeframe = item["symbol"], item["timeframe"]
        item.update(state="downloading", error=None, updated_at=int(time.time() * 1000))
        self._save()
        candles, sources = [], []
        for index, month in enumerate(self.months, 1):
            data = download_binance_month(symbol, timeframe, month, self.archives)
            candles.extend(data["candles"])
            sources.append(data["metadata"])
            downloaded = item.setdefault("downloaded_months", [])
            if month not in downloaded:
                downloaded.append(month)
            item.update(downloaded_bars=len(candles), download_progress=index / len(self.months),
                        last_month=month, cache_hit=data["metadata"].get("cache_hit", False),
                        updated_at=int(time.time() * 1000))
            self._save()
            print(f"[下载] {key} {index}/{len(self.months)} {month}，累计 {len(candles):,} 根"
                  f"{'（缓存）' if data['metadata'].get('cache_hit') else ''}", flush=True)
        dataset = validate_series(candles)
        if dataset["gaps"]:
            raise ValueError(f"官方月档案合并后存在 {dataset['missing_bars']} 根缺口")
        item.update(state="backtesting", bars=len(candles), updated_at=int(time.time() * 1000))
        self._save()
        print(f"[回测] {key} 开始，{len(candles):,} 根，单指标后组合，含成本与时间外验证", flush=True)
        started = time.perf_counter()
        last_printed = -1
        def replay_progress(update):
            nonlocal last_printed
            item.update(backtest_stage=update["stage"],
                        backtest_processed_bars=update["processed_bars"],
                        backtest_total_bars=update["total_bars"],
                        backtest_progress=update["progress"], updated_at=int(time.time() * 1000))
            self._save()
            percent = int(update["progress"] * 100)
            if percent // 10 != last_printed // 10:
                last_printed = percent
                print(f"[回测] {key} {percent}%（{update['processed_bars']:,}/{update['total_bars']:,}）",
                      flush=True)
        result = run_backtest(
            candles, self.settings.engine(), self.settings.indicator_config(),
            self.settings.quality.model_dump(),
            {"max_holding_bars": 5, "compare_thresholds": True,
             "threshold_profile": self.settings.threshold_profile,
             "fee_bps_per_side": self.settings.quality.fee_bps_per_side,
             "slippage_bps_per_side": self.settings.quality.slippage_bps_per_side},
            progress=replay_progress)
        elapsed = time.perf_counter() - started
        result["provenance"] = {"symbol": symbol, "timeframe": timeframe,
            "provenance": {"type": "binance_official_archive", "verified_checksum": True,
                           "sources": sources}, "bars": len(candles)}
        result["policy_snapshot"] = self.settings.model_dump()
        result["policy_hash"] = self.settings.fingerprint()
        stem = f"{symbol}-{timeframe}-{self.months[0]}_{self.months[-1]}"
        report_path = self.root / f"{stem}.report.json"
        summary_path = self.root / f"{stem}.summary.csv"
        trades_path = self.root / f"{stem}.trades.csv"
        wrapper = {"kind": "backtest", "state": "completed", "result": result}
        summary_path.write_bytes(backtest_summary_csv(wrapper).encode("utf-8"))
        trades_path.write_bytes(backtest_trades_csv(wrapper).encode("utf-8"))
        compact_backtest_result(result)
        result["exports"] = {"summary_csv": str(summary_path), "trades_csv": str(trades_path),
                             "encoding": "UTF-8 with BOM"}
        _atomic_json(report_path, wrapper)
        item.update(state="completed", elapsed_seconds=elapsed, report=str(report_path),
                    summary_csv=str(summary_path), trades_csv=str(trades_path),
                    updated_at=int(time.time() * 1000))
        self._save()
        print(f"[完成] {key} 用时 {elapsed:.1f} 秒；CSV 已写入 {summary_path.name}", flush=True)


def _settings(path):
    if not path:
        return Settings()
    return Settings(**json.loads(Path(path).read_text("utf-8")))


def main(argv=None):
    parser = argparse.ArgumentParser(description="最近一年 Binance USD-M 5m/15m 可恢复回测")
    parser.add_argument("--symbols", help="逗号分隔，例如 BTCUSDT,ETHUSDT")
    parser.add_argument("--symbols-file", help="每行一个合约代码")
    parser.add_argument("--timeframes", default="5m,15m")
    parser.add_argument("--first-month", required=True)
    parser.add_argument("--last-month", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--settings")
    args = parser.parse_args(argv)
    symbols = [value for value in (args.symbols or "").split(",") if value.strip()]
    if args.symbols_file:
        symbols.extend(line.strip() for line in Path(args.symbols_file).read_text("utf-8-sig").splitlines()
                       if line.strip())
    runner = YearBatch(args.output, symbols,
                       [value.strip() for value in args.timeframes.split(",") if value.strip()],
                       month_range(args.first_month, args.last_month), _settings(args.settings))
    manifest = runner.run()
    return 0 if manifest["state"] == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
