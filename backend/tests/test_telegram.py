"""Telegram tests use only fake Bot API clients; no real message is sent."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient

from monitor.collector import MonitorService
from monitor.telegram import (
    BotApiClient,
    BotTokenVault,
    DEFAULT_PREFERENCES,
    TelegramError,
    TelegramService,
    TelegramState,
    TokenStorageError,
    format_listing,
    format_signal,
    menu_keyboard,
    normalize_preferences,
    quick_menu_keyboard,
    reset_filter_preferences,
    signal_direction,
    signal_matches,
)


TOKEN = "123456789:" + "A" * 35
HEADERS = {"X-Pulse-Client": "dashboard", "Origin": "http://127.0.0.1:5173"}


class MemoryVault:
    def __init__(self, token: str | None = TOKEN):
        self.token = token

    def configured(self):
        return self.token is not None

    def source(self):
        return "test" if self.token else None

    def load(self):
        return self.token

    def save(self, value):
        self.token = value

    def clear(self):
        existed = self.token is not None
        self.token = None
        return existed


class FakeClient:
    def __init__(self, token: str):
        self.token_was_supplied = bool(token)
        self.calls: list[tuple[str, dict]] = []
        self.poll_gate = asyncio.Event()
        self.started = False
        self.closed = False

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True
        self.poll_gate.set()

    async def call(self, method, payload):
        if method == "getMe":
            return {"id": 42, "is_bot": True, "username": "pulse_test_bot", "first_name": "PULSE"}
        if method == "getUpdates":
            await self.poll_gate.wait()
            return []
        self.calls.append((method, dict(payload)))
        return True


def signal(**changes):
    value = {
        "id": "BTCUSDT:5m:1788300000000:TWO_BAR_VOLATILITY",
        "source": "live",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "close_time": 1788300000000,
        "second_candle_close_time": 1788300000000,
        "pattern": "阳阳",
        "direction": "up",
        "activity_score": 88,
        "volatility_ratio": 2.35,
        "relative_volume": 2.1,
        "qualified": False,
        "quality_schema_version": 2,
        "quality_reasons": ["成本后空间不足", "买卖价差过大"],
        "available_confirmations": [
            {"id": "wt_extreme_cross", "passed": True},
            {"id": "smc", "passed": True},
        ],
        "trade_plan": {"direction": "long", "reference_entry": 62450.5,
                       "stop_loss": 62000, "target": 63500, "risk_reward": 1.9},
    }
    value.update(changes)
    return value


class PreferenceTests(unittest.TestCase):
    def test_message_filter_description_tracks_mode_and_exact_thresholds(self):
        from monitor.telegram import filter_description
        anomaly=normalize_preferences({'min_activity':80,'strategy_mode':'any','strategies':['wt_extreme_cross']})
        text=format_signal(signal(),anomaly)
        self.assertIn('活跃原分≥80（非百分比）',text)
        self.assertIn('任一通过',text)
        pure=normalize_preferences({**anomaly,'event_kind':'indicator','rules':['wt_extreme_cross'],'require_book':True})
        description=filter_description(pure)
        self.assertIn('价差≤0.08%、流动性分位≥40%',description)
        self.assertIn('不要求两 K 异常',description)
        self.assertNotIn('方向确认',description)

    def test_reset_filters_keeps_both_delivery_switches(self):
        reset = reset_filter_preferences({
            "timeframes": ["5m"], "directions": ["short"],
            "min_activity": 90, "signal_scope": "qualified",
            "strategies": ["wt_extreme_cross"], "strategy_mode": "all",
            "signals_enabled": True, "listing_enabled": False,
        })
        self.assertEqual(reset["timeframes"], ["5m", "15m"])
        self.assertEqual(reset["directions"], ["long", "short"])
        self.assertEqual(reset["min_activity"], 65)
        self.assertEqual(reset["signal_scope"], "activity")
        self.assertEqual(reset["strategies"], [])
        self.assertEqual(reset["strategy_mode"], "any")
        self.assertTrue(reset["signals_enabled"])
        self.assertFalse(reset["listing_enabled"])

    def test_persistent_quick_keyboards_separate_admin_functions(self):
        admin = quick_menu_keyboard(True)
        member = quick_menu_keyboard(False)
        self.assertTrue(admin["is_persistent"])
        self.assertFalse(admin["one_time_keyboard"])
        admin_labels = [button["text"] for row in admin["keyboard"] for button in row]
        member_labels = [button["text"] for row in member["keyboard"] for button in row]
        self.assertEqual(admin_labels, ["⚙️ 推送设置", "🔔 申请列表", "👥 授权用户", "↩️ 重置筛选"])
        self.assertEqual(member_labels, ["⚙️ 推送设置", "↩️ 重置筛选"])

    def test_old_signal_mode_migrates_to_scope_and_is_never_written_back(self):
        migrated = normalize_preferences({"signal_mode": "qualified"})
        self.assertEqual(migrated["signal_scope"], "qualified")
        self.assertNotIn("signal_mode", migrated)

    def test_activity_and_qualified_scopes_are_explicit(self):
        strategy_filter = {"strategies": ["wt_extreme_cross"], "strategy_mode": "all"}
        activity = normalize_preferences({**strategy_filter, "signal_scope": "activity",
                                          "signals_enabled": True})
        strict = normalize_preferences({**strategy_filter, "signal_scope": "qualified",
                                        "signals_enabled": True})
        self.assertTrue(signal_matches(signal(), activity)[0])
        self.assertFalse(signal_matches(signal(), strict)[0])
        self.assertTrue(signal_matches(signal(qualified=True), strict)[0])
        activity_text = format_signal(signal(), activity, ["wt_extreme_cross"])
        strict_text = format_signal(signal(qualified=True), strict, ["wt_extreme_cross"])
        self.assertIn("合约推送｜强活跃异常", activity_text)
        self.assertIn("策略：WaveTrend · 超买超卖交叉", activity_text)
        self.assertIn("完整门槛：未通过", activity_text)
        self.assertIn("未通过原因：成本后空间不足；买卖价差过大", activity_text)
        self.assertIn("合约推送｜完整强信号", strict_text)
        self.assertIn("完整门槛：已通过", strict_text)

    def test_filters_and_multi_strategy_match_one_logical_event(self):
        base = normalize_preferences({"signals_enabled": True,
                                      "strategies": ["wt_extreme_cross"],
                                      "strategy_mode": "all"})
        self.assertEqual(signal_matches(signal(), base), (True, ["wt_extreme_cross"]))
        any_strategy = normalize_preferences({**base, "strategy_mode": "any"})
        one_pass = signal(available_confirmations=[
            {"id": "wt_extreme_cross", "passed": True}, {"id": "smc", "passed": False}])
        self.assertEqual(signal_matches(one_pass, any_strategy), (True, ["wt_extreme_cross"]))
        self.assertTrue(signal_matches(one_pass, base)[0])
        self.assertFalse(signal_matches(signal(timeframe="15m"), {**base, "timeframes": ["5m"]})[0])
        self.assertFalse(signal_matches(signal(activity_score=79), base)[0])
        self.assertFalse(signal_matches(signal(delayed=True), base)[0])

    def test_empty_strategy_is_explicit_and_not_mixed_with_indicators(self):
        raw = normalize_preferences({"strategies": [], "signals_enabled": True})
        self.assertTrue(signal_matches(signal(available_confirmations=[]), raw)[0])
        self.assertIn("空策略（不做指标过滤）", format_signal(signal(), raw, []))
        keyboard = menu_keyboard(raw)
        callbacks = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
        labels = [button["text"] for row in keyboard["inline_keyboard"] for button in row]
        self.assertTrue(any("WaveTrend" in label for label in labels))
        self.assertFalse(any("智慧资金结构" in label for label in labels))
        self.assertTrue(all(len(value.encode("utf-8")) <= 64 for value in callbacks))

    def test_listing_template_localizes_exchange_metadata(self):
        text = format_listing({
            "symbol": "HK0700USDT",
            "event_type": "listed",
            "contract_type": "TRADIFI_PERPETUAL",
            "underlying_type": "HK_EQUITY",
            "status": "TRADING",
            "onboard_date": 1788300000000,
        })
        self.assertIn("合约类型：传统金融永续合约", text)
        self.assertIn("资产类型：港股", text)
        self.assertIn("交易状态：交易中", text)
        self.assertIn("来源：币安 U 本位合约清单", text)
        self.assertNotIn("Binance USD-M exchangeInfo", text)

    def test_direction_uses_saved_trade_evidence_before_fallbacks(self):
        contradictory = signal(pattern="阳阳", direction="up", trade_plan={"direction": "short"})
        self.assertEqual(signal_direction(contradictory), "short")
        self.assertEqual(signal_direction(signal(trade_plan=None, entry_direction="short")), "short")
        self.assertEqual(signal_direction(signal(trade_plan=None, direction="down", pattern="阳阳")), "short")
        self.assertEqual(signal_direction(signal(trade_plan=None, direction="neutral", pattern="阴阳")), "long")

    def test_text_shows_daily_counts_and_never_calls_blocked_setup_actionable(self):
        preferences = normalize_preferences({"signals_enabled": True})
        text = format_signal(signal(
            daily_signal_count=5,
            daily_timeframe_signal_count=3,
            daily_direction_signal_count=4,
            daily_signal_date="2026-09-02",
            position_assessment={
                "blocked": True,
                "trade_direction": "long",
                "label": "震荡区间上沿 · 前方压力位",
                "reason": "距离阻力区过近",
                "advice": "等待回落到支撑区域，或放量突破后重新确认",
                "zone_low": 110.5,
                "zone_high": 112.0,
                "distance_pct": 0.12,
                "breakout_confirmed": False,
                "uses_closed_bars_only": True,
            }), preferences, [])
        self.assertIn("BTCUSDT · 5m · 观察（暂不入场）", text)
        self.assertIn("候选方向：做多（当前不建议入场）", text)
        self.assertIn("今日出现：第 5 次（同周期 3；同方向 4；北京时间隔日清零）", text)
        self.assertIn("位置判断：震荡区间上沿 · 前方压力位", text)
        self.assertIn("操作提示：等待回落到支撑区域", text)
        self.assertIn("关键区域：110.5 – 112", text)
        self.assertIn("距关键区域：0.12%", text)
        self.assertIn("判断口径：仅使用信号时点前的已收盘 K 线", text)


class TelegramRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.clients: list[FakeClient] = []

        def factory(token):
            client = FakeClient(token)
            self.clients.append(client)
            return client

        self.service = TelegramService(
            self.folder.name,
            client_factory=factory,
            token_vault=MemoryVault(),
            state=TelegramState(":memory:"),
        )

        async def no_wait(chat_id):
            return None

        self.service._rate_wait = no_wait

    async def asyncTearDown(self):
        await self.service.close()
        self.folder.cleanup()

    async def test_only_one_time_code_can_bind_and_other_chats_stay_unauthorized(self):
        connected = await self.service.connect()
        code = parse_qs(urlparse(connected["pairing_url"]).query)["start"][0]
        self.assertLessEqual(len(code), 64)
        await self.service._handle_update({"message": {
            "text": "/start wrong-code-that-is-long-enough-0000",
            "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        self.assertIsNone(self.service.state.subscriber())
        await self.service._handle_update({"message": {
            "text": f"/start {code}", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner", "username": "owner"},
        }})
        subscriber = self.service.state.subscriber()
        self.assertEqual(subscriber["chat_id"], 1001)
        self.assertTrue(self.service.state.authorized(1001, 1001))
        await self.service._handle_update({"message": {
            "text": f"/start {code}", "chat": {"id": 2002, "type": "private"},
            "from": {"id": 2002, "first_name": "Other"},
        }})
        self.assertEqual(self.service.state.subscriber()["chat_id"], 1001)
        status = self.service.safe_status()
        self.assertNotIn('"chat_id": 1001', json.dumps(status, ensure_ascii=False))
        self.assertEqual(status["chat_id_masked"], "****1001")
        self.assertNotIn("pairing_url", status)

    async def test_pending_owner_can_bind_by_pasted_command_and_clears_pending(self):
        connected = await self.service.connect()
        code = parse_qs(urlparse(connected['pairing_url']).query)['start'][0]
        message = {'text': '/start', 'chat': {'id': 1001, 'type': 'private'},
                   'from': {'id': 1001, 'first_name': 'Owner'}}
        await self.service._handle_update({'message': message})
        self.assertIsNone(self.service.state.administrator())
        notices = [payload.get('text', '') for method, payload in self.clients[0].calls if method == 'sendMessage']
        self.assertTrue(any('尚未绑定首位管理员' in value for value in notices))
        self.assertEqual(len(self.service.state.pending_requests()), 1)
        await self.service._handle_update({'message': {**message, 'text': f'/start {code}'}})
        self.assertEqual(self.service.state.administrator()['bound_user_id'], 1001)
        self.assertEqual(self.service.safe_status()['pending_count'], 0)
        self.assertEqual(self.service.safe_status()['authorized_count'], 1)

    async def test_preconnect_event_is_never_replayed_and_delivery_is_deduplicated(self):
        old = signal(id="old")
        self.assertFalse(self.service.enqueue("signal", old))
        code, _ = self.service.state.issue_pairing()
        active = normalize_preferences({"signals_enabled": True})
        self.assertTrue(self.service.state.consume_pairing(
            code, 1001, 1001, "Owner", active))
        await self.service.connect()
        self.assertEqual([call for call in self.clients[0].calls if call[0] == "sendMessage"], [])
        current = signal(id="new")
        self.assertTrue(self.service.enqueue("signal", current))
        self.assertFalse(self.service.enqueue("signal", current))
        await asyncio.wait_for(self.service._queue.join(), timeout=2)
        sends = [call for call in self.clients[0].calls if call[0] == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertIn("强活跃异常", sends[0][1]["text"])
        # A later duplicate callback reaches the worker but persistent delivery
        # dedupe prevents a second Telegram message.
        self.assertTrue(self.service.enqueue("signal", current))
        await asyncio.wait_for(self.service._queue.join(), timeout=2)
        self.assertEqual(len([call for call in self.clients[0].calls if call[0] == "sendMessage"]), 1)

    async def test_listing_switch_is_independent_from_signal_switch(self):
        preferences = normalize_preferences({"listing_enabled": False, "signals_enabled": True})
        self.service.set_preferences(preferences)
        code, _ = self.service.state.issue_pairing()
        self.service.state.consume_pairing(code, 1001, 1001, "Owner", preferences)
        await self.service.connect()
        self.assertTrue(self.service.enqueue("listing", {
            "id": "NEWUSDT:1:listed", "source": "live", "symbol": "NEWUSDT",
            "event_type": "listed", "observed_at": 1788300000000, "status": "TRADING"}))
        self.assertTrue(self.service.enqueue("signal", signal(id="signal-after-listing")))
        await asyncio.wait_for(self.service._queue.join(), timeout=2)
        sends = [call for call in self.clients[0].calls if call[0] == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertIn("BTCUSDT", sends[0][1]["text"])

    async def _bind_admin(self, chat_id=1001):
        connected = await self.service.connect()
        code = parse_qs(urlparse(connected["pairing_url"]).query)["start"][0]
        await self.service._handle_update({"message": {
            "text": f"/start {code}", "chat": {"id": chat_id, "type": "private"},
            "from": {"id": chat_id, "first_name": "Owner", "username": "owner"},
        }})
        self.clients[0].calls.clear()

    async def test_unknown_private_user_applies_once_and_only_admin_can_approve(self):
        await self._bind_admin()
        application = {"message": {
            "text": "/start", "chat": {"id": 2002, "type": "private"},
            "from": {"id": 2002, "first_name": "Guest", "last_name": "Two",
                     "username": "guest_two"},
        }}
        await self.service._handle_update(application)
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertEqual(len(sends), 2)
        admin_notice = next(payload for payload in sends if payload["chat_id"] == 1001)
        self.assertIn("Telegram ID：2002", admin_notice["text"])
        self.assertIn("@guest_two", admin_notice["text"])
        self.assertEqual(admin_notice["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "a:ok:2002")
        # A repeated command neither creates a second request nor notifies the
        # administrator again (and the requester notice is throttled).
        self.clients[0].calls.clear()
        await self.service._handle_update(application)
        self.assertFalse(any(method == "sendMessage" for method, _ in self.clients[0].calls))

        forged = {"id": "q-forged", "data": "a:ok:2002", "from": {"id": 2002},
                  "message": {"message_id": 8, "chat": {"id": 2002, "type": "private"}}}
        await self.service._handle_callback(forged)
        self.assertFalse(self.service.state.authorized(2002, 2002))
        answers = [payload for method, payload in self.clients[0].calls if method == "answerCallbackQuery"]
        self.assertEqual(answers[-1]["text"], "只有管理员可以审批")

        approved = {"id": "q-admin", "data": "a:ok:2002", "from": {"id": 1001},
                    "message": {"message_id": 9, "chat": {"id": 1001, "type": "private"}}}
        await self.service._handle_callback(approved)
        self.assertTrue(self.service.state.authorized(2002, 2002))
        approved_member = self.service.state.subscriber(user_id=2002)
        self.assertEqual(approved_member["role"], "user")
        self.assertFalse(approved_member["preferences"]["signals_enabled"])
        self.assertFalse(approved_member["preferences"]["listing_enabled"])
        self.assertEqual(self.service.state.pending_requests(), [])

    async def test_invalid_old_pairing_link_becomes_request_after_admin_exists(self):
        await self._bind_admin()
        await self.service._handle_update({"message": {
            "text": "/start invalid-old-code-that-is-long-enough",
            "chat": {"id": 2442, "type": "private"},
            "from": {"id": 2442, "first_name": "Late Guest"},
        }})
        self.assertEqual([item["user_id"] for item in self.service.state.pending_requests()], [2442])

    async def test_rejected_user_cannot_reopen_request_or_renotify_admin(self):
        await self._bind_admin()
        application = {"message": {
            "text": "/start", "chat": {"id": 2002, "type": "private"},
            "from": {"id": 2002, "first_name": "Denied", "username": "denied"},
        }}
        await self.service._handle_update(application)
        rejected = {"id": "q-reject", "data": "a:no:2002", "from": {"id": 1001},
                    "message": {"message_id": 7, "chat": {"id": 1001, "type": "private"}}}
        await self.service._handle_callback(rejected)
        self.clients[0].calls.clear()
        self.service._unauthorized_notice_at.pop(2002, None)
        await self.service._handle_update(application)
        self.assertEqual(self.service.state.pending_requests(), [])
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertEqual([payload["chat_id"] for payload in sends], [2002])
        self.assertIn("申请未通过", sends[0]["text"])
        self.assertNotIn("申请已提交", sends[0]["text"])

    async def test_removed_user_cannot_reapply_and_only_admin_add_restores(self):
        await self._bind_admin()
        self.service.state.add_user(3003, 1001, DEFAULT_PREFERENCES)
        self.assertIsNotNone(self.service.state.remove_user(3003, 1001))
        self.clients[0].calls.clear()
        await self.service._handle_update({"message": {
            "text": "/menu", "chat": {"id": 3003, "type": "private"},
            "from": {"id": 3003, "first_name": "Removed"},
        }})
        self.assertEqual(self.service.state.pending_requests(), [])
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertEqual([payload["chat_id"] for payload in sends], [3003])
        self.assertIn("权限已取消", sends[0]["text"])
        self.assertNotIn("申请已提交", sends[0]["text"])
        self.service.state.add_user(3003, 1001, DEFAULT_PREFERENCES)
        self.assertTrue(self.service.state.authorized(3003, 3003))

    async def test_program_rejects_groups_before_binding_or_access_request(self):
        await self._bind_admin()
        await self.service._handle_update({"message": {
            "text": "/start", "chat": {"id": -9988, "type": "supergroup"},
            "from": {"id": 2002, "first_name": "Guest"},
        }})
        self.assertEqual(self.service.state.pending_requests(), [])
        self.assertFalse(any(method == "sendMessage" for method, _ in self.clients[0].calls))

    async def test_each_authorized_user_has_independent_preferences_and_delivery(self):
        await self._bind_admin()
        self.service.state.add_user(2002, 1001, normalize_preferences({
            "signals_enabled": True, "directions": ["short"], "min_activity": 65,
        }))
        admin_preferences = normalize_preferences({
            "signals_enabled": True, "directions": ["long"], "min_activity": 80,
        })
        self.service.state.update_preferences(admin_preferences, chat_id=1001)
        self.assertNotEqual(self.service.state.subscriber(user_id=1001)["preferences"]["directions"],
                            self.service.state.subscriber(user_id=2002)["preferences"]["directions"])
        self.clients[0].calls.clear()
        self.assertTrue(self.service.enqueue("signal", signal(id="long-only")))
        await asyncio.wait_for(self.service._queue.join(), timeout=2)
        recipients = [payload["chat_id"] for method, payload in self.clients[0].calls
                      if method == "sendMessage"]
        self.assertEqual(recipients, [1001])

    async def test_admin_commands_add_list_and_remove_without_removing_admin(self):
        await self._bind_admin()
        await self.service._handle_update({"message": {
            "text": "/add 3003", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        self.assertTrue(self.service.state.authorized(3003, 3003))
        await self.service._handle_update({"message": {
            "text": "/users", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        texts = [payload["text"] for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertTrue(any("ID 3003" in value for value in texts))
        await self.service._handle_update({"message": {
            "text": "/remove 3003", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        self.assertFalse(self.service.state.authorized(3003, 3003))
        await self.service._handle_update({"message": {
            "text": "/remove 1001", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        self.assertTrue(self.service.state.is_admin(1001, 1001))

    async def test_admin_shortcut_lists_each_pending_request_with_approval_buttons(self):
        await self._bind_admin()
        for user_id, name in ((2002, "Guest Two"), (3003, "Guest Three")):
            request, _ = self.service.state.request_access(user_id, user_id, "", name)
            self.service.state.mark_request_notified(request["user_id"])
        await self.service._handle_update({"message": {
            "text": "🔔 申请列表", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertEqual(len(sends), 3)
        self.assertIn("共 2 人待审核", sends[0]["text"])
        self.assertNotIn("reply_markup", sends[0])
        self.assertIn("显示名：Guest Two", sends[1]["text"])
        self.assertIn("显示名：Guest Three", sends[2]["text"])
        callbacks = [
            [button["callback_data"] for button in payload["reply_markup"]["inline_keyboard"][0]]
            for payload in sends[1:]
        ]
        self.assertEqual(callbacks, [["a:ok:2002", "a:no:2002"],
                                     ["a:ok:3003", "a:no:3003"]])

        # Approving the first card edits only its own message. The second card
        # remains a separate Telegram message with usable callback buttons.
        self.clients[0].calls.clear()
        await self.service._handle_callback({
            "id": "approve-first", "data": "a:ok:2002", "from": {"id": 1001},
            "message": {"message_id": 41, "chat": {"id": 1001, "type": "private"}},
        })
        edits = [payload for method, payload in self.clients[0].calls if method == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["message_id"], 41)
        self.assertTrue(any(item["user_id"] == 3003 for item in self.service.state.pending_requests()))

    async def test_personal_pure_rule_callbacks_preserve_other_users_and_page(self):
        await self._bind_admin()
        self.service.state.add_user(2002,1001,normalize_preferences({}))
        original=self.service.state.subscriber(chat_id=1001)['preferences']
        async def click(data):
            await self.service._handle_callback({'id':'test-rule','data':data,'from':{'id':2002},
                'message':{'message_id':55,'chat':{'id':2002,'type':'private'}}})
        await click('p:source:indicator')
        await click('p:rules:all')
        await click('p:tf:all')
        await click('p:pattern:all')
        await click('p:book')
        p=self.service.state.subscriber(chat_id=2002)['preferences']
        self.assertEqual(set(p['timeframes']),{'1m','5m','15m','30m','1h','4h','1d','1w','1M'})
        self.assertEqual(set(p['rules']),{'wt_extreme_cross','nan_strategy1_oversold'})
        self.assertEqual(len(p['patterns']),4)
        self.assertTrue(p['require_book'])
        self.assertFalse(p['signals_enabled'])
        await click('p:page:2')
        edits=[p for method,p in self.clients[0].calls if method=='editMessageText']
        self.assertTrue(any('1/1' in b['text'] for row in edits[-1]['reply_markup']['inline_keyboard'] for b in row))
        self.assertEqual(self.service.state.subscriber(chat_id=1001)['preferences'],original)

    async def test_reset_shortcut_resets_only_filters_for_each_user(self):
        await self._bind_admin()
        custom = normalize_preferences({
            "timeframes": ["5m"], "directions": ["short"], "min_activity": 90,
            "signal_scope": "qualified", "strategies": ["wt_extreme_cross"], "strategy_mode": "all",
            "signals_enabled": True, "listing_enabled": False,
        })
        self.service.state.update_preferences(custom, chat_id=1001)
        self.clients[0].calls.clear()
        await self.service._handle_update({"message": {
            "text": "↩️ 重置筛选", "chat": {"id": 1001, "type": "private"},
            "from": {"id": 1001, "first_name": "Owner"},
        }})
        reset = self.service.state.subscriber(user_id=1001)["preferences"]
        self.assertEqual(reset, reset_filter_preferences(custom))
        self.assertTrue(reset["signals_enabled"])
        self.assertFalse(reset["listing_enabled"])
        messages = [payload["text"] for method, payload in self.clients[0].calls
                    if method == "sendMessage"]
        self.assertTrue(any("两个开关保持原状" in text for text in messages))

    async def test_local_action_refreshes_admin_shortcuts_and_latest_inline_menu(self):
        await self._bind_admin()
        result = await self.service.refresh_admin_menu()
        self.assertEqual(result, {"sent": True, "masked_chat_id": "****1001"})
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        self.assertEqual(len(sends), 2)
        self.assertIn("keyboard", sends[0]["reply_markup"])
        self.assertIn("inline_keyboard", sends[1]["reply_markup"])

    async def test_authorized_member_quick_menu_has_no_admin_buttons(self):
        await self._bind_admin()
        self.service.state.add_user(2002, 1001, DEFAULT_PREFERENCES)
        self.clients[0].calls.clear()
        await self.service._handle_update({"message": {
            "text": "/menu", "chat": {"id": 2002, "type": "private"},
            "from": {"id": 2002, "first_name": "Member"},
        }})
        sends = [payload for method, payload in self.clients[0].calls if method == "sendMessage"]
        shortcut = next(payload for payload in sends if "keyboard" in payload.get("reply_markup", {}))
        labels = [button["text"] for row in shortcut["reply_markup"]["keyboard"] for button in row]
        self.assertEqual(labels, ["⚙️ 推送设置", "↩️ 重置筛选"])


class TelegramPersistenceTests(unittest.TestCase):
    def test_single_subscriber_database_migrates_owner_to_admin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.sqlite3"
            database = __import__("sqlite3").connect(path)
            database.execute("""
                CREATE TABLE subscribers (
                    chat_id INTEGER PRIMARY KEY, bound_user_id INTEGER NOT NULL,
                    chat_label TEXT NOT NULL, preferences TEXT NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                )
            """)
            database.execute("INSERT INTO subscribers VALUES (?,?,?,?,?,?)", (
                1001, 1001, "Existing owner", json.dumps(DEFAULT_PREFERENCES), 1, 1))
            database.commit()
            database.close()
            migrated = TelegramState(path)
            self.assertTrue(migrated.is_admin(1001, 1001))
            self.assertEqual(migrated.subscriber()["role"], "admin")
            migrated.close()

    def test_members_requests_and_audit_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram.sqlite3"
            state = TelegramState(path)
            code, _ = state.issue_pairing()
            self.assertTrue(state.consume_pairing(code, 1001, 1001, "Owner", DEFAULT_PREFERENCES))
            state.request_access(2002, 2002, "guest", "Guest")
            self.assertTrue(state.mark_request_notified(2002))
            self.assertIsNotNone(state.decide_request(2002, 1001, True, DEFAULT_PREFERENCES))
            state.close()
            reopened = TelegramState(path)
            self.assertTrue(reopened.is_admin(1001, 1001))
            self.assertTrue(reopened.authorized(2002, 2002))
            audit = reopened._db.execute("SELECT action,target_user_id FROM access_audit").fetchall()
            self.assertEqual([tuple(row) for row in audit],
                             [("requested", 2002), ("approved", 2002)])
            reopened.close()


class FakeResponse:
    def __init__(self, status, data):
        self.status = status
        self.data = data

    async def json(self, content_type=None):
        return self.data


class FakeRequestContext:
    def __init__(self, response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def post(self, url, json):
        self.urls.append(url)
        return FakeRequestContext(self.responses.pop(0))


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_429_retry_after_is_honored_with_mock_transport(self):
        session = FakeSession([
            FakeResponse(429, {"ok": False, "error_code": 429,
                               "parameters": {"retry_after": 3}}),
            FakeResponse(200, {"ok": True, "result": {"message_id": 1}}),
        ])
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        client = BotApiClient(TOKEN, session=session, sleep=fake_sleep)
        await client.start()
        result = await client.call("sendMessage", {"chat_id": 1, "text": "test"})
        self.assertEqual(result, {"message_id": 1})
        self.assertEqual(sleeps, [3.0])
        self.assertEqual(len(session.urls), 2)

    async def test_upstream_error_description_and_url_are_not_exposed(self):
        session = FakeSession([FakeResponse(400, {
            "ok": False, "error_code": 400,
            "description": f"bad request at https://api.telegram.org/bot{TOKEN}/sendMessage",
        })])
        client = BotApiClient(TOKEN, session=session)
        with self.assertRaises(TelegramError) as raised:
            await client.call("sendMessage", {"chat_id": 1, "text": "test"})
        self.assertNotIn(TOKEN, str(raised.exception))
        self.assertNotIn("https://", str(raised.exception))
        self.assertEqual(str(raised.exception), "Telegram API 请求失败（400）")


@unittest.skipUnless(os.name == "nt", "Windows DPAPI only")
class DpapiTests(unittest.TestCase):
    def test_token_round_trip_is_ciphertext_and_not_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "telegram-token.dpapi"
            vault = BotTokenVault(path)
            vault.save(TOKEN)
            self.assertEqual(vault.load(), TOKEN)
            self.assertNotIn(TOKEN.encode("utf-8"), path.read_bytes())
            self.assertEqual(vault.source(), "windows_dpapi")

    def test_environment_token_cannot_be_silently_overridden(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
                os.environ, {"PULSE_TELEGRAM_BOT_TOKEN": TOKEN}):
            path = Path(folder) / "telegram-token.dpapi"
            vault = BotTokenVault(path)
            with self.assertRaisesRegex(TokenStorageError, "来自环境变量"):
                vault.save("987654321:" + "B" * 35)
            self.assertFalse(path.exists())


class TelegramApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.environment = patch.dict(os.environ, {
            "PULSE_DATA_DIR": self.folder.name,
            "PULSE_DISABLE_COLLECTOR": "1",
        })
        self.environment.start()
        self.start_patch = patch.object(MonitorService, "start", side_effect=AssertionError("collector must stay offline"))
        self.start_mock = self.start_patch.start()
        self.module_name = "monitor._isolated_telegram_api_test"
        path = Path(__file__).resolve().parents[1] / "monitor" / "api.py"
        spec = importlib.util.spec_from_file_location(self.module_name, path)
        self.api = importlib.util.module_from_spec(spec)
        sys.modules[self.module_name] = self.api
        spec.loader.exec_module(self.api)
        self.lock_patch=patch("monitor.instance_lock.InstanceLock.acquire")
        self.lock_patch.start();self.addCleanup(self.lock_patch.stop)
        self.client = TestClient(self.api.app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.start_mock.assert_not_called()
        self.start_patch.stop()
        self.environment.stop()
        sys.modules.pop(self.module_name, None)
        self.folder.cleanup()

    def test_get_never_returns_token_and_put_saves_preferences(self):
        initial = self.client.get("/api/telegram/settings")
        self.assertEqual(initial.status_code, 200)
        self.assertNotIn("bot_token", initial.json())
        self.assertFalse(initial.json()["preferences"]["signals_enabled"])
        self.assertTrue(initial.json()["preferences"]["listing_enabled"])
        preferences = {**DEFAULT_PREFERENCES, "timeframes": ["5m"],
                       "directions": ["short"], "min_activity": 65,
                       "signal_scope": "qualified", "strategies": ["wt_extreme_cross"],
                       "strategy_mode": "any", "listing_enabled": False}
        with patch("monitor.telegram._crypt_protect", return_value=b"encrypted-token"):
            saved = self.client.put("/api/telegram/settings", headers=HEADERS,
                                    json={"bot_token": TOKEN, "preferences": preferences})
        self.assertEqual(saved.status_code, 200)
        serialized = json.dumps(saved.json(), ensure_ascii=False)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn("bot_token", saved.json())
        self.assertTrue(saved.json()["token_configured"])
        self.assertEqual(saved.json()["preferences"]["signal_scope"], "qualified")
        self.assertEqual((Path(self.folder.name) / "telegram-token.dpapi").read_bytes(), b"encrypted-token")
        after = self.client.get("/api/telegram/settings")
        self.assertNotIn(TOKEN, json.dumps(after.json(), ensure_ascii=False))
        self.assertNotIn("bot_token", after.json())

    def test_independent_profiles_round_trip_in_api_without_sending(self):
        from monitor.live_rule_signals import rule_catalog
        profiles=[dict(kind=k,rule_id=rule_catalog(k)[0]['id'],timeframes=[tf],min_activity=37,min_actionability=62,patterns=[],symbols=symbols) for k,tf,symbols in [('indicator','1h',['BTCUSDT'])]]
        response=self.client.put('/api/telegram/settings',headers=HEADERS,json={'preferences':{**DEFAULT_PREFERENCES,'rule_settings':profiles}})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.get('/api/telegram/settings').json()['preferences']['rule_settings'],profiles)
        profiles[0]['timeframes']=[]
        bad=self.client.put('/api/telegram/settings',headers=HEADERS,json={'preferences':{**DEFAULT_PREFERENCES,'rule_settings':profiles}})
        self.assertEqual(bad.status_code,422)

    def test_connect_uses_mock_bot_api_and_only_returns_one_time_pairing_link(self):
        with patch("monitor.telegram._crypt_protect", return_value=b"encrypted-token"):
            saved = self.client.put("/api/telegram/settings", headers=HEADERS,
                                    json={"bot_token": TOKEN})
        self.assertEqual(saved.status_code, 200)
        clients = []

        def factory(token):
            client = FakeClient(token)
            clients.append(client)
            return client

        self.api.telegram_service._client_factory = factory
        with patch("monitor.telegram._crypt_unprotect", return_value=TOKEN.encode("utf-8")):
            response = self.client.post("/api/telegram/connect", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["connected"])
        self.assertTrue(body["pairing_url"].startswith("https://t.me/pulse_test_bot?start="))
        self.assertGreater(body["pairing_expires_at"], body["connected_at"])
        self.assertNotIn(TOKEN, json.dumps(body, ensure_ascii=False))
        self.assertFalse(any(method == "sendMessage" for method, _ in clients[0].calls))
        status = self.client.get("/api/telegram/settings").json()
        self.assertNotIn("pairing_url", status)
        self.assertTrue(status["pairing_pending"])
        disconnected = self.client.post("/api/telegram/disconnect", headers=HEADERS)
        self.assertEqual(disconnected.status_code, 200)
        self.assertFalse(disconnected.json()["connected"])
        self.assertFalse(disconnected.json()["pairing_pending"])

    def test_write_and_actions_require_local_dashboard_marker(self):
        for method, path in (("put", "/api/telegram/settings"),
                             ("post", "/api/telegram/connect"),
                             ("post", "/api/telegram/test"),
                             ("post", "/api/telegram/admin-menu"),
                             ("post", "/api/telegram/disconnect"),
                             ("post", "/api/telegram/unpair")):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path, json={} if method == "put" else None)
                self.assertEqual(response.status_code, 403)

    def test_admin_menu_endpoint_is_explicit_and_reports_safe_result(self):
        refresh = AsyncMock(return_value={"sent": True, "masked_chat_id": "***001"})
        with patch.object(self.api.telegram_service, "refresh_admin_menu", refresh):
            response = self.client.post("/api/telegram/admin-menu", headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["sent"])
        self.assertIn("输入框下方", response.json()["message"])
        self.assertIsInstance(response.json()["sent_at"], int)
        refresh.assert_awaited_once_with()

    def test_admin_menu_endpoint_returns_safe_conflict_when_unavailable(self):
        refresh = AsyncMock(side_effect=TelegramError("Telegram 尚未连接"))
        with patch.object(self.api.telegram_service, "refresh_admin_menu", refresh):
            response = self.client.post("/api/telegram/admin-menu", headers=HEADERS)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "Telegram 尚未连接")

    def test_invalid_token_response_does_not_echo_secret(self):
        secret = "definitely-not-a-valid-token"
        response = self.client.put("/api/telegram/settings", headers=HEADERS,
                                   json={"bot_token": secret})
        self.assertEqual(response.status_code, 400)
        self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
