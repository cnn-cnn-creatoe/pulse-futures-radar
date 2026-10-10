"""Bounded imports and verified Binance public USD-M historical archives.

Public archives are an offline data product, not a replacement endpoint for a
restricted live feed. A denied archive request fails; no mirror/proxy fallback.
Reference: https://github.com/binance/binance-public-data (2026-08-31).
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
from pathlib import Path
import re
import time
import urllib.error
from urllib.parse import quote
import urllib.request
import zipfile

from .archive_gate import archive_gate
from .periods import floor_bar, shift, count_bars
from .engine import Candle, TIMEFRAME_MS, _valid_prices, _valid_volume

MAX_DOWNLOAD_BYTES = 50_000_000
MAX_UNCOMPRESSED_BYTES = 100_000_000
MAX_IMPORT_BARS = 120_000
ARCHIVE_ORIGIN = "https://data.binance.vision"


def valid_contract_symbol(symbol: object) -> bool:
    """Official Unicode symbols and dated-contract underscores, without paths."""
    return (isinstance(symbol, str) and 2 <= len(symbol) <= 40
            and any(character.isalnum() for character in symbol)
            and all(character.isalnum() or character == "_" for character in symbol))


def _identity(symbol: str, timeframe: str) -> tuple[str, str]:
    # Dated contracts include underscores, e.g. BTCUSDT_260925. All other path,
    # formula and control characters remain invalid across imports and HTTP.
    if not valid_contract_symbol(symbol) or symbol != symbol.upper():
        raise ValueError("symbol must be 2-40 uppercase Unicode letters/numbers or underscores")
    if timeframe not in TIMEFRAME_MS:
        raise ValueError("unsupported timeframe")
    return symbol, timeframe


def validate_series(candles: list[Candle], now_ms: int | None = None) -> dict:
    """Reject malformed, mixed, duplicate, unsorted or unclosed data; report gaps.

    Never fills gaps or silently deduplicates a historical sample.
    """
    if len(candles) > MAX_IMPORT_BARS:
        raise ValueError(f"at most {MAX_IMPORT_BARS:,} candles per import")
    if not candles:
        return {"bars": 0, "gaps": 0, "missing_bars": 0}
    symbol, timeframe = _identity(candles[0].symbol, candles[0].timeframe)
    interval = TIMEFRAME_MS[timeframe]
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    gaps = missing = 0
    for i, candle in enumerate(candles):
        if candle.symbol != symbol or candle.timeframe != timeframe:
            raise ValueError("mixed symbols/timeframes in historical series")
        if (type(candle.open_time) is not int or type(candle.close_time) is not int
                or candle.open_time < 0 or floor_bar(candle.open_time,timeframe) != candle.open_time
                or candle.close_time != shift(candle.open_time,timeframe) - 1):
            raise ValueError(f"invalid millisecond UTC timestamp at row {i + 1}")
        if candle.closed is not True or candle.close_time >= now_ms:
            raise ValueError(f"unclosed/future candle at row {i + 1}")
        if not _valid_prices(candle) or not _valid_volume(candle):
            raise ValueError(f"invalid OHLC/volume at row {i + 1}")
        if i:
            delta = candle.open_time - candles[i - 1].open_time
            if delta <= 0:
                raise ValueError("historical timestamps must be unique and increasing")
            if candle.open_time != shift(candles[i - 1].open_time,timeframe):
                gaps += 1
                missing += count_bars(candles[i - 1].open_time,candle.open_time,timeframe) - 2
    return {"symbol": symbol, "timeframe": timeframe, "bars": len(candles),
            "first_close_time": candles[0].close_time,
            "last_close_time": candles[-1].close_time, "gaps": gaps,
            "missing_bars": missing}


def _integer(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric field")
    # Require an exact integer rather than silently truncating a timestamp.
    number = float(value)
    if not math.isfinite(number) or not number.is_integer():
        raise ValueError("integer field must be finite and integral")
    return int(number)


def _float(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean is not a numeric field")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("numeric field must be finite")
    return number


def parse_candles_csv(content: str, symbol: str, timeframe: str,
                      now_ms: int | None = None) -> list[Candle]:
    """Read official 12-column USD-M kline CSV (optional Binance header)."""
    _identity(symbol, timeframe)
    if len(content.encode("utf-8")) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("CSV exceeds import byte limit")
    candles = []
    for row_number, row in enumerate(csv.reader(io.StringIO(content.lstrip("\ufeff"))), 1):
        if not row:
            continue
        if row_number == 1 and row[0].strip().lower() in {"open_time", "open time"}:
            continue
        if len(row) != 12:
            raise ValueError(f"expected 12 Binance kline columns at row {row_number}")
        try:
            candles.append(Candle(symbol, timeframe, _integer(row[0]), _integer(row[6]),
                                  *[_float(row[i]) for i in (1, 2, 3, 4)],
                                  quote_volume=_float(row[7]), taker_buy_quote=_float(row[10]),
                                  trades=_integer(row[8]), volume=_float(row[5])))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError(f"invalid Binance CSV row {row_number}: {exc}") from exc
        if len(candles) > MAX_IMPORT_BARS:
            raise ValueError("CSV exceeds candle limit")
    validate_series(candles, now_ms)
    return candles


def parse_candles_json(content: str, symbol: str, timeframe: str,
                       now_ms: int | None = None) -> list[Candle]:
    """Read an array of Candle objects; optional identity must match the import."""
    _identity(symbol, timeframe)
    if len(content.encode("utf-8")) > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("JSON exceeds import byte limit")
    rows = json.loads(content)
    if not isinstance(rows, list) or len(rows) > MAX_IMPORT_BARS:
        raise ValueError("JSON must be a bounded array of candle objects")
    allowed = {field.name for field in fields(Candle)}
    candles = []
    for i, row in enumerate(rows):
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError(f"invalid candle fields at row {i + 1}")
        values = {"symbol": symbol, "timeframe": timeframe, **row}
        if values["symbol"] != symbol or values["timeframe"] != timeframe:
            raise ValueError("candle identity differs from import identity")
        for key in ("open_time", "close_time", "trades"):
            if key in values:
                values[key] = _integer(values[key])
        for key in ("open", "high", "low", "close", "quote_volume", "taker_buy_quote", "volume"):
            if key in values:
                values[key] = _float(values[key])
        try:
            candles.append(Candle(**values))
        except TypeError as exc:
            raise ValueError(f"missing candle fields at row {i + 1}") from exc
    validate_series(candles, now_ms)
    return candles


def import_history(path: str | Path, symbol: str, timeframe: str,
                   now_ms: int | None = None) -> list[Candle]:
    """Local-only helper. Web APIs must accept uploads, never arbitrary paths."""
    path = Path(path)
    if path.stat().st_size > MAX_UNCOMPRESSED_BYTES:
        raise ValueError("historical file exceeds import byte limit")
    content = path.read_text(encoding="utf-8-sig")
    if path.suffix.lower() == ".csv":
        return parse_candles_csv(content, symbol, timeframe, now_ms)
    if path.suffix.lower() == ".json":
        return parse_candles_json(content, symbol, timeframe, now_ms)
    raise ValueError("only CSV or JSON history imports are supported")


def _download(url: str, limit: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "PULSE-history/1.0"})
    # Disallow redirects to alternate origins, even if issued by an intermediary.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise ValueError("archive redirect refused; use the official documented source")
    with archive_gate.request():
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
                data = response.read(limit + 1)
        except urllib.error.HTTPError as exc:
            archive_gate.reject(exc.code, exc.headers.get("Retry-After") if exc.headers else None)
            if exc.code in {403, 451}:
                raise ValueError(f"official historical archive denied access (HTTP {exc.code}); no fallback attempted") from exc
            raise ValueError(f"official historical archive unavailable (HTTP {exc.code})") from exc
    if len(data) > limit:
        raise ValueError("archive download exceeds size limit")
    return data


def download_binance_month(symbol: str, timeframe: str, month: str,
                           destination: str | Path) -> dict:
    """Download one monthly public archive, verify SHA256, read CSV in memory.

    Returns candles plus metadata; writes only verified archive/checksum/metadata
    into the supplied local directory. No ZipFile.extract or API credentials.
    """
    _identity(symbol, timeframe)
    if not isinstance(month, str) or not re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", month):
        raise ValueError("month must use YYYY-MM")
    stamp = datetime.strptime(month, "%Y-%m").replace(tzinfo=timezone.utc)
    current = datetime.now(timezone.utc)
    if (stamp.year, stamp.month) >= (current.year, current.month):
        raise ValueError("monthly archives must precede the current UTC month")
    filename = f"{symbol}-{timeframe}-{month}.zip"
    # Encode only path components; the scheme and official origin stay fixed.
    url = f"{ARCHIVE_ORIGIN}/data/futures/um/monthly/klines/{quote(symbol, safe='')}/{timeframe}/{quote(filename, safe='')}"
    checksum = _download(url + ".CHECKSUM", 4096).decode("utf-8-sig").strip()
    parts = checksum.split()
    if (len(parts) != 2 or not re.fullmatch(r"[a-fA-F0-9]{64}", parts[0])
            or parts[1].lstrip("*") != filename):
        raise ValueError("invalid official CHECKSUM content")
    target = Path(destination).resolve()
    target.mkdir(parents=True, exist_ok=True)
    archive_path = target / filename
    cache_hit = False
    if archive_path.is_file() and archive_path.stat().st_size <= MAX_DOWNLOAD_BYTES:
        cached = archive_path.read_bytes()
        if hashlib.sha256(cached).hexdigest() == parts[0].lower():
            data = cached
            cache_hit = True
        else:
            data = _download(url, MAX_DOWNLOAD_BYTES)
    else:
        data = _download(url, MAX_DOWNLOAD_BYTES)
    digest = hashlib.sha256(data).hexdigest()
    if digest != parts[0].lower():
        raise ValueError("historical archive SHA256 verification failed")
    csv_name = filename[:-4] + ".csv"
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        members = archive.infolist()
        if len(members) != 1 or members[0].filename != csv_name:
            raise ValueError("expected exactly the matching CSV member in archive")
        if members[0].file_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("uncompressed CSV exceeds size limit")
        candles = parse_candles_csv(archive.read(csv_name).decode("utf-8-sig"), symbol, timeframe)
    metadata = {**validate_series(candles), "source": "Binance public USD-M monthly archive",
                "source_url": url, "month": month, "sha256": digest, "checksum_verified": True,
                "downloaded_at": datetime.now(timezone.utc).isoformat(), "live_feed": False,
                "cache_hit": cache_hit}
    archive_path.write_bytes(data)
    (target / (filename + ".CHECKSUM")).write_text(checksum + "\n", encoding="utf-8")
    (target / (filename + ".metadata.json")).write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"candles": candles, "metadata": metadata, "archive_path": str(archive_path)}
