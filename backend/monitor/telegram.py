"""Telegram notification transport, secure pairing, and subscriber filters.

The module deliberately has no market-data reader.  It accepts only events
handed to :meth:`TelegramService.enqueue` while the runtime is connected, so
starting or reconnecting the bot can never replay rows from SQLite history.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import inspect
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from typing import Any, Callable

import aiohttp


BOT_TOKEN_ENV = "PULSE_TELEGRAM_BOT_TOKEN"
BOT_TOKEN_PATTERN = re.compile(r"^[0-9]{6,16}:[A-Za-z0-9_-]{30,128}$")
from .selected_period_rules import TIMEFRAMES
ANOMALY_TIMEFRAMES=('5m','15m')
DIRECTIONS = ("long", "short")
ACTIVITY_LEVELS = (0, 35, 50, 65, 80, 90)
STRATEGIES = ("wt_extreme_cross", "nan_strategy1_oversold")
STRATEGY_MODES = ("any", "all")
PAIRING_TTL_SECONDS = 600
MAX_CALLBACK_BYTES = 64
LOCAL_TIMEZONE = timezone(timedelta(hours=8))

DEFAULT_PREFERENCES: dict[str, Any] = {
    "timeframes": ["5m", "15m"],
    "directions": ["long", "short"],
    "min_activity": 80,
    # activity: explicit user activity/strategy filter; qualified: require the
    # complete versioned quality gate as well.  The label and message evidence
    # always make this distinction visible.
    "signal_scope": "activity",
    "strategies": [],
    "strategy_mode": "any",
    "event_kind": "anomaly",
    "rules": [],
    "patterns": [],
    "min_actionability": 0,
    "require_book": False,
    "listing_enabled": True,
    # Binding proves chat ownership but does not itself opt into market signal
    # delivery.  The user must explicitly enable signals in the menu or page.
    "signals_enabled": False,
}

QUICK_MENU_SETTINGS = "⚙️ 推送设置"
QUICK_MENU_PENDING = "🔔 申请列表"
QUICK_MENU_USERS = "👥 授权用户"
QUICK_MENU_RESET = "↩️ 重置筛选"
QUICK_MENU_COMMANDS = {
    QUICK_MENU_SETTINGS: ("menu", None),
    QUICK_MENU_PENDING: ("pending", None),
    QUICK_MENU_USERS: ("users", None),
    QUICK_MENU_RESET: ("reset", None),
}

STRATEGY_LABELS = {
    "wt_extreme_cross": "WaveTrend · 超买超卖交叉",
    "nan_strategy1_oversold": "楠策略1超买超卖",
}
CONTRACT_TYPE_LABELS = {
    "PERPETUAL": "永续合约",
    "TRADIFI_PERPETUAL": "传统金融永续合约",
    "TRADFI_PERPETUAL": "传统金融永续合约",
    "CURRENT_QUARTER": "当季交割合约",
    "NEXT_QUARTER": "次季交割合约",
    "CURRENT_QUARTER_DELIVERING": "当季交割合约（交割中）",
    "NEXT_QUARTER_DELIVERING": "次季交割合约（交割中）",
}
UNDERLYING_TYPE_LABELS = {
    "COIN": "加密资产",
    "INDEX": "指数",
    "STOCK": "股票",
    "EQUITY": "股票",
    "COMMODITY": "商品",
    "CN_EQUITY": "A 股",
    "HK_EQUITY": "港股",
    "KR_EQUITY": "韩股",
    "PREMARKET": "盘前市场",
}
TRADING_STATUS_LABELS = {
    "TRADING": "交易中",
    "PENDING_TRADING": "待开放交易",
    "PRE_TRADING": "交易前",
    "SETTLING": "结算中",
    "CLOSE": "已关闭",
}


class TelegramError(RuntimeError):
    """A user-safe Telegram error which never contains a bot token or URL."""


class TokenStorageError(TelegramError):
    pass


def validate_bot_token(value: str) -> str:
    token = value.strip() if isinstance(value, str) else ""
    if not BOT_TOKEN_PATTERN.fullmatch(token):
        raise ValueError("Bot Token 格式无效")
    return token


def migrate_preferences(value):
    """Prune stored retired subscriptions without enabling broader notifications."""
    p=deepcopy(value or {})
    old=p.get('strategies',[])
    removed=any(r not in ('wt_extreme_cross','nan_strategy1_oversold') for r in old)
    p['strategies']=[r for r in old if r in ('wt_extreme_cross','nan_strategy1_oversold')]
    saved=p.get('rule_settings')
    if saved is not None:
        p['rule_settings']=[r for r in saved if r.get('kind')=='indicator' and r.get('rule_id') in ('wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure')]
        if saved and not p['rule_settings']:p['signals_enabled']=False
    rules=p.get('rules',[])
    if p.get('event_kind')=='quant':
        p.update(event_kind='indicator',rules=[],rule_settings=[],signals_enabled=False)
    else:
        p['rules']=[r for r in rules if r in ('wt_extreme_cross','nan_strategy1_oversold','smc_equity_structure')]
        if rules and not p['rules'] and saved is None:p.update(rule_settings=[],signals_enabled=False)
    if removed and not p['strategies'] and saved is None:p['signals_enabled']=False
    return p


def normalize_preferences(value: dict[str, Any] | None) -> dict[str, Any]:
    """Return a canonical copy and reject ambiguous or unsupported filters."""
    provided = dict(value or {})
    # One pre-release build used signal_mode.  Read it once for migration, but
    # every write and every API response uses the canonical signal_scope key.
    if "signal_scope" not in provided and provided.get("signal_mode") in {"activity", "qualified"}:
        provided["signal_scope"] = provided["signal_mode"]
    provided.pop("signal_mode", None)
    source = {**DEFAULT_PREFERENCES, **provided}
    timeframes = list(dict.fromkeys(source.get("timeframes") or []))
    directions = list(dict.fromkeys(source.get("directions") or []))
    strategies = list(dict.fromkeys(source.get("strategies") or []))
    if not timeframes or any(item not in TIMEFRAMES for item in timeframes):
        raise ValueError("周期至少选择一个；支持1m、5m、15m、30m、1h、4h、1d、1w、1M")
    if not directions or any(item not in DIRECTIONS for item in directions):
        raise ValueError("方向至少选择一个，且仅支持 long、short")
    if source.get("min_activity") not in ACTIVITY_LEVELS:
        raise ValueError("最低活跃度仅支持 0、35、50、65、80、90（原分）")
    if source.get("signal_scope") not in {"activity", "qualified"}:
        raise ValueError("signal_scope 仅支持 activity、qualified")
    if any(item not in STRATEGIES for item in strategies):
        raise ValueError("仅支持 WaveTrend 超买超卖交叉")
    from .live_rule_signals import rule_catalog
    kind=source.get('event_kind')
    if kind not in ('anomaly','indicator','quant'):raise ValueError('请选择异常、指标或策略事件')
    if kind=='anomaly' and any(tf not in ANOMALY_TIMEFRAMES for tf in timeframes):raise ValueError('两K异常仅支持5m、15m；其他周期请选择指标或策略事件')
    allowed={r['id'] for r in rule_catalog(kind)}
    rules=list(dict.fromkeys(source.get('rules') or []))
    patterns=list(dict.fromkeys(source.get('patterns') or []))
    if any(r not in allowed for r in rules):raise ValueError('仅可选择已接入的事件规则')
    if any(p not in ('阳阳','阴阴','阳阴','阴阳') for p in patterns):raise ValueError('未知形态')
    minimum=source.get('min_actionability')
    if isinstance(minimum,bool) or not isinstance(minimum,(int,float)) or not 0<=minimum<=100:raise ValueError('可操作度门槛应为0至100')
    if source.get("strategy_mode") not in STRATEGY_MODES:
        raise ValueError("strategy_mode 仅支持 any、all")
    from .delivery_profiles import normalize_profiles
    return {
        "rule_settings":normalize_profiles(source.get('rule_settings')),
        "timeframes": [item for item in TIMEFRAMES if item in timeframes],
        "directions": [item for item in DIRECTIONS if item in directions],
        "event_kind":kind,"rules":rules,"patterns":patterns,"min_actionability":minimum,"require_book":bool(source.get("require_book")),
        "min_activity": int(source["min_activity"]),
        "signal_scope": source["signal_scope"],
        # [] is the explicit empty strategy.  It is never stored alongside an
        # indicator, avoiding a redundant "raw OR indicator" interpretation.
        "strategies": [item for item in STRATEGIES if item in strategies],
        "strategy_mode": source["strategy_mode"],
        "listing_enabled": bool(source.get("listing_enabled")),
        "signals_enabled": bool(source.get("signals_enabled")),
    }


def reset_filter_preferences(value: dict[str, Any] | None) -> dict[str, Any]:
    """Reset filters while preserving the user's two delivery opt-ins."""
    current = normalize_preferences(value)
    if current.get("rule_settings") is not None:
        return normalize_preferences({**current, "rule_settings": [
            {**row, "min_activity": 0, "min_actionability": 0, "patterns": [], "symbols": []}
            for row in current["rule_settings"]
        ]})
    return normalize_preferences({
        "timeframes": ["5m", "15m"],
        "directions": ["long", "short"],
        "min_activity": 65,
        "signal_scope": "activity",
        "strategies": [],
        "strategy_mode": "any",
        "signals_enabled": current["signals_enabled"],
        "listing_enabled": current["listing_enabled"],
    })


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))), buffer


def _crypt_protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise TokenStorageError("当前系统只允许通过环境变量配置 Bot Token")
    source, keepalive = _blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    # CRYPTPROTECT_UI_FORBIDDEN: a service must never open a credential dialog.
    if not crypt32.CryptProtectData(ctypes.byref(source), "PULSE Telegram Bot Token", None,
                                    None, None, 0x1, ctypes.byref(target)):
        raise TokenStorageError("Bot Token 无法使用当前 Windows 账户加密")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
        del keepalive


def _crypt_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise TokenStorageError("当前系统只允许通过环境变量配置 Bot Token")
    source, keepalive = _blob(data)
    target = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                      0x1, ctypes.byref(target)):
        raise TokenStorageError("Bot Token 无法由当前 Windows 账户解密")
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)
        del keepalive


class BotTokenVault:
    """Load from env, or persist only a Windows-DPAPI ciphertext."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def source(self) -> str | None:
        if os.getenv(BOT_TOKEN_ENV):
            return "environment"
        return "windows_dpapi" if self.path.exists() else None

    def configured(self) -> bool:
        return self.source() is not None

    def load(self) -> str | None:
        environmental = os.getenv(BOT_TOKEN_ENV)
        if environmental:
            try:
                return validate_bot_token(environmental)
            except ValueError as error:
                raise TokenStorageError("环境变量中的 Bot Token 格式无效") from error
        if not self.path.exists():
            return None
        try:
            token = _crypt_unprotect(self.path.read_bytes()).decode("utf-8")
            return validate_bot_token(token)
        except TokenStorageError:
            raise
        except Exception as error:
            raise TokenStorageError("已保存的 Bot Token 无法安全读取") from error

    def save(self, token: str) -> None:
        if os.getenv(BOT_TOKEN_ENV):
            raise TokenStorageError(
                f"Bot Token 当前来自环境变量 {BOT_TOKEN_ENV}，网页保存不会覆盖；请在进程外修改后重启")
        token = validate_bot_token(token)
        ciphertext = _crypt_protect(token.encode("utf-8"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            temporary.write_bytes(ciphertext)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            temporary.replace(self.path)
        finally:
            if temporary.exists():
                temporary.unlink()

    def clear(self) -> bool:
        if os.getenv(BOT_TOKEN_ENV):
            raise TokenStorageError(f"Bot Token 来自环境变量 {BOT_TOKEN_ENV}，请在进程外清除")
        if not self.path.exists():
            return False
        self.path.unlink()
        return True


class TelegramConfigStore:
    """Non-secret runtime configuration; the token is intentionally absent."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._value = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"enabled": False, "preferences": deepcopy(DEFAULT_PREFERENCES)}
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            return {"enabled": raw.get("enabled") is True,
                    "preferences": normalize_preferences(migrate_preferences(raw.get("preferences")))}
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"enabled": False, "preferences": deepcopy(DEFAULT_PREFERENCES)}

    def get(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._value)

    def update(self, *, enabled: bool | None = None,
               preferences: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            value = deepcopy(self._value)
            if enabled is not None:
                value["enabled"] = bool(enabled)
            if preferences is not None:
                value["preferences"] = normalize_preferences(preferences)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), "utf-8")
            temporary.replace(self.path)
            self._value = value
            return deepcopy(value)


class TelegramState:
    """Persistent private-bot membership, preferences, requests, and dedupe."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._db.row_factory = sqlite3.Row
        with self._db:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA busy_timeout=15000")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id INTEGER PRIMARY KEY,
                    bound_user_id INTEGER NOT NULL,
                    chat_label TEXT NOT NULL,
                    preferences TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    chat_id INTEGER NOT NULL,
                    event_key TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    delivered_at INTEGER NOT NULL,
                    PRIMARY KEY(chat_id,event_key,event_type)
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS telegram_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                ) WITHOUT ROWID;
                CREATE TABLE IF NOT EXISTS access_requests (
                    user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    requested_at INTEGER NOT NULL,
                    decided_at INTEGER,
                    decided_by INTEGER,
                    notified_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS access_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    actor_user_id INTEGER NOT NULL,
                    target_user_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
            """)
            columns = {row[1] for row in self._db.execute("PRAGMA table_info(subscribers)")}
            if "role" not in columns:
                self._db.execute("ALTER TABLE subscribers ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            if "active" not in columns:
                self._db.execute("ALTER TABLE subscribers ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            # A database created by the earlier single-subscriber build has one
            # trusted owner.  Promote only the oldest row during migration.
            if not self._db.execute("SELECT 1 FROM subscribers WHERE role='admin' LIMIT 1").fetchone():
                oldest = self._db.execute("SELECT chat_id FROM subscribers ORDER BY created_at LIMIT 1").fetchone()
                if oldest:
                    self._db.execute("UPDATE subscribers SET role='admin' WHERE chat_id=?", (oldest[0],))

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM telegram_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT INTO telegram_meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (key, value))

    def update_offset(self) -> int:
        with self._lock:
            try:
                return max(0, int(self._meta("update_offset") or 0))
            except ValueError:
                return 0

    def set_update_offset(self, value: int) -> None:
        with self._lock, self._db:
            self._set_meta("update_offset", str(max(0, int(value))))

    def issue_pairing(self, ttl_seconds: int = PAIRING_TTL_SECONDS) -> tuple[str, int]:
        code = secrets.token_urlsafe(24)
        expires_at = int(time.time()) + max(60, min(int(ttl_seconds), 3600))
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        with self._lock, self._db:
            self._set_meta("pairing_hash", digest)
            self._set_meta("pairing_expires_at", str(expires_at))
        return code, expires_at * 1000

    def pairing_pending(self) -> bool:
        with self._lock:
            digest = self._meta("pairing_hash")
            try:
                expiry = int(self._meta("pairing_expires_at") or 0)
            except ValueError:
                expiry = 0
            return bool(digest and expiry >= int(time.time()))

    def cancel_pairing(self) -> None:
        with self._lock, self._db:
            self._db.execute("DELETE FROM telegram_meta WHERE key IN ('pairing_hash','pairing_expires_at')")

    def consume_pairing(self, code: str, chat_id: int, user_id: int,
                        chat_label: str, preferences: dict[str, Any]) -> bool:
        if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", code):
            return False
        digest = hashlib.sha256(code.encode("ascii")).hexdigest()
        now_seconds = int(time.time())
        now_ms = int(time.time() * 1000)
        with self._lock, self._db:
            stored = self._meta("pairing_hash") or ""
            try:
                expiry = int(self._meta("pairing_expires_at") or 0)
            except ValueError:
                expiry = 0
            existing = self._db.execute("SELECT chat_id FROM subscribers WHERE role='admin' LIMIT 1").fetchone()
            if existing or expiry < now_seconds or not hmac.compare_digest(stored, digest):
                return False
            self._db.execute("INSERT INTO subscribers "
                             "(chat_id,bound_user_id,chat_label,preferences,created_at,updated_at,role,active) "
                             "VALUES (?,?,?,?,?,?,'admin',1)", (
                int(chat_id), int(user_id), _clean_label(chat_label),
                json.dumps(normalize_preferences(preferences), ensure_ascii=False), now_ms, now_ms))
            self._db.execute("DELETE FROM telegram_meta WHERE key IN ('pairing_hash','pairing_expires_at')")
            self._db.execute("UPDATE access_requests SET status='approved',decided_at=?,decided_by=? WHERE user_id=?",
                             (now_ms, int(user_id), int(user_id)))
            return True

    @staticmethod
    def _subscriber_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        return {"chat_id": int(row["chat_id"]), "bound_user_id": int(row["bound_user_id"]),
                "chat_label": row["chat_label"],
                "preferences": normalize_preferences(migrate_preferences(json.loads(row["preferences"]))),
                "created_at": int(row["created_at"]), "updated_at": int(row["updated_at"]),
                "role": row["role"], "active": bool(row["active"])}

    def subscriber(self, *, chat_id: int | None = None,
                   user_id: int | None = None) -> dict[str, Any] | None:
        with self._lock:
            if chat_id is not None and user_id is not None:
                row = self._db.execute(
                    "SELECT * FROM subscribers WHERE chat_id=? AND bound_user_id=? AND active=1",
                    (int(chat_id), int(user_id))).fetchone()
            elif chat_id is not None:
                row = self._db.execute("SELECT * FROM subscribers WHERE chat_id=? AND active=1",
                                       (int(chat_id),)).fetchone()
            elif user_id is not None:
                row = self._db.execute("SELECT * FROM subscribers WHERE bound_user_id=? AND active=1",
                                       (int(user_id),)).fetchone()
            else:
                row = self._db.execute(
                    "SELECT * FROM subscribers WHERE role='admin' AND active=1 ORDER BY created_at LIMIT 1"
                ).fetchone()
        return self._subscriber_row(row)

    def subscribers(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM subscribers WHERE active=1 ORDER BY role='admin' DESC, created_at"
            ).fetchall()
        return [value for row in rows if (value := self._subscriber_row(row)) is not None]

    def administrator(self) -> dict[str, Any] | None:
        return self.subscriber()

    def authorized(self, chat_id: int, user_id: int) -> bool:
        return self.subscriber(chat_id=chat_id, user_id=user_id) is not None

    def is_admin(self, chat_id: int, user_id: int) -> bool:
        member = self.subscriber(chat_id=chat_id, user_id=user_id)
        return bool(member and member["role"] == "admin")

    def update_preferences(self, preferences: dict[str, Any], *, chat_id: int | None = None) -> dict[str, Any]:
        value = normalize_preferences(preferences)
        now = int(time.time() * 1000)
        with self._lock, self._db:
            if chat_id is None:
                self._db.execute("UPDATE subscribers SET preferences=?,updated_at=? WHERE role='admin' AND active=1",
                                 (json.dumps(value, ensure_ascii=False), now))
            else:
                self._db.execute("UPDATE subscribers SET preferences=?,updated_at=? WHERE chat_id=? AND active=1",
                                 (json.dumps(value, ensure_ascii=False), now, int(chat_id)))
        return value

    def request_access(self, chat_id: int, user_id: int, username: str,
                       display_name: str) -> tuple[dict[str, Any], bool]:
        now = int(time.time() * 1000)
        username = _clean_label(username.lstrip("@")) if username else ""
        display_name = _clean_label(display_name)
        with self._lock, self._db:
            existing = self._db.execute("SELECT * FROM access_requests WHERE user_id=?", (int(user_id),)).fetchone()
            # Rejected/removed are terminal until an administrator explicitly
            # restores the user with /add.  Letting arbitrary new messages
            # reopen them would allow a denied user to repeatedly notify the
            # administrator.
            created = existing is None
            if created:
                self._db.execute(
                    "INSERT INTO access_requests "
                    "(user_id,chat_id,username,display_name,status,requested_at,decided_at,decided_by,notified_at) "
                    "VALUES (?,?,?,?, 'pending', ?,NULL,NULL,NULL) "
                    "ON CONFLICT(user_id) DO UPDATE SET chat_id=excluded.chat_id,username=excluded.username,"
                    "display_name=excluded.display_name,status='pending',requested_at=excluded.requested_at,"
                    "decided_at=NULL,decided_by=NULL,notified_at=NULL",
                    (int(user_id), int(chat_id), username, display_name, now))
                self._db.execute(
                    "INSERT INTO access_audit(action,actor_user_id,target_user_id,created_at) "
                    "VALUES ('requested',?,?,?)", (int(user_id), int(user_id), now))
            row = self._db.execute("SELECT * FROM access_requests WHERE user_id=?", (int(user_id),)).fetchone()
        return dict(row), created

    def mark_request_notified(self, user_id: int) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute(
                "UPDATE access_requests SET notified_at=? WHERE user_id=? AND status='pending' AND notified_at IS NULL",
                (int(time.time() * 1000), int(user_id)))
        return cursor.rowcount == 1

    def pending_requests(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM access_requests WHERE status='pending' ORDER BY requested_at"
            ).fetchall()
        return [dict(row) for row in rows]

    def decide_request(self, target_user_id: int, actor_user_id: int, approve: bool,
                       preferences: dict[str, Any]) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        with self._lock, self._db:
            request = self._db.execute(
                "SELECT * FROM access_requests WHERE user_id=? AND status='pending'", (int(target_user_id),)
            ).fetchone()
            if not request:
                return None
            status = "approved" if approve else "rejected"
            if approve:
                self._db.execute(
                    "INSERT INTO subscribers "
                    "(chat_id,bound_user_id,chat_label,preferences,created_at,updated_at,role,active) "
                    "VALUES (?,?,?,?,?,?,'user',1) "
                    "ON CONFLICT(chat_id) DO UPDATE SET bound_user_id=excluded.bound_user_id,"
                    "chat_label=excluded.chat_label,preferences=excluded.preferences,updated_at=excluded.updated_at,"
                    "role='user',active=1",
                    (int(request["chat_id"]), int(request["user_id"]), _clean_label(request["display_name"]),
                     json.dumps(normalize_preferences(preferences), ensure_ascii=False), now, now))
            self._db.execute(
                "UPDATE access_requests SET status=?,decided_at=?,decided_by=? WHERE user_id=?",
                (status, now, int(actor_user_id), int(target_user_id)))
            self._db.execute("INSERT INTO access_audit(action,actor_user_id,target_user_id,created_at) VALUES (?,?,?,?)",
                             (status, int(actor_user_id), int(target_user_id), now))
        return dict(request)

    def add_user(self, target_user_id: int, actor_user_id: int,
                 preferences: dict[str, Any]) -> dict[str, Any]:
        """Manually authorize a private user id; private chat ids equal user ids."""
        target_user_id = int(target_user_id)
        now = int(time.time() * 1000)
        label = f"手动授权用户 {target_user_id}"
        with self._lock, self._db:
            existing_admin = self._db.execute(
                "SELECT * FROM subscribers WHERE bound_user_id=? AND role='admin' AND active=1",
                (target_user_id,)).fetchone()
            if existing_admin:
                return self._subscriber_row(existing_admin) or {}
            self._db.execute(
                "INSERT INTO subscribers "
                "(chat_id,bound_user_id,chat_label,preferences,created_at,updated_at,role,active) "
                "VALUES (?,?,?,?,?,?,'user',1) "
                "ON CONFLICT(chat_id) DO UPDATE SET bound_user_id=excluded.bound_user_id,"
                "chat_label=excluded.chat_label,preferences=excluded.preferences,updated_at=excluded.updated_at,"
                "role='user',active=1",
                (target_user_id, target_user_id, label,
                 json.dumps(normalize_preferences(preferences), ensure_ascii=False), now, now))
            self._db.execute(
                "INSERT INTO access_requests "
                "(user_id,chat_id,username,display_name,status,requested_at,decided_at,decided_by,notified_at) "
                "VALUES (?,?,'',?,'approved',?,?,?,NULL) "
                "ON CONFLICT(user_id) DO UPDATE SET status='approved',decided_at=excluded.decided_at,"
                "decided_by=excluded.decided_by",
                (target_user_id, target_user_id, label, now, now, int(actor_user_id)))
            self._db.execute(
                "INSERT INTO access_audit(action,actor_user_id,target_user_id,created_at) VALUES ('manual_add',?,?,?)",
                (int(actor_user_id), target_user_id, now))
        return self.subscriber(user_id=target_user_id) or {}

    def remove_user(self, target_user_id: int, actor_user_id: int) -> dict[str, Any] | None:
        now = int(time.time() * 1000)
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM subscribers WHERE bound_user_id=? AND role!='admin' AND active=1",
                (int(target_user_id),)).fetchone()
            if not row:
                return None
            self._db.execute("UPDATE subscribers SET active=0,updated_at=? WHERE bound_user_id=?",
                             (now, int(target_user_id)))
            self._db.execute(
                "INSERT INTO access_requests(user_id,chat_id,username,display_name,status,requested_at,decided_at,decided_by,notified_at) "
                "VALUES (?,?, '',?,'removed',?,?,?,NULL) "
                "ON CONFLICT(user_id) DO UPDATE SET status='removed',decided_at=excluded.decided_at,decided_by=excluded.decided_by",
                (int(target_user_id), int(row["chat_id"]), row["chat_label"], now, now, int(actor_user_id)))
            self._db.execute("INSERT INTO access_audit(action,actor_user_id,target_user_id,created_at) VALUES ('removed',?,?,?)",
                             (int(actor_user_id), int(target_user_id), now))
        return self._subscriber_row(row)

    def unpair(self) -> bool:
        with self._lock, self._db:
            existed = self._db.execute("SELECT COUNT(*) FROM subscribers").fetchone()[0]
            self._db.execute("DELETE FROM subscribers")
            self._db.execute("DELETE FROM access_requests")
            self._db.execute("DELETE FROM telegram_meta WHERE key IN ('pairing_hash','pairing_expires_at')")
        return bool(existed)

    def delivered(self, chat_id: int, event_key: str, event_type: str) -> bool:
        with self._lock:
            row = self._db.execute("SELECT 1 FROM deliveries WHERE chat_id=? AND event_key=? AND event_type=?",
                                   (int(chat_id), event_key, event_type)).fetchone()
        return row is not None

    def mark_delivered(self, chat_id: int, event_key: str, event_type: str) -> bool:
        with self._lock, self._db:
            cursor = self._db.execute("INSERT OR IGNORE INTO deliveries VALUES (?,?,?,?)", (
                int(chat_id), event_key, event_type, int(time.time() * 1000)))
        return cursor.rowcount == 1


class BotApiClient:
    """Small Bot API client with bounded 429 retry and redacted failures."""

    def __init__(self, token: str, *, session: aiohttp.ClientSession | None = None,
                 sleep: Callable[[float], Any] = asyncio.sleep):
        self._token = validate_bot_token(token)
        self._session = session
        self._owns_session = session is None
        self._sleep = sleep

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45))

    async def close(self) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
        self._session = None

    async def call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise TelegramError("Telegram 客户端尚未启动")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]{1,40}", method):
            raise TelegramError("Telegram 请求方法无效")
        url = f"https://api.telegram.org/bot{self._token}/{method}"
        for attempt in range(3):
            try:
                async with self._session.post(url, json=payload or {}) as response:
                    data = await response.json(content_type=None)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
                if attempt < 2:
                    await self._sleep(1 + attempt)
                    continue
                raise TelegramError("Telegram 网络请求失败") from None
            if response.status == 429 or (isinstance(data, dict) and data.get("error_code") == 429):
                parameters = data.get("parameters") if isinstance(data, dict) else {}
                delay = parameters.get("retry_after", 1) if isinstance(parameters, dict) else 1
                try:
                    delay = max(1, min(float(delay), 60))
                except (TypeError, ValueError):
                    delay = 1
                if attempt < 2:
                    await self._sleep(delay)
                    continue
                raise TelegramError("Telegram 限速重试后仍未恢复")
            if response.status >= 400 or not isinstance(data, dict) or data.get("ok") is not True:
                code = data.get("error_code") if isinstance(data, dict) else response.status
                # The upstream description may echo request data.  Expose only
                # the numeric category; never include a URL, payload, or token.
                raise TelegramError(f"Telegram API 请求失败（{code or 'unknown'}）")
            return data.get("result")
        raise TelegramError("Telegram 请求失败")


class TelegramService:
    def __init__(self, data_dir: str | Path, *,
                 client_factory: Callable[[str], Any] | None = None,
                 token_vault: BotTokenVault | None = None,
                 state: TelegramState | None = None):
        root = Path(data_dir)
        self.vault = token_vault or BotTokenVault(root / "telegram-token.dpapi")
        self.config = TelegramConfigStore(root / "telegram-settings.json")
        self.state = state or TelegramState(root / "telegram.sqlite3")
        self._client_factory = client_factory or (lambda token: BotApiClient(token))
        self._client: Any = None
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._send_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=500)
        self._queued: set[tuple[str, str]] = set()
        self._running = False
        self._connected = False
        self._accept_events = False
        self._bot: dict[str, Any] | None = None
        self._last_error: str | None = None
        self._connected_at: int | None = None
        self._last_send_at = 0.0
        self._chat_last_send: dict[int, float] = {}
        self._rate_lock = asyncio.Lock()
        self._dropped = 0
        self._unauthorized_notice_at: dict[int, float] = {}

    def set_token(self, token: str) -> None:
        self.vault.save(token)

    def clear_token(self) -> bool:
        return self.vault.clear()

    def set_preferences(self, preferences: dict[str, Any]) -> dict[str, Any]:
        value = normalize_preferences(preferences)
        self.config.update(preferences=value)
        if self.state.subscriber():
            self.state.update_preferences(value)
        return value

    def _new_user_preferences(self) -> dict[str, Any]:
        # Approval grants access to the menu, not consent to receive messages.
        # Copy harmless filter choices, but require every new user to opt into
        # both signal and listing delivery independently.
        return normalize_preferences({**self.config.get()["preferences"],
                                      "signals_enabled": False,
                                      "listing_enabled": False})

    def safe_status(self) -> dict[str, Any]:
        subscriber = self.state.subscriber()
        configured = self.config.get()
        preferences = subscriber["preferences"] if subscriber else configured["preferences"]
        bot = ({"username": self._bot.get("username"),
                "name": self._bot.get("first_name")}
               if self._bot else None)
        masked_chat = _mask_chat_id(subscriber["chat_id"]) if subscriber else None
        authorized_count = len(self.state.subscribers())
        pending_count = len(self.state.pending_requests())
        return {
            "enabled": configured["enabled"],
            "connected": self._connected,
            "token_configured": self.vault.configured(),
            "token_source": self.vault.source(),
            "bot": bot,
            "bot_username": bot["username"] if bot else None,
            "bot_name": bot["name"] if bot else None,
            "subscriber": ({"paired": True, "chat_label": subscriber["chat_label"],
                            "masked_chat_id": masked_chat,
                            "paired_at": subscriber["created_at"]}
                           if subscriber else {"paired": False, "chat_label": None,
                                               "masked_chat_id": None, "paired_at": None}),
            "chat_name": subscriber["chat_label"] if subscriber else None,
            "chat_id_masked": masked_chat,
            "preferences": deepcopy(preferences),
            "pairing_required": self._connected and subscriber is None,
            "pairing_pending": self.state.pairing_pending(),
            "queue_depth": self._queue.qsize(),
            "dropped_events": self._dropped,
            "connected_at": self._connected_at,
            "last_error": self._last_error,
            "history_replay": False,
            "authorized_count": authorized_count,
            "pending_count": pending_count,
            "access_control": {
                "private_only": True,
                "authorized_users": authorized_count,
                "pending_requests": pending_count,
            },
        }

    async def start_if_enabled(self) -> None:
        if self.config.get()["enabled"] and self.vault.configured():
            try:
                await self.connect(issue_pairing=False)
            except TelegramError as error:
                self._last_error = str(error)

    async def connect(self, *, issue_pairing: bool = True) -> dict[str, Any]:
        async with self._lock:
            if not self._connected:
                token = self.vault.load()
                if not token:
                    raise TelegramError("请先保存 Bot Token")
                client = self._client_factory(token)
                try:
                    started = client.start()
                    if inspect.isawaitable(started):
                        await started
                    # A stale optional notification endpoint must not hold the
                    # entire local API startup for the Bot API session timeout.
                    bot = await asyncio.wait_for(client.call("getMe", {}), timeout=12)
                    if not isinstance(bot, dict) or bot.get("is_bot") is not True or not bot.get("username"):
                        raise TelegramError("Telegram 未返回有效的机器人身份")
                except TelegramError:
                    result = client.close()
                    if inspect.isawaitable(result):
                        await result
                    raise
                except Exception:
                    result = client.close()
                    if inspect.isawaitable(result):
                        await result
                    raise TelegramError("Telegram 连接失败") from None
                self._client = client
                self._bot = {"id": bot.get("id"), "username": str(bot["username"]),
                             "first_name": str(bot.get("first_name") or bot["username"])}
                self._running = self._connected = self._accept_events = True
                self._connected_at = int(time.time() * 1000)
                self._last_error = None
                self.config.update(enabled=True)
                self._poll_task = asyncio.create_task(self._poll_loop(), name="telegram-update-poll")
                self._send_task = asyncio.create_task(self._send_loop(), name="telegram-event-send")
            response = self.safe_status()
            if issue_pairing and self.state.subscriber() is None:
                code, expires_at = self.state.issue_pairing()
                response.update({
                    "pairing_url": f"https://t.me/{self._bot['username']}?start={code}",
                    "pairing_expires_at": expires_at,
                })
            else:
                response.update({"pairing_url": None, "pairing_expires_at": None})
            return response

    async def disconnect(self) -> dict[str, Any]:
        self.config.update(enabled=False)
        self.state.cancel_pairing()
        await self._stop_runtime()
        return self.safe_status()

    async def close(self) -> None:
        await self._stop_runtime()
        self.state.close()

    async def _stop_runtime(self) -> None:
        async with self._lock:
            self._accept_events = self._running = self._connected = False
            tasks = [task for task in (self._poll_task, self._send_task) if task]
            self._poll_task = self._send_task = None
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            while not self._queue.empty():
                try:
                    kind, event = self._queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._queued.discard((kind, _event_key(event)))
                self._queue.task_done()
            client, self._client = self._client, None
            if client is not None:
                result = client.close()
                if inspect.isawaitable(result):
                    await result

    def enqueue(self, kind: str, event: dict[str, Any]) -> bool:
        """Queue one event from the live collector; never query old events."""
        if not self._accept_events or kind not in {"signal", "listing"}:
            return False
        if not isinstance(event, dict) or event.get("source", "live") != "live" or event.get("synthetic") is True:
            return False
        if kind == "signal" and event.get("delayed") is True:
            return False
        key = _event_key(event)
        identity = (kind, key)
        if not key or identity in self._queued:
            return False
        try:
            self._queue.put_nowait((kind, deepcopy(event)))
        except asyncio.QueueFull:
            self._dropped += 1
            return False
        self._queued.add(identity)
        return True

    async def send_test(self) -> dict[str, Any]:
        subscriber = self.state.subscriber()
        if not self._connected or self._client is None:
            raise TelegramError("Telegram 尚未连接")
        if not subscriber:
            raise TelegramError("请先使用一次性链接绑定 Telegram 会话")
        await self._rate_wait(subscriber["chat_id"])
        await self._client.call("sendMessage", {
            "chat_id": subscriber["chat_id"],
            "text": "合约推送｜连接测试\n\nTelegram 纯文字推送已连接。\n此消息不代表行情或交易信号。",
            "disable_web_page_preview": True,
        })
        return {"sent": True, "masked_chat_id": _mask_chat_id(subscriber["chat_id"])}

    async def unpair(self) -> dict[str, Any]:
        removed = self.state.unpair()
        return {**self.safe_status(), "unpaired": removed}

    async def refresh_admin_menu(self) -> dict[str, Any]:
        """Push the persistent shortcuts and a fresh inline settings menu.

        This is intentionally an explicit action for a protected local API;
        status reads must never call it or send Telegram messages.
        """
        admin = self.state.administrator()
        if not self._connected or self._client is None:
            raise TelegramError("Telegram 尚未连接")
        if not admin:
            raise TelegramError("请先绑定 Telegram 管理员")
        await self._send_shortcuts(admin["chat_id"], is_admin=True)
        await self._send_menu(admin["chat_id"], "快捷菜单和推送设置已刷新。")
        return {"sent": True, "masked_chat_id": _mask_chat_id(admin["chat_id"])}

    async def _poll_loop(self) -> None:
        while self._running and self._client is not None:
            try:
                updates = await self._client.call("getUpdates", {
                    "offset": self.state.update_offset(), "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                })
                if not isinstance(updates, list):
                    raise TelegramError("Telegram 更新格式无效")
                for update in updates[:100]:
                    update_id = update.get("update_id") if isinstance(update, dict) else None
                    if not isinstance(update_id, int):
                        continue
                    try:
                        await self._handle_update(update)
                    finally:
                        self.state.set_update_offset(update_id + 1)
            except asyncio.CancelledError:
                raise
            except TelegramError as error:
                self._last_error = str(error)
                await asyncio.sleep(2)
            except Exception:
                self._last_error = "Telegram 更新处理失败"
                await asyncio.sleep(2)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("text"), str):
            return
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        actor = message.get("from") if isinstance(message.get("from"), dict) else {}
        chat_id, user_id = chat.get("id"), actor.get("id")
        if not isinstance(chat_id, int) or not isinstance(user_id, int):
            return
        # Program-level private mode: BotFather group settings are defense in
        # depth only.  No group/supergroup/channel message may bind, apply, or
        # invoke a command.
        if chat.get("type") != "private" or chat_id != user_id:
            return
        shortcut = QUICK_MENU_COMMANDS.get(message["text"].strip())
        command, argument = shortcut if shortcut is not None else _parse_command(message["text"])
        if command == "start" and argument:
            bound = self.state.consume_pairing(argument, chat_id, user_id,
                                                 _chat_label(chat, actor),
                                                 self.config.get()["preferences"])
            if bound:
                await self._send_shortcuts(chat_id, is_admin=True)
                await self._send_menu(chat_id, "绑定成功。之后可随时用 /menu 调整推送。")
            elif self.state.authorized(chat_id, user_id):
                member = self.state.subscriber(chat_id=chat_id, user_id=user_id)
                await self._send_shortcuts(chat_id, is_admin=member["role"] == "admin")
                await self._send_menu(chat_id)
            elif self.state.administrator() is not None:
                await self._handle_access_request(chat, actor)
            else:
                await self._send_text(chat_id, "绑定码无效、已过期或已被使用。请回到本机看板重新生成。")
            return
        if not self.state.authorized(chat_id, user_id):
            await self._handle_access_request(chat, actor)
            return
        member = self.state.subscriber(chat_id=chat_id, user_id=user_id)
        if command in {"users", "pending", "add", "remove"}:
            if member["role"] != "admin":
                await self._send_text(chat_id, "此功能仅管理员可用。")
            else:
                await self._handle_admin_command(chat_id, user_id, command, argument)
            return
        if command == 'coins':
            from .delivery_menu import coins
            await coins(self,member,argument);return
        if command == "reset":
            preferences = reset_filter_preferences(member["preferences"])
            self.state.update_preferences(preferences, chat_id=chat_id)
            if member["role"] == "admin":
                self.config.update(preferences=preferences)
            await self._send_menu(
                chat_id,
                "筛选已重置为：5m + 15m、多空、活跃度口径、最低 65、空策略、任一通过。"
                "信号推送和新上架两个开关保持原状。",
            )
            return
        if command in {"start", "menu"}:
            await self._send_shortcuts(chat_id, is_admin=member["role"] == "admin")
            await self._send_menu(chat_id)
        elif command == "stop":
            subscriber = self.state.subscriber(chat_id=chat_id, user_id=user_id)
            preferences = {**subscriber["preferences"], "signals_enabled": False}
            preferences = self.state.update_preferences(preferences, chat_id=chat_id)
            if subscriber["role"] == "admin":
                self.config.update(preferences=preferences)
            await self._send_menu(chat_id, "信号推送已暂停；新上架提醒按独立开关处理。")

    async def _handle_access_request(self, chat: dict[str, Any], actor: dict[str, Any]) -> None:
        chat_id, user_id = int(chat["id"]), int(actor["id"])
        display_name = " ".join(str(actor.get(key) or "") for key in ("first_name", "last_name")).strip()
        request, _ = self.state.request_access(
            chat_id, user_id, str(actor.get("username") or ""), display_name or "Telegram 用户")
        admin = self.state.administrator()
        status = request.get("status")
        if (status == "pending" and admin and request.get("notified_at") is None
                and self._client is not None):
            username = f"@{request['username']}" if request.get("username") else "未设置"
            payload = {
                "chat_id": admin["chat_id"],
                "text": "\n".join([
                    "合约推送｜新的使用申请", "", f"显示名：{request['display_name']}",
                    f"用户名：{username}", f"Telegram ID：{request['user_id']}",
                    "", "请确认身份后再批准。",
                ]),
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✅ 通过", "callback_data": f"a:ok:{request['user_id']}"},
                    {"text": "❌ 拒绝", "callback_data": f"a:no:{request['user_id']}"},
                ]]},
                "disable_web_page_preview": True,
            }
            await self._rate_wait(admin["chat_id"])
            await self._client.call("sendMessage", payload)
            self.state.mark_request_notified(user_id)
        now = time.monotonic()
        if now - self._unauthorized_notice_at.get(user_id, 0.0) >= 30:
            self._unauthorized_notice_at[user_id] = now
            if status == "rejected":
                notice = "🔒 你的使用申请未通过。如需复核，请直接联系管理员。"
            elif status == "removed":
                notice = "🔒 你的使用权限已取消。如需恢复，请联系管理员，由管理员重新授权。"
            elif status == "approved":
                notice = "🔒 授权状态需要管理员复核，请联系管理员重新授权。"
            elif admin is None:
                notice = "尚未绑定首位管理员。请在运行程序的电脑打开「系统与推送 → 连接与绑定」，生成安全链接。若无法打开客户端，复制页面中的完整绑定命令，在本聊天发送。普通 /start 不能完成管理员绑定。"
            else:
                notice = "🔒 当前机器人为私有机器人。申请已提交，请等待管理员授权。"
            await self._send_text(chat_id, notice)

    async def _handle_admin_command(self, chat_id: int, user_id: int, command: str,
                                    argument: str | None) -> None:
        if command == "users":
            members = self.state.subscribers()
            lines = ["合约推送｜已授权用户", ""]
            lines.extend(f"{'管理员' if item['role'] == 'admin' else '用户'}｜{item['chat_label']}｜ID {item['bound_user_id']}"
                         for item in members)
            await self._send_text(chat_id, "\n".join(lines))
            return
        if command == "pending":
            await self._send_pending_requests(chat_id)
            return
        if not argument or not argument.isdigit() or not (0 < int(argument) < 10**20):
            await self._send_text(chat_id, f"用法：/{command} <Telegram ID>")
            return
        target = int(argument)
        if command == "add":
            member = self.state.add_user(target, user_id, self._new_user_preferences())
            await self._send_text(chat_id, f"已授权：{member.get('chat_label')}｜ID {target}")
            await self._send_text(target, "你的合约推送使用申请已通过。发送 /menu 设置独立推送条件。")
            await self._send_shortcuts(target, is_admin=False)
        elif command == "remove":
            removed = self.state.remove_user(target, user_id)
            if removed:
                await self._send_text(chat_id, f"已取消授权：{removed['chat_label']}｜ID {target}")
                await self._send_text(removed["chat_id"], "你的合约推送使用权限已被管理员取消。")
            else:
                await self._send_text(chat_id, "未找到可移除的普通授权用户；管理员不能在 Telegram 内移除自己。")

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        query_id = callback.get("id")
        data = callback.get("data")
        actor = callback.get("from") if isinstance(callback.get("from"), dict) else {}
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
        chat_id, user_id, message_id = chat.get("id"), actor.get("id"), message.get("message_id")
        if not isinstance(query_id, str) or not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
            return
        if (not isinstance(chat_id, int) or not isinstance(user_id, int)
                or chat.get("type") != "private" or chat_id != user_id):
            await self._answer_callback(query_id, "仅允许私聊使用")
            return
        if data.startswith("a:"):
            await self._handle_admin_callback(query_id, data, chat_id, user_id, message_id)
            return
        if not self.state.authorized(chat_id, user_id):
            await self._answer_callback(query_id, "此会话未绑定")
            return
        subscriber = self.state.subscriber(chat_id=chat_id, user_id=user_id)
        preferences = deepcopy(subscriber["preferences"])
        if data.startswith('q:'):
            from .delivery_menu import handle
            await self._answer_callback(query_id,'已打开独立规则设置')
            await handle(self,subscriber,data,message_id);return
        note = "已更新"
        menu_page=0
        parts = data.split(":")
        if parts[:2]==['p','page'] and len(parts)==3 and parts[2].isdigit():
            await self._answer_callback(query_id,'已翻页')
            await self._send_menu(chat_id,edit_message_id=message_id,page=int(parts[2]));return
        if parts[:2]==['p','source'] and len(parts)==3 and parts[2] in ('anomaly','indicator','quant'):
            preferences.update(event_kind=parts[2],rules=[])
            if parts[2]=='anomaly':preferences['timeframes']=[tf for tf in preferences['timeframes'] if tf in ANOMALY_TIMEFRAMES] or list(ANOMALY_TIMEFRAMES)
        elif parts[:2]==['p','rule'] and len(parts)==3:
            from .live_rule_signals import rule_catalog
            rule_ids=[r['id'] for r in rule_catalog(preferences['event_kind'])]
            if parts[2] not in rule_ids:return
            menu_page=rule_ids.index(parts[2])//6
            selected=preferences['rules'];selected.remove(parts[2]) if parts[2] in selected else selected.append(parts[2])
        elif data in ('p:rules:all','p:rules:none'):
            from .live_rule_signals import rule_catalog
            preferences['rules']=[r['id'] for r in rule_catalog(preferences['event_kind']) if r['id']!='smc_equity_structure'] if data.endswith('all') else []
        elif parts[:2]==['p','pattern'] and len(parts)==3:
            values=['阳阳','阴阴','阳阴','阴阳'];v=parts[2]
            if v in ('none','all'):preferences['patterns']=values if v=='all' else []
            elif v in ('0','1','2','3'):
                item=values[int(v)];selected=preferences['patterns'];selected.remove(item) if item in selected else selected.append(item)
        elif data=='p:book':preferences['require_book']=not preferences['require_book']
        elif data=='p:action':
            levels=[0,40,60,80,90];current=preferences['min_actionability'];preferences['min_actionability']=next((v for v in levels if v>current),0)
        elif parts[:2] == ["p", "tf"] and len(parts) == 3 and parts[2]=='all':
            preferences['timeframes']=list(ANOMALY_TIMEFRAMES if preferences['event_kind']=='anomaly' else TIMEFRAMES)
        elif parts[:2] == ["p", "tf"] and len(parts) == 3 and parts[2] in TIMEFRAMES:
            if preferences['event_kind']=='anomaly' and parts[2] not in ANOMALY_TIMEFRAMES:return
            note = _toggle_required(preferences["timeframes"], parts[2], "至少保留一个周期")
        elif parts[:2] == ["p", "dir"] and len(parts) == 3 and parts[2] in DIRECTIONS:
            note = _toggle_required(preferences["directions"], parts[2], "至少保留一个方向")
        elif parts[:2] == ["p", "act"] and len(parts) == 3 and parts[2].isdigit() and int(parts[2]) in ACTIVITY_LEVELS:
            preferences["min_activity"] = int(parts[2])
        elif data in {"p:sigmode:activity", "p:sigmode:qualified"}:
            preferences["signal_scope"] = data.rsplit(":", 1)[1]
        elif parts[:2] == ["p", "str"] and len(parts) == 3:
            strategy = parts[2]
            if strategy == "raw":
                preferences["strategies"] = []
            elif strategy in STRATEGIES:
                selected = preferences["strategies"]
                if not selected:
                    selected.append(strategy)
                elif strategy in selected:
                    selected.remove(strategy)
                else:
                    selected.append(strategy)
        elif data in {"p:mode:any", "p:mode:all"}:
            preferences["strategy_mode"] = data.rsplit(":", 1)[1]
        elif data == "p:list":
            preferences["listing_enabled"] = not preferences["listing_enabled"]
        elif data == "p:signals":
            preferences["signals_enabled"] = not preferences["signals_enabled"]
        elif data != "p:refresh":
            await self._answer_callback(query_id, "按钮已失效，请刷新菜单")
            return
        preferences = self.state.update_preferences(preferences, chat_id=chat_id)
        if subscriber["role"] == "admin":
            self.config.update(preferences=preferences)
        await self._answer_callback(query_id, note)
        await self._send_menu(chat_id, edit_message_id=message_id if isinstance(message_id, int) else None,page=menu_page)

    async def _handle_admin_callback(self, query_id: str, data: str, chat_id: int,
                                     user_id: int, message_id: Any) -> None:
        if not self.state.is_admin(chat_id, user_id):
            await self._answer_callback(query_id, "只有管理员可以审批")
            return
        match = re.fullmatch(r"a:(ok|no):([1-9][0-9]{0,19})", data)
        if not match:
            await self._answer_callback(query_id, "审批按钮无效")
            return
        approve = match.group(1) == "ok"
        target = int(match.group(2))
        request = self.state.decide_request(
            target, user_id, approve, self._new_user_preferences())
        if request is None:
            await self._answer_callback(query_id, "申请已处理或不存在")
            return
        await self._answer_callback(query_id, "已批准" if approve else "已拒绝")
        if self._client is not None and isinstance(message_id, int):
            await self._client.call("editMessageText", {
                "chat_id": chat_id, "message_id": message_id,
                "text": f"合约推送｜使用申请\n\n{request['display_name']}｜ID {target}\n"
                        + ("✅ 已批准" if approve else "❌ 已拒绝"),
            })
        if approve:
            await self._send_text(request["chat_id"],
                                  "你的合约推送使用申请已通过。发送 /menu 设置独立推送条件。")
            await self._send_shortcuts(request["chat_id"], is_admin=False)
        else:
            await self._send_text(request["chat_id"], "你的合约推送使用申请未通过。")

    async def _answer_callback(self, query_id: str, text: str) -> None:
        if self._client is not None:
            await self._client.call("answerCallbackQuery", {"callback_query_id": query_id,
                                                             "text": str(text)[:150]})

    async def _send_text(self, chat_id: int, text: str) -> None:
        if self._client is not None:
            await self._rate_wait(chat_id)
            await self._client.call("sendMessage", {"chat_id": chat_id, "text": text,
                                                     "disable_web_page_preview": True})

    async def _send_shortcuts(self, chat_id: int, *, is_admin: bool) -> None:
        if self._client is None:
            return
        await self._rate_wait(chat_id)
        await self._client.call("sendMessage", {
            "chat_id": chat_id,
            "text": ("管理员快捷菜单已就绪。审批申请可点“申请列表”。"
                     if is_admin else "快捷菜单已就绪。可随时调整或重置自己的筛选。"),
            "reply_markup": quick_menu_keyboard(is_admin),
            "disable_web_page_preview": True,
        })

    async def _send_pending_requests(self, chat_id: int) -> None:
        if self._client is None:
            return
        pending = self.state.pending_requests()
        if not pending:
            await self._send_text(chat_id, "合约推送｜待审核申请\n\n当前没有待审核申请。")
            return
        visible = pending[:20]
        overview = ["合约推送｜待审核申请", "", f"共 {len(pending)} 人待审核。下面每人一张独立审批卡片，处理一人不会影响其他按钮。"]
        if len(pending) > len(visible):
            overview.append(f"本次先显示前 {len(visible)} 人；处理后再次刷新可查看其余申请。")
        await self._send_text(chat_id, "\n".join(overview))
        for index, item in enumerate(visible, 1):
            username = f"@{item['username']}" if item.get("username") else "未设置用户名"
            await self._rate_wait(chat_id)
            await self._client.call("sendMessage", {
                "chat_id": chat_id,
                "text": "\n".join([
                    f"合约推送｜待审核申请 {index}/{len(pending)}", "",
                    f"显示名：{item['display_name']}", f"用户名：{username}",
                    f"Telegram ID：{item['user_id']}", "", "请确认身份后再批准。",
                ]),
                "reply_markup": {"inline_keyboard": [[
                    {"text": "✅ 通过", "callback_data": f"a:ok:{item['user_id']}"},
                    {"text": "❌ 拒绝", "callback_data": f"a:no:{item['user_id']}"},
                ]]},
                "disable_web_page_preview": True,
            })

    async def _send_menu(self, chat_id: int, prefix: str | None = None,
                         edit_message_id: int | None = None, page: int = 0) -> None:
        subscriber = self.state.subscriber(chat_id=chat_id)
        if not subscriber or subscriber["chat_id"] != chat_id or self._client is None:
            return
        text = menu_text(subscriber["preferences"])
        if prefix:
            text = prefix + "\n\n" + text
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text,
                                   "reply_markup": menu_keyboard(subscriber["preferences"],page),
                                   "disable_web_page_preview": True}
        if edit_message_id is not None:
            payload["message_id"] = edit_message_id
            await self._client.call("editMessageText", payload)
        else:
            await self._rate_wait(chat_id)
            await self._client.call("sendMessage", payload)

    async def _send_loop(self) -> None:
        while self._running:
            kind, event = await self._queue.get()
            identity = (kind, _event_key(event))
            try:
                subscribers = self.state.subscribers()
                if not subscribers:
                    continue
                event_key = _event_key(event)
                for subscriber in subscribers:
                    preferences = subscriber["preferences"]
                    if kind == "signal":
                        matches, indicators = signal_matches(event, preferences)
                        if not matches:
                            continue
                        text = format_signal(event, preferences, indicators)
                    else:
                        if not preferences["listing_enabled"]:
                            continue
                        text = format_listing(event)
                    if self.state.delivered(subscriber["chat_id"], event_key, kind):
                        continue
                    await self._rate_wait(subscriber["chat_id"])
                    for attempt in range(3):
                        try:
                            await self._client.call("sendMessage", {
                                "chat_id": subscriber["chat_id"], "text": text,
                                "disable_web_page_preview": True,
                            })
                            self.state.mark_delivered(subscriber["chat_id"], event_key, kind)
                            self._last_error = None
                            break
                        except TelegramError as error:
                            self._last_error = str(error)
                            if attempt == 2:
                                break
                            await asyncio.sleep(1 + attempt)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._last_error = "Telegram 消息生成或发送失败"
            finally:
                self._queued.discard(identity)
                self._queue.task_done()

    async def _rate_wait(self, chat_id: int) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = max(0.0, self._last_send_at + 0.05 - now,
                       self._chat_last_send.get(chat_id, 0.0) + 1.0 - now)
            if wait:
                await asyncio.sleep(wait)
            sent_at = time.monotonic()
            self._last_send_at = sent_at
            self._chat_last_send[chat_id] = sent_at


def signal_direction(signal: dict[str, Any]) -> str | None:
    plan = signal.get("trade_plan") if isinstance(signal.get("trade_plan"), dict) else {}
    recorded = plan.get("direction") or signal.get("entry_direction")
    if recorded in DIRECTIONS:
        return recorded
    direction = signal.get("direction")
    if direction in {"up", "down"}:
        return "long" if direction == "up" else "short"
    pattern = signal.get("pattern")
    if pattern in {"阳阳", "阴阳"}:
        return "long"
    if pattern in {"阴阴", "阳阴"}:
        return "short"
    return None


def signal_matches(signal: dict[str, Any], preferences: dict[str, Any]) -> tuple[bool, list[str]]:
    preferences = normalize_preferences(preferences)
    if signal.get('rule_id')=='smc_equity_structure' and preferences['rule_settings'] is None:return False,[]
    if preferences['rule_settings'] is not None:
        profile=next((p for p in preferences['rule_settings'] if p['kind']==signal.get('rule_kind') and p['rule_id']==signal.get('rule_id')),None)
        if profile is None:return False,[]
        if profile['rule_id']=='smc_equity_structure' and not profile['symbols']:return False,[]
        if profile['symbols'] and signal.get('symbol','').upper() not in profile['symbols']:return False,[]
        if profile['rule_id']=='smc_equity_structure':
            from .smc_delivery import matches
            if not matches(signal,profile):return False,[]
        preferences={**preferences,**profile,'event_kind':profile['kind'],'rules':[profile['rule_id']],
            'directions':['long','short'],'require_book':False,'signal_scope':'activity','strategies':[]}
    if not preferences["signals_enabled"] or signal.get("source", "live") != "live":
        return False, []
    if signal.get("synthetic") is True or signal.get("delayed") is True:
        return False, []
    if signal.get("timeframe") not in preferences["timeframes"]:
        return False, []
    if signal.get('rule_kind','anomaly')!=preferences['event_kind']:return False,[]
    if preferences['event_kind']!='anomaly' and signal.get('rule_id') not in preferences['rules']:return False,[]
    if signal.get('rule_id')=='nan_strategy1_oversold' and signal.get('signal_kind') in ('卖出平多','买回平空'):return True,['楠策略原生平仓']
    if preferences['patterns'] and signal.get('pattern') not in preferences['patterns']:return False,[]
    action=signal.get('actionability') or {}
    if preferences['min_actionability']>0 and (_finite(action.get('score')) is None or action['score']<preferences['min_actionability']):return False,[]
    if preferences['require_book'] and action.get('execution_status')!='通过':return False,[]
    if signal_direction(signal) not in preferences["directions"]:
        return False, []
    score = _finite(signal.get("activity_score"))
    if preferences["min_activity"]>0 and (score is None or score < preferences["min_activity"]):
        return False, []
    if preferences["event_kind"]=="anomaly" and preferences["signal_scope"] == "qualified":
        schema = signal.get("quality_schema_version")
        if (signal.get("qualified") is not True or not isinstance(schema, int)
                or isinstance(schema, bool) or schema != 2):
            return False, []
    if preferences["event_kind"]!="anomaly":return True,[]
    selected = set(preferences["strategies"])
    if not selected:
        return True, []
    confirmations = signal.get("available_confirmations") or signal.get("confirmations") or []
    passed = {str(item.get("id")) for item in confirmations
              if isinstance(item, dict) and item.get("passed") is True}
    matches = selected & passed
    accepted = selected <= passed if preferences["strategy_mode"] == "all" else bool(matches)
    return accepted, [item for item in STRATEGIES if item in matches]


def filter_description(preferences: dict[str, Any]) -> str:
    p=normalize_preferences(preferences)
    parts=['周期 '+(' / '.join(p['timeframes']) or '未选，不推送'),
        '方向 '+(' / '.join('多向' if d=='long' else '空向' for d in p['directions']) or '未选，不推送'),
        '形态 '+(' / '.join(p['patterns']) or '不限（含十字星）'),
        f"活跃原分≥{p['min_activity']}（非百分比）",
        f"结构分≥{p['min_actionability']:g}/100（非胜率）",
        '盘口 '+('信号当时必须通过：价差≤0.08%、流动性分位≥40%' if p['require_book'] else '不额外过滤，保留真实检查结果')]
    if p['event_kind']=='anomaly':
        parts += ['保留全局两 K 异常门槛', '完整质量 '+('必须通过' if p['signal_scope']=='qualified' else '不额外过滤'),
            '方向确认 '+((' / '.join(STRATEGY_LABELS[r] for r in p['strategies']))+'（'+('全部通过' if p['strategy_mode']=='all' else '任一通过')+'）' if p['strategies'] else '不额外过滤')]
    else:
        parts += [f"已选 {len(p['rules'])} 条规则，任一触发分别提示" if p['rules'] else '规则未选，不推送', '不要求两 K 异常']
    return '筛选（各项同时满足；0 分不按该分数过滤）：'+ '；'.join(parts)


def format_signal(signal: dict[str, Any], preferences: dict[str, Any],
                  matched_indicators: list[str] | None = None) -> str:
    if signal.get('rule_id') == 'smc_equity_structure':
        from .smc_delivery import format_message
        profile=next((r for r in (preferences.get('rule_settings') or []) if r.get('rule_id')=='smc_equity_structure'),{})
        return format_message(signal,profile,_time_text)
    if signal.get('rule_kind') in ('indicator','quant'):
        a=signal.get('actionability') or {}
        action=str(signal.get('signal_kind') or '')
        direction='多' if signal_direction(signal)=='long' else '空' if signal_direction(signal)=='short' else '未确认'
        if '平' in action:direction=action
        pattern=signal.get('pattern') or '无'
        if pattern not in ('阳阳','阴阴','阳阴','阴阳'):pattern='无'
        label='指标策略'
        return '\n'.join([f"{signal.get('symbol')} · {signal.get('timeframe')} · {direction}",
            f"时间：{_time_text(signal.get('close_time'))}（北京时间）",
            f"活跃度：{_number(signal.get('activity_score'),0)} / 可做度：{_number(a.get('score'),1)}",
            f"形态：{pattern}",f"{label}：{signal.get('rule_name')}"])
    direction = signal_direction(signal)
    candidate_direction_label = ("做多" if direction == "long" else
                                 "做空" if direction == "short" else "方向未确认")
    assessment = (signal.get("position_assessment")
                  if isinstance(signal.get("position_assessment"), dict) else {})
    assessment_status = str(assessment.get("status") or assessment.get("verdict")
                            or assessment.get("action") or "").strip().lower()
    non_actionable = (assessment.get("blocked") is True
                      or str(assessment.get("trade_direction") or "").lower() == "wait"
                      or assessment_status in {
        "blocked", "wait", "waiting", "avoid", "no_trade", "no-trade",
        "观望", "等待", "阻止", "暂不入场", "不入场",
    })
    direction_label = "观察（暂不入场）" if non_actionable else candidate_direction_label
    setup_label = "反转" if signal.get("setup") == "reversal" else "趋势" if signal.get("setup") == "trend" else "未分类"
    strategies = preferences.get("strategies") or []
    if not strategies:
        strategy_label = "空策略（不做指标过滤）"
    else:
        matched = matched_indicators if matched_indicators is not None else strategies
        strategy_label = (" + " if preferences.get("strategy_mode") == "all" else " / ").join(
            STRATEGY_LABELS[item] for item in matched if item in STRATEGY_LABELS) or "未确认"
    plan = signal.get("trade_plan") if isinstance(signal.get("trade_plan"), dict) else {}
    price = plan.get("reference_entry")
    if price is None:
        candles = signal.get("candles")
        if isinstance(candles, list) and candles and isinstance(candles[-1], dict):
            price = candles[-1].get("close")
    gate_passed = (signal.get("qualified") is True
                   and signal.get("quality_schema_version") == 2)
    signal_title = ("完整强信号" if preferences.get("signal_scope") == "qualified"
                    else "强活跃异常")
    lines = [
        f"合约推送｜{signal_title}",
        "",
        f"{signal.get('symbol', '未知合约')} · {signal.get('timeframe', '未知周期')} · {direction_label}",
        f"策略：{strategy_label}",
        f"形态：{signal.get('pattern') or '未知'}（{setup_label}）",
        f"活跃度：{_number(signal.get('activity_score'), 0)}/{signal.get('score_available_max') or '未记录'}（原分）",
        f"可操作度：{_number((signal.get('actionability') or {}).get('score'),1)} / 100（结构分，非胜率）",
        f"盘口：{(signal.get('actionability') or {}).get('execution_status','证据不足')}",
        f"异常波动：{_number(signal.get('volatility_ratio'), 2)}×",
        "完整门槛：" + ("已通过" if gate_passed else "未通过"),
        f"收线：{_time_text(signal.get('second_candle_close_time', signal.get('close_time')))}",
        filter_description(preferences),
    ]
    daily_count = signal.get("daily_signal_count")
    if isinstance(daily_count, int) and not isinstance(daily_count, bool) and daily_count > 0:
        details = []
        timeframe_count = signal.get("daily_timeframe_signal_count")
        direction_count = signal.get("daily_direction_signal_count")
        if isinstance(timeframe_count, int) and not isinstance(timeframe_count, bool) and timeframe_count > 0:
            details.append(f"同周期 {timeframe_count}")
        if isinstance(direction_count, int) and not isinstance(direction_count, bool) and direction_count > 0:
            details.append(f"同方向 {direction_count}")
        suffix = ("；".join(details) + "；" if details else "") + "北京时间隔日清零"
        lines.append(f"今日出现：第 {daily_count} 次（{suffix}）")
    if assessment:
        if non_actionable and direction is not None:
            lines.append(f"候选方向：{candidate_direction_label}（当前不建议入场）")
        for title, keys in (
            ("位置判断", ("label", "zone_label", "position_label")),
            ("判断依据", ("reason", "reason_text")),
            ("操作提示", ("advice", "advice_text")),
        ):
            value = next((assessment.get(key) for key in keys if assessment.get(key)), None)
            if value is not None:
                lines.append(f"{title}：{_clean_message_text(value)}")
        zone_low = _finite(assessment.get("zone_low"))
        zone_high = _finite(assessment.get("zone_high"))
        if zone_low is not None and zone_high is not None:
            low, high = sorted((zone_low, zone_high))
            lines.append(f"关键区域：{_price(low)} – {_price(high)}")
        distance = _finite(assessment.get("distance_pct"))
        if distance is not None:
            lines.append(f"距关键区域：{_number(distance, 2)}%")
        if assessment.get("breakout_confirmed") is True:
            lines.append("突破状态：连续收盘确认已完成")
        if assessment.get("uses_closed_bars_only") is True:
            lines.append("判断口径：仅使用信号时点前的已收盘 K 线")
    if not gate_passed:
        reasons = signal.get("quality_reasons") or signal.get("rejection_reasons") or []
        if isinstance(reasons, list) and reasons:
            safe_reasons = [_clean_label(str(item)) for item in reasons[:2] if item]
            if safe_reasons:
                lines.append("未通过原因：" + "；".join(safe_reasons))
    if _finite(price) is not None:
        lines.append(f"参考价：{_price(price)}")
    if _finite(signal.get("relative_volume")) is not None:
        lines.append(f"相对成交额：{_number(signal.get('relative_volume'), 2)}×")
    if _finite(signal.get("oi_change_pct")) is not None:
        lines.append(f"持仓变化：{_signed(signal.get('oi_change_pct'))}%")
    if plan and _finite(plan.get("stop_loss")) is not None and _finite(plan.get("target")) is not None:
        lines.extend([f"失效参考：{_price(plan['stop_loss'])}",
                      f"目标参考：{_price(plan['target'])}",
                      f"成本后盈亏比：{_number(plan.get('risk_reward'), 2)}"])
    lines.extend(["", "仅为行情监测提示，不构成交易建议；请自行核对价格、风险与流动性。"])
    return "\n".join(lines)


def format_listing(event: dict[str, Any]) -> str:
    event_type = event.get("event_type")
    title = "新合约已开放交易" if event_type == "listed" else "发现待上架合约"
    lines = ["合约推送｜新合约提醒", "", f"{event.get('symbol', '未知合约')} · {title}"]
    if event.get("contract_type"):
        lines.append(f"合约类型：{_localized_code(event['contract_type'], CONTRACT_TYPE_LABELS)}")
    if event.get("underlying_type"):
        lines.append(f"资产类型：{_localized_code(event['underlying_type'], UNDERLYING_TYPE_LABELS)}")
    if event.get("onboard_date"):
        lines.append(f"计划时间：{_time_text(event['onboard_date'])}")
    lines.extend([f"交易状态：{_localized_code(event.get('status'), TRADING_STATUS_LABELS)}",
                  "来源：币安 U 本位合约清单",
                  "", "上架状态与行情预热就绪是两件事；未就绪前不会产生正式周期信号。"])
    return "\n".join(lines)


def menu_text(preferences: dict[str, Any]) -> str:
    p = normalize_preferences(preferences)
    if p['rule_settings'] is not None:
        return f"合约推送｜我的推送规则\n已选 {len(p['rule_settings'])} 项；点击各项独立设置周期、可做度、活跃度、形态和币种。\n仅影响你自己；每项满足自己的全部条件就推送。\n信号：{'开启' if p['signals_enabled'] else '暂停'}"
    from .live_rule_signals import rule_catalog
    names={r['id']:r['name'] for r in rule_catalog(p['event_kind'])}
    strategy = "空策略" if not p["strategies"] else (
        (" + " if p["strategy_mode"] == "all" else " / ").join(
            STRATEGY_LABELS[item] for item in p["strategies"]))
    return "\n".join([
        "合约推送｜推送菜单",
        "",
        "每位授权用户独立保存；修改仅影响你自己。",
        "事件："+{"anomaly":"两K异常","indicator":"指标策略","quant":"指标策略"}[p["event_kind"]],
        "形态："+(" / ".join(p["patterns"]) or "无形态"),
        "事件规则："+((" / ".join(names[r] for r in p["rules"]) or "未选，不会推送") if p['event_kind']!='anomaly' else '两 K 异常候选'),
        "可操作度门槛："+str(p["min_actionability"])+"；盘口通过才推："+str(p["require_book"]),
        "信号：" + ("开启" if p["signals_enabled"] else "暂停"),
        filter_description(p),
        "周期：" + "、".join(p["timeframes"]),
        "方向：" + "、".join("做多" if item == "long" else "做空" for item in p["directions"]),
        f"最低活跃度：{p['min_activity']}",
        (f"两 K 方向确认：{strategy}" if p['event_kind']=='anomaly' else '开平仓只按所选源码规则'),
        "新上架提醒：" + ("开启" if p["listing_enabled"] else "关闭"),
        "",
        "点击下方按钮立即保存。同一用户同一事件只推送一次。",
        "实时推送支持 5m / 15m；其他周期当前仅供图表和历史研究。发送 /menu 获取新版菜单。",
    ])


def menu_keyboard(preferences: dict[str, Any], page: int = 0) -> dict[str, Any]:
    p = normalize_preferences(preferences)
    if p['rule_settings'] is not None:
        from .delivery_menu import overview
        return overview(p)
    mark = lambda active: "✅" if active else "▫️"
    rows = [
        [{"text":"选择指标策略", "callback_data":"q:list:indicator:0"}],
        [{"text":"全部可推周期", "callback_data":"p:tf:all"}],
        [{"text": f"{mark('5m' in p['timeframes'])} 5m", "callback_data": "p:tf:5m"},
         {"text": f"{mark('15m' in p['timeframes'])} 15m", "callback_data": "p:tf:15m"}],
        [{"text": f"{mark('long' in p['directions'])} 做多", "callback_data": "p:dir:long"},
         {"text": f"{mark('short' in p['directions'])} 做空", "callback_data": "p:dir:short"}],
        [{"text": f"{mark(p['min_activity'] == level)} ≥{level}", "callback_data": f"p:act:{level}"}
         for level in ACTIVITY_LEVELS],
        [{"text": f"{mark(p['signal_scope'] == 'activity')} 活跃异常", "callback_data": "p:sigmode:activity"},
         {"text": f"{mark(p['signal_scope'] == 'qualified')} 完整门槛", "callback_data": "p:sigmode:qualified"}],
        [{"text": f"{mark(not p['strategies'])} 空策略", "callback_data": "p:str:raw"},
         {"text": f"{mark('wt_extreme_cross' in p['strategies'])} WaveTrend 超买超卖交叉", "callback_data": "p:str:wt_extreme_cross"}],
    ]
    if p['event_kind']!='anomaly':
        periods=[tf for tf in TIMEFRAMES if tf not in ANOMALY_TIMEFRAMES]
        rows[2:2]=[[dict(text=f"{mark(tf in p['timeframes'])} {tf}",callback_data=f'p:tf:{tf}') for tf in periods[i:i+3]] for i in range(0,len(periods),3)]
    if len(p["strategies"]) > 1:
        rows.append([
            {"text": f"{mark(p['strategy_mode'] == 'any')} 任一确认", "callback_data": "p:mode:any"},
            {"text": f"{mark(p['strategy_mode'] == 'all')} 全部共振", "callback_data": "p:mode:all"},
        ])
    rows.extend([
        [{"text": f"{mark(p['signals_enabled'])} 信号推送", "callback_data": "p:signals"},
         {"text": f"{mark(p['listing_enabled'])} 新上架", "callback_data": "p:list"}],
        [{"text": "刷新菜单", "callback_data": "p:refresh"}],
    ])
    if p['event_kind']!='anomaly':
        rows=[row for row in rows if not any(b['callback_data'].startswith(('p:str:','p:sigmode:','p:mode:')) for b in row)]
    from .live_rule_signals import rule_catalog
    catalogue=rule_catalog(p['event_kind']);pages=max(1,(len(catalogue)+5)//6);page=max(0,min(page,pages-1))
    rows[0:0]=[
        [{'text':mark(p['event_kind']==k)+v,'callback_data':'p:source:'+k} for k,v in [('anomaly','两K异常'),('indicator','指标策略')]],
        [{'text':mark(not p['patterns'])+'无形态','callback_data':'p:pattern:none'},{'text':mark(len(p['patterns'])==4)+'全部形态','callback_data':'p:pattern:all'}],
        [{'text':mark(v in p['patterns'])+v,'callback_data':'p:pattern:'+str(i)} for i,v in enumerate(('阳阳','阴阴','阳阴','阴阳'))],
        [{'text':mark(p['require_book'])+'仅盘口通过','callback_data':'p:book'},{'text':'可操作度 ≥'+str(p['min_actionability'])+'（切换）','callback_data':'p:action'}]]
    if catalogue:
        rows.extend([[{'text':'全部规则','callback_data':'p:rules:all'},{'text':'清空规则','callback_data':'p:rules:none'}]])
        rows.extend([[{'text':mark(r['id'] in p['rules'])+r['name'],'callback_data':'p:rule:'+r['id']}] for r in catalogue[page*6:page*6+6]])
        rows.append([{'text':'上一页','callback_data':'p:page:'+str(max(0,page-1))},{'text':f'{page+1}/{pages} 下一页','callback_data':'p:page:'+str(min(pages-1,page+1))}])
    assert all(len(button["callback_data"].encode("utf-8")) <= MAX_CALLBACK_BYTES
               for row in rows for button in row)
    return {"inline_keyboard": rows}


def quick_menu_keyboard(is_admin: bool) -> dict[str, Any]:
    """Persistent keyboard shown below Telegram's message input."""
    if is_admin:
        rows = [
            [{"text": QUICK_MENU_SETTINGS}, {"text": QUICK_MENU_PENDING}],
            [{"text": QUICK_MENU_USERS}, {"text": QUICK_MENU_RESET}],
        ]
    else:
        rows = [[{"text": QUICK_MENU_SETTINGS}, {"text": QUICK_MENU_RESET}]]
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": True,
        "input_field_placeholder": "选择合约推送功能",
    }


def _toggle_required(items: list[str], value: str, warning: str) -> str:
    if value in items:
        if len(items) == 1:
            return warning
        items.remove(value)
    else:
        items.append(value)
    return "已更新"


def _localized_code(value: Any, labels: dict[str, str]) -> str:
    if value is None or str(value).strip() == "":
        return "未知"
    code = str(value).strip().upper()
    return labels.get(code, str(value).strip())


def _parse_command(text: str) -> tuple[str | None, str | None]:
    coins=re.fullmatch(r'/coins(?:@[A-Za-z0-9_]+)?\s+([^\r\n]{1,3900})',text.strip(),re.I)
    if coins:return 'coins',coins.group(1)
    match = re.match(r"^/([A-Za-z]+)(?:@[A-Za-z0-9_]+)?(?:\s+([^\s]{1,80}))?\s*$", text[:200])
    return (match.group(1).lower(), match.group(2)) if match else (None, None)


def _event_key(event: dict[str, Any]) -> str:
    key = event.get("id") or event.get("alert_id")
    if isinstance(key, str) and 1 <= len(key) <= 300:
        return key
    symbol, timeframe = event.get("symbol"), event.get("timeframe")
    close_time = event.get("close_time") or event.get("observed_at")
    if symbol and close_time:
        return f"{symbol}:{timeframe or 'listing'}:{close_time}"
    return ""


def _clean_label(value: str) -> str:
    cleaned = " ".join(str(value).replace("\x00", " ").split())
    return cleaned[:80] or "Telegram 会话"


def _clean_message_text(value: Any) -> str:
    """Bound untrusted event labels without allowing multiline spoofing."""
    cleaned = " ".join(str(value).replace("\x00", " ").split())
    return cleaned[:240] or "暂无"


def _chat_label(chat: dict[str, Any], actor: dict[str, Any]) -> str:
    if chat.get("title"):
        return _clean_label(str(chat["title"]))
    name = " ".join(str(actor.get(key) or "") for key in ("first_name", "last_name")).strip()
    username = str(actor.get("username") or "")
    return _clean_label(name + (f" (@{username})" if username else ""))


def _mask_chat_id(chat_id: int) -> str:
    value = str(int(chat_id))
    sign = "-" if value.startswith("-") else ""
    digits = value.lstrip("-")
    visible = digits[-4:]
    return sign + "*" * max(4, len(digits) - len(visible)) + visible


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _number(value: Any, decimals: int) -> str:
    number = _finite(value)
    return "暂无" if number is None else f"{number:.{decimals}f}"


def _signed(value: Any) -> str:
    number = _finite(value)
    return "暂无" if number is None else f"{number:+.2f}"


def _price(value: Any) -> str:
    number = _finite(value)
    if number is None:
        return "暂无"
    if abs(number) >= 1000:
        return f"{number:,.2f}"
    if abs(number) >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _time_text(value: Any) -> str:
    try:
        timestamp = int(value) / 1000
        return datetime.fromtimestamp(timestamp, LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S (UTC+8)")
    except (TypeError, ValueError, OSError, OverflowError):
        return "时间未知"
