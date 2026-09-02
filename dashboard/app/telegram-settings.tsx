'use client';

import { useCallback, useEffect, useState } from 'react';
import {
  AlertTriangle,
  Bell,
  CheckCircle2,
  Copy,
  ExternalLink,
  RefreshCw,
  Send,
  ShieldCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';

type TelegramTimeframe = '5m' | '15m';
type TelegramDirection = 'long' | 'short';
type TelegramStrategy = 'supertrend' | 'smc';
type TelegramStrategyMode = 'any' | 'all';
type TelegramActivityThreshold = 65 | 80 | 90;
type TelegramSignalScope = 'activity' | 'qualified';

export type TelegramPreferences = {
  timeframes: TelegramTimeframe[];
  directions: TelegramDirection[];
  min_activity: TelegramActivityThreshold;
  strategies: TelegramStrategy[];
  strategy_mode: TelegramStrategyMode;
  signal_scope: TelegramSignalScope;
  listing_enabled: boolean;
  signals_enabled: boolean;
};

export type TelegramSettingsSnapshot = {
  enabled?: boolean;
  connected?: boolean;
  token_configured?: boolean;
  token_source?: string | null;
  bot?: { username?: string | null; name?: string | null } | null;
  subscriber?: {
    paired?: boolean;
    chat_label?: string | null;
    masked_chat_id?: string | null;
  } | null;
  preferences?: Partial<TelegramPreferences> | null;
  authorized_count?: number;
  pending_count?: number;
  access_control?: {
    private_only?: boolean;
    authorized_users?: number;
    pending_requests?: number;
  } | null;
};

type PairingResponse = {
  connected?: boolean;
  pairing_url?: string | null;
  pairing_expires_at?: string | number | null;
};

type Feedback = {
  tone: 'neutral' | 'success' | 'error';
  text: string;
};

const DEFAULT_PREFERENCES: TelegramPreferences = {
  timeframes: ['5m', '15m'],
  directions: ['long', 'short'],
  min_activity: 80,
  strategies: [],
  strategy_mode: 'any',
  signal_scope: 'activity',
  listing_enabled: true,
  signals_enabled: false,
};

const STRATEGIES: {
  id: TelegramStrategy;
  name: string;
  description: string;
}[] = [
  {
    id: 'supertrend',
    name: '指标 1 · 超级趋势',
    description: '用 ATR 趋势方向确认多空。',
  },
  {
    id: 'smc',
    name: '指标 2 · 智慧资金结构',
    description:
      '先检查支撑压力、订单块与区间位置；压力区不追多，支撑区不追空。',
  },
];

const parseError = async (response: Response, fallback: string) => {
  try {
    const data = (await response.json()) as {
      detail?: string;
      message?: string;
    };
    return data.detail || data.message || fallback;
  } catch {
    return fallback;
  }
};

const normalizePreferences = (
  value?: Partial<TelegramPreferences> | null,
): TelegramPreferences => ({
  timeframes:
    value?.timeframes?.filter(
      (item): item is TelegramTimeframe => item === '5m' || item === '15m',
    ) || DEFAULT_PREFERENCES.timeframes,
  directions:
    value?.directions?.filter(
      (item): item is TelegramDirection => item === 'long' || item === 'short',
    ) || DEFAULT_PREFERENCES.directions,
  min_activity:
    value?.min_activity === 65 ||
    value?.min_activity === 80 ||
    value?.min_activity === 90
      ? value.min_activity
      : DEFAULT_PREFERENCES.min_activity,
  strategies:
    value?.strategies?.filter(
      (item): item is TelegramStrategy =>
        item === 'supertrend' || item === 'smc',
    ) || DEFAULT_PREFERENCES.strategies,
  strategy_mode:
    value?.strategy_mode === 'all' ? 'all' : DEFAULT_PREFERENCES.strategy_mode,
  signal_scope:
    value?.signal_scope === 'qualified'
      ? 'qualified'
      : DEFAULT_PREFERENCES.signal_scope,
  listing_enabled:
    value?.listing_enabled ?? DEFAULT_PREFERENCES.listing_enabled,
  signals_enabled:
    value?.signals_enabled ?? DEFAULT_PREFERENCES.signals_enabled,
});

const telegramTokenLooksValid = (value: string) =>
  /^\d{6,16}:[A-Za-z0-9_-]{30,128}$/.test(value.trim());

const pairingExpiryTimestamp = (value?: string | number | null) => {
  if (value == null || value === '') return 0;
  if (typeof value === 'number')
    return value < 1_000_000_000_000 ? value * 1000 : value;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : 0;
};

export function TelegramSettingsPanel({
  api,
  online,
}: {
  api?: string | null;
  online?: boolean;
}) {
  const [snapshot, setSnapshot] = useState<TelegramSettingsSnapshot>({});
  const [preferences, setPreferences] =
    useState<TelegramPreferences>(DEFAULT_PREFERENCES);
  const [botToken, setBotToken] = useState('');
  const [pairing, setPairing] = useState<PairingResponse>({});
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [adminMenuBusy, setAdminMenuBusy] = useState(false);
  const [adminMenuFeedback, setAdminMenuFeedback] = useState<Feedback | null>(
    null,
  );
  const [feedback, setFeedback] = useState<Feedback>({
    tone: 'neutral',
    text: '本页不会自动发测试消息；在 Telegram 点击 Start 时，机器人会回复绑定确认。',
  });

  const loadSettings = useCallback(
    async (quiet = false) => {
      if (!api || !online) return null;
      if (!quiet) setLoading(true);
      try {
        const response = await fetch(api + '/api/telegram/settings', {
          signal: AbortSignal.timeout(6000),
        });
        if (!response.ok)
          throw new Error(
            await parseError(response, 'Telegram 设置暂不可用。'),
          );
        const next = (await response.json()) as TelegramSettingsSnapshot;
        setSnapshot(next);
        if (next.preferences) {
          const normalized = normalizePreferences(next.preferences);
          setPreferences(
            next.subscriber?.paired
              ? normalized
              : {
                  ...normalized,
                  signals_enabled: false,
                },
          );
        }
        if (next.subscriber?.paired) {
          setPairing({});
          if (!quiet)
            setFeedback({
              tone: 'success',
              text: '已读取到安全绑定状态。连接检查没有发送测试消息。',
            });
        }
        return next;
      } catch (error) {
        if (!quiet)
          setFeedback({
            tone: 'error',
            text:
              error instanceof Error
                ? error.message
                : 'Telegram 设置暂不可用。',
          });
        return null;
      } finally {
        if (!quiet) setLoading(false);
      }
    },
    [api, online],
  );

  useEffect(() => {
    const timer = window.setTimeout(() => void loadSettings(true), 0);
    return () => window.clearTimeout(timer);
  }, [loadSettings]);

  useEffect(() => {
    if (!api || !online || !pairing.pairing_url || snapshot.subscriber?.paired)
      return;
    const pollTimer = window.setInterval(() => void loadSettings(true), 4000);
    const expiresAt = pairingExpiryTimestamp(pairing.pairing_expires_at);
    const expiryTimer = expiresAt
      ? window.setTimeout(
          () => {
            setPairing({});
            setFeedback({
              tone: 'neutral',
              text: '安全绑定链接已过期，没有完成绑定。请重新生成。',
            });
          },
          Math.max(0, expiresAt - Date.now()),
        )
      : undefined;
    return () => {
      window.clearInterval(pollTimer);
      if (expiryTimer != null) window.clearTimeout(expiryTimer);
    };
  }, [
    api,
    online,
    pairing.pairing_url,
    pairing.pairing_expires_at,
    snapshot.subscriber?.paired,
    loadSettings,
  ]);

  useEffect(() => {
    if (!api || !online || !snapshot.subscriber?.paired) return;
    const timer = window.setInterval(() => void loadSettings(true), 10_000);
    return () => window.clearInterval(timer);
  }, [api, online, snapshot.subscriber?.paired, loadSettings]);

  const setListValue = <T extends string>(
    key: 'timeframes' | 'directions' | 'strategies',
    item: T,
    checked: boolean,
  ) => {
    setPreferences((current) => {
      const existing = current[key] as T[];
      const next = checked
        ? Array.from(new Set([...existing, item]))
        : existing.filter((value) => value !== item);
      return { ...current, [key]: next } as TelegramPreferences;
    });
  };

  const validateChoices = () => {
    if (!preferences.timeframes.length) return '至少选择一个推送周期。';
    if (!preferences.directions.length) return '至少选择一个信号方向。';
    return '';
  };

  const savePreferences = async () => {
    const choiceError = validateChoices();
    if (choiceError) {
      setFeedback({ tone: 'error', text: choiceError });
      return;
    }
    if (!api || !online) {
      setFeedback({
        tone: 'neutral',
        text: '在线预览只展示设置界面；请在本机看板连接服务后保存。',
      });
      return;
    }
    setSaving(true);
    try {
      const safePreferences = subscriberPaired
        ? preferences
        : {
            ...preferences,
            signals_enabled: false,
          };
      const response = await fetch(api + '/api/telegram/settings', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-Pulse-Client': 'dashboard',
        },
        body: JSON.stringify({ preferences: safePreferences }),
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok)
        throw new Error(await parseError(response, '保存推送规则失败。'));
      const next = (await response.json()) as TelegramSettingsSnapshot;
      setSnapshot(next);
      setPreferences(normalizePreferences(next.preferences || safePreferences));
      setFeedback({
        tone: 'success',
        text: '推送规则已保存。没有发送 Telegram 消息。',
      });
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : '保存推送规则失败。',
      });
    } finally {
      setSaving(false);
    }
  };

  const createPairingLink = async () => {
    const tokenReady = snapshot.token_configured || botToken.trim();
    if (!tokenReady) {
      setFeedback({ tone: 'error', text: '请先填写 Bot Token。' });
      return;
    }
    if (botToken.trim() && !telegramTokenLooksValid(botToken)) {
      setFeedback({
        tone: 'error',
        text: 'Bot Token 格式不完整，请重新复制 BotFather 提供的整段内容。',
      });
      return;
    }
    const choiceError = validateChoices();
    if (choiceError) {
      setFeedback({ tone: 'error', text: choiceError });
      return;
    }
    if (!api || !online) {
      setFeedback({
        tone: 'neutral',
        text: '在线预览不会保存密钥或创建绑定链接，请在本机看板操作。',
      });
      return;
    }
    setSaving(true);
    try {
      const safePreferences = snapshot.subscriber?.paired
        ? preferences
        : { ...preferences, signals_enabled: false };
      const saveResponse = await fetch(api + '/api/telegram/settings', {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
          'X-Pulse-Client': 'dashboard',
        },
        body: JSON.stringify({
          ...(botToken.trim() ? { bot_token: botToken.trim() } : {}),
          preferences: safePreferences,
        }),
        signal: AbortSignal.timeout(8000),
      });
      if (!saveResponse.ok)
        throw new Error(
          await parseError(saveResponse, '保存 Bot Token 失败。'),
        );
      const saved = (await saveResponse.json()) as TelegramSettingsSnapshot;
      setSnapshot(saved);
      setPreferences(
        normalizePreferences(saved.preferences || safePreferences),
      );
      setBotToken('');

      const pairResponse = await fetch(api + '/api/telegram/connect', {
        method: 'POST',
        headers: { 'X-Pulse-Client': 'dashboard' },
        signal: AbortSignal.timeout(8000),
      });
      if (!pairResponse.ok)
        throw new Error(
          await parseError(pairResponse, '创建安全绑定链接失败。'),
        );
      const pair = (await pairResponse.json()) as PairingResponse;
      setPairing(pair);
      if (pair.pairing_url) {
        setFeedback({
          tone: 'success',
          text: '安全绑定链接已生成。请在 10 分钟内打开 Telegram 并点击 Start。',
        });
      } else if (pair.connected) {
        await loadSettings(true);
      } else {
        throw new Error('未收到有效的安全绑定链接。');
      }
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : '创建安全绑定链接失败。',
      });
    } finally {
      setSaving(false);
    }
  };

  const copyPreview = async () => {
    try {
      await navigator.clipboard.writeText(preview);
      setFeedback({ tone: 'success', text: '文字预览已复制。' });
    } catch {
      setFeedback({ tone: 'error', text: '复制失败，请手动选择预览文字。' });
    }
  };

  const postTelegramAction = async (
    action: 'test' | 'unpair' | 'disconnect',
  ) => {
    if (!api || !online) {
      setFeedback({
        tone: 'neutral',
        text: '请在本机看板连接服务后操作。',
      });
      return;
    }
    setSaving(true);
    try {
      const response = await fetch(api + `/api/telegram/${action}`, {
        method: 'POST',
        headers: { 'X-Pulse-Client': 'dashboard' },
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) {
        const fallback =
          action === 'test'
            ? '发送测试文字失败。'
            : action === 'unpair'
              ? '解除聊天绑定失败。'
              : '断开机器人失败。';
        throw new Error(await parseError(response, fallback));
      }
      if (action !== 'test') setPairing({});
      await loadSettings(true);
      setFeedback({
        tone: 'success',
        text:
          action === 'test'
            ? '测试文字已发送到已绑定聊天。'
            : action === 'unpair'
              ? '已解除聊天绑定。'
              : '已断开 Telegram 机器人。',
      });
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : 'Telegram 操作失败。',
      });
    } finally {
      setSaving(false);
    }
  };

  const confirmTelegramAction = (action: 'unpair' | 'disconnect') => {
    const confirmed = window.confirm(
      action === 'unpair'
        ? '确定清空管理员绑定、全部白名单和待审申请吗？清空后所有人都不会再收到推送。'
        : '确定断开 Telegram 机器人吗？聊天绑定会保留，但断开期间不会推送。',
    );
    if (confirmed) void postTelegramAction(action);
  };

  const botConnected = online === true && snapshot.connected === true;
  const subscriberPaired =
    online === true && snapshot.subscriber?.paired === true;
  const readyToSend = botConnected && subscriberPaired;
  const tokenManagedByEnvironment =
    snapshot.token_source === 'env' || snapshot.token_source === 'environment';
  const tokenInputLocked = tokenManagedByEnvironment || readyToSend;
  const sendAdminMenu = async () => {
    if (!api || !online || !readyToSend) {
      setAdminMenuFeedback({
        tone: 'neutral',
        text: '机器人恢复连接后可发送管理员菜单。',
      });
      return;
    }
    setAdminMenuBusy(true);
    setAdminMenuFeedback({
      tone: 'neutral',
      text: '正在发送最新管理员菜单…',
    });
    try {
      const response = await fetch(api + '/api/telegram/admin-menu', {
        method: 'POST',
        headers: { 'X-Pulse-Client': 'dashboard' },
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok)
        throw new Error(
          await parseError(response, '发送管理员菜单失败。'),
        );
      setAdminMenuFeedback({
        tone: 'success',
        text: '最新管理员菜单已发送到管理员私聊。',
      });
    } catch (error) {
      setAdminMenuFeedback({
        tone: 'error',
        text:
          error instanceof Error
            ? `发送失败：${error.message}`
            : '发送管理员菜单失败。',
      });
    } finally {
      setAdminMenuBusy(false);
    }
  };
  const periodExample = preferences.timeframes.includes('5m')
    ? '5分钟'
    : preferences.timeframes.includes('15m')
      ? '15分钟'
      : '未选择';
  const directionExample = preferences.directions.includes('long')
    ? '做多'
    : preferences.directions.includes('short')
      ? '做空'
      : '未选择';
  const strategySummary = !preferences.strategies.length
    ? '空策略（不做指标过滤）'
    : preferences.strategies.length === 1
      ? preferences.strategies[0] === 'supertrend'
        ? '超级趋势'
        : '智慧资金结构'
      : `超级趋势 + 智慧资金结构（${preferences.strategy_mode === 'all' ? '全部通过' : '任一通过'}）`;
  const exampleScore = Math.min(96, preferences.min_activity + 6);
  const examplePattern = directionExample === '做空' ? '阴阴' : '阳阳';
  const examplePosition =
    directionExample === '做空'
      ? '震荡区间上沿 · 前高压力区'
      : '震荡区间下沿 · 前低支撑区';
  const exampleAdvice =
    directionExample === '做空'
      ? '压力附近可观察做空；若向上突破站稳则取消'
      : '支撑附近可观察做多；若向下跌破站稳则取消';
  const authorizedCount =
    snapshot.authorized_count ??
    snapshot.access_control?.authorized_users ??
    (subscriberPaired ? 1 : 0);
  const pendingCount =
    snapshot.pending_count ?? snapshot.access_control?.pending_requests ?? 0;
  const preview = [
    `【PULSE ${preferences.signal_scope === 'qualified' ? '完整强信号' : '强活跃异常'} · 预览示例】`,
    '品种：BTCUSDT',
    `周期：${periodExample}`,
    `方向：${directionExample}`,
    `K线形态：${examplePattern}`,
    `活跃度：${exampleScore} / 100（门槛 ≥${preferences.min_activity}）`,
    `指标确认：${strategySummary}`,
    `位置判断：${examplePosition}`,
    `操作提示：${exampleAdvice}`,
    '完整质量门槛：已通过（预览示例）',
    '今日出现：第 3 次（同周期 2 · 同方向 2；北京时间隔日清零）',
    '信号时间：2026-09-02 10:25:00 UTC+8',
    '说明：仅提醒，不会自动开仓；请结合风险自行判断。',
  ].join('\n');

  const pairingExpiryValue = pairingExpiryTimestamp(pairing.pairing_expires_at);
  const pairingExpiry = pairingExpiryValue
    ? new Date(pairingExpiryValue).toLocaleTimeString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        hour: '2-digit',
        minute: '2-digit',
      })
    : '';

  return (
    <section className="panel telegram-settings-panel">
      <div className="panel-heading telegram-settings-heading">
        <div>
          <h2>
            <Send size={17} />
            Telegram 推送设置
          </h2>
          <p>先检查收到哪些提醒，再安全绑定自己的聊天。</p>
        </div>
        <span
          aria-live="polite"
          className={
            'telegram-connection-badge ' +
            (readyToSend
              ? 'is-connected'
              : botConnected || subscriberPaired
                ? 'is-bot-ready'
                : '')
          }
        >
          <i />
          {!online
            ? '状态未知'
            : readyToSend
              ? '已安全绑定'
              : subscriberPaired
                ? '聊天已绑定 · 机器人已断开'
                : botConnected
                  ? '机器人已连接 · 等待绑定'
                  : '未连接'}
        </span>
      </div>

      <div className="telegram-settings-body">
        <div className="telegram-settings-columns">
          <div className="telegram-settings-column">
            <section className="telegram-config-block">
              <div className="telegram-block-title">
                <span>1</span>
                <div>
                  <b>连接 Telegram</b>
                  <p>聊天不会手动填写，使用一次性链接防止绑定到陌生账号。</p>
                </div>
              </div>

              <label
                className="telegram-token-field"
                htmlFor="telegram-bot-token"
              >
                <span>
                  Bot Token
                  {tokenManagedByEnvironment ? (
                    <small>由启动环境变量管理</small>
                  ) : readyToSend ? (
                    <small>已连接 · 更换前请先断开</small>
                  ) : (
                    snapshot.token_configured && <small>已安全保存</small>
                  )}
                </span>
                <Input
                  id="telegram-bot-token"
                  type="password"
                  autoComplete="new-password"
                  spellCheck={false}
                  disabled={tokenInputLocked}
                  aria-describedby="telegram-token-help"
                  value={botToken}
                  placeholder={
                    tokenManagedByEnvironment
                      ? '由启动环境变量提供；请在进程外修改'
                      : readyToSend
                        ? '机器人已连接；如需更换请先断开'
                        : snapshot.token_configured
                          ? '已保存；留空表示继续使用现有 Token'
                          : '粘贴 BotFather 提供的完整 Token'
                  }
                  onChange={(event) => {
                    setBotToken(event.target.value);
                    setFeedback({
                      tone: 'neutral',
                      text: 'Token 只写不回显；当前没有发送消息。',
                    });
                  }}
                />
                <p id="telegram-token-help">
                  {tokenManagedByEnvironment
                    ? '当前 Token 由启动环境变量提供，本页不能替换或清除。'
                    : readyToSend
                      ? '当前机器人正在使用此 Token。要更换机器人，请先点击下方“断开机器人”。'
                      : '保存后页面和接口都不会返回原文，也不会写入行情数据库。'}
                </p>
              </label>

              {subscriberPaired ? (
                <div className="telegram-bound-chat">
                  <CheckCircle2 />
                  <div>
                    <b>
                      {snapshot.subscriber?.chat_label || 'Telegram 聊天已绑定'}
                    </b>
                    <span>
                      {snapshot.subscriber?.masked_chat_id || '聊天编号已隐藏'}
                      {snapshot.bot?.username
                        ? ` · @${snapshot.bot.username}`
                        : ''}
                    </span>
                  </div>
                </div>
              ) : pairing.pairing_url ? (
                <div className="telegram-pairing-box">
                  <div>
                    <b>安全链接已准备好</b>
                    <span>
                      {pairingExpiry
                        ? `请在 ${pairingExpiry} 前完成绑定`
                        : '请在 10 分钟内完成绑定'}
                    </span>
                  </div>
                  <a
                    href={pairing.pairing_url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    打开 Telegram
                    <ExternalLink size={14} />
                  </a>
                  <ol>
                    <li>点击上面的按钮打开机器人。</li>
                    <li>在 Telegram 中点击 Start。</li>
                    <li>回到本页，绑定状态会自动更新。</li>
                  </ol>
                </div>
              ) : (
                <div className="telegram-pairing-empty">
                  <ShieldCheck />
                  <p>填写 Token 后生成一次性链接，再去 Telegram 点击 Start。</p>
                </div>
              )}

              <div className="telegram-private-access">
                <ShieldCheck />
                <div>
                  <b>私有白名单</b>
                  <p>
                    首位安全绑定者是管理员。陌生用户只能申请，须由管理员在 Telegram
                    点击通过；群组、超级群组和频道无法使用。
                  </p>
                  <dl>
                    <div>
                      <dt>已授权</dt>
                      <dd>{authorizedCount} 人</dd>
                    </div>
                    <div>
                      <dt>待审核</dt>
                      <dd className={pendingCount > 0 ? 'has-pending' : ''}>
                        {pendingCount} 人
                      </dd>
                    </div>
                  </dl>
                  {subscriberPaired && (
                    <div className="telegram-private-menu-actions">
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => void sendAdminMenu()}
                        disabled={!readyToSend || adminMenuBusy || saving}
                        aria-describedby="telegram-admin-menu-status"
                      >
                        {adminMenuBusy ? (
                          <RefreshCw className="spin" />
                        ) : (
                          <Send />
                        )}
                        {adminMenuBusy
                          ? '正在发送菜单…'
                          : '发送或刷新管理员菜单'}
                      </Button>
                      <output
                        id="telegram-admin-menu-status"
                        aria-live="polite"
                        className={
                          'telegram-private-menu-status ' +
                          (adminMenuFeedback?.tone || 'neutral')
                        }
                      >
                        {!readyToSend
                          ? '机器人恢复连接后可发送。'
                          : adminMenuFeedback?.text ||
                            (pendingCount > 0
                              ? `有 ${pendingCount} 个待审核申请，发送后可直接处理。`
                              : '发送后可查看授权用户并管理白名单。')}
                      </output>
                    </div>
                  )}
                  {subscriberPaired && (
                    <small>
                      管理员命令：/users 查看授权 · /pending 查看待审 · /add ID
                      授权 · /remove ID 移除
                    </small>
                  )}
                </div>
              </div>

              <div className="telegram-connection-actions">
                {!readyToSend && (
                  <Button onClick={createPairingLink} disabled={saving}>
                    {saving ? <RefreshCw className="spin" /> : <ShieldCheck />}
                    {subscriberPaired ? '重新连接机器人' : '保存并生成安全链接'}
                  </Button>
                )}
                <Button
                  variant="outline"
                  onClick={async () => {
                    const next = await loadSettings();
                    if (next && !next.subscriber?.paired)
                      setFeedback({
                        tone: 'neutral',
                        text: next.connected
                          ? '机器人可用，但聊天尚未绑定。本次没有发送测试消息。'
                          : '机器人尚未连接。本次只检查状态，没有发送测试消息。',
                      });
                  }}
                  disabled={!api || !online || loading}
                >
                  {loading ? <RefreshCw className="spin" /> : <Send />}
                  检查连接状态
                </Button>
                {readyToSend && (
                  <Button
                    variant="outline"
                    onClick={() => void postTelegramAction('test')}
                    disabled={saving}
                  >
                    <Send />
                    发送测试文字
                  </Button>
                )}
              </div>
              <p className="telegram-no-send-note">
                <AlertTriangle size={13} />
                只有主动点击“发送测试文字”或“发送或刷新管理员菜单”才会发送；状态检查不会发消息。
              </p>
              {(subscriberPaired || botConnected) && (
                <div className="telegram-manage-actions">
                  {subscriberPaired && (
                    <button
                      type="button"
                      onClick={() => confirmTelegramAction('unpair')}
                      disabled={saving}
                      aria-label="清空 Telegram 管理员绑定与白名单"
                    >
                      清空绑定与白名单
                    </button>
                  )}
                  {botConnected && (
                    <button
                      type="button"
                      onClick={() => confirmTelegramAction('disconnect')}
                      disabled={saving}
                      aria-label="断开 Telegram 机器人"
                    >
                      断开机器人
                    </button>
                  )}
                </div>
              )}
            </section>

            <section className="telegram-config-block">
              <div className="telegram-block-title">
                <span>2</span>
                <div>
                  <b>选择要收到的信号</b>
                  <p>同一个信号即使满足多个条件，也只推送一次。</p>
                </div>
              </div>

              <fieldset className="telegram-choice-group">
                <legend>信号口径</legend>
                <div className="telegram-choice-grid two">
                  {[
                    [
                      'activity',
                      '按活跃度提醒',
                      '推荐 · 达到活跃度后再看所选指标',
                    ],
                    [
                      'qualified',
                      '完整强信号',
                      '严格 · 活跃度 + 完整质量门槛 + 所选指标全部满足',
                    ],
                  ].map(([id, label, help]) => {
                    const selected = preferences.signal_scope === id;
                    return (
                      <button
                        type="button"
                        className={
                          'telegram-scope-card ' +
                          (selected ? 'is-selected' : '')
                        }
                        aria-pressed={selected}
                        onClick={() =>
                          setPreferences((current) => ({
                            ...current,
                            signal_scope: id as TelegramSignalScope,
                          }))
                        }
                        key={id}
                      >
                        <span>{selected ? <CheckCircle2 /> : <i />}</span>
                        <span>
                          <b>{label}</b>
                          <small>{help}</small>
                        </span>
                      </button>
                    );
                  })}
                </div>
                {preferences.signal_scope === 'qualified' && (
                  <p className="telegram-strict-warning">
                    严格口径不会自动降低条件；活跃度、流动性、成本、空间和所选指标有一项未通过，就不会推送。
                  </p>
                )}
              </fieldset>

              <fieldset className="telegram-choice-group">
                <legend>周期</legend>
                <div className="telegram-choice-grid two">
                  {[
                    ['5m', '5 分钟', '更及时，信号较多'],
                    ['15m', '15 分钟', '更平稳，噪声较少'],
                  ].map(([id, label, help]) => {
                    const value = id as TelegramTimeframe;
                    const checked = preferences.timeframes.includes(value);
                    return (
                      <label
                        htmlFor={`telegram-timeframe-${id}`}
                        className={
                          'telegram-choice-card ' +
                          (checked ? 'is-selected' : '')
                        }
                        key={id}
                      >
                        <Checkbox
                          id={`telegram-timeframe-${id}`}
                          checked={checked}
                          onCheckedChange={(next) =>
                            setListValue('timeframes', value, next)
                          }
                        />
                        <span>
                          <b>{label}</b>
                          <small>{help}</small>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              <fieldset className="telegram-choice-group">
                <legend>方向</legend>
                <div className="telegram-choice-grid two">
                  {[
                    ['long', '做多', '向上信号'],
                    ['short', '做空', '向下信号'],
                  ].map(([id, label, help]) => {
                    const value = id as TelegramDirection;
                    const checked = preferences.directions.includes(value);
                    return (
                      <label
                        htmlFor={`telegram-direction-${id}`}
                        className={
                          'telegram-choice-card ' +
                          (checked ? 'is-selected' : '')
                        }
                        key={id}
                      >
                        <Checkbox
                          id={`telegram-direction-${id}`}
                          checked={checked}
                          onCheckedChange={(next) =>
                            setListValue('directions', value, next)
                          }
                        />
                        <span>
                          <b>{label}</b>
                          <small>{help}</small>
                        </span>
                      </label>
                    );
                  })}
                </div>
              </fieldset>

              <fieldset className="telegram-choice-group">
                <legend>最低活跃度</legend>
                <div className="telegram-thresholds">
                  {[
                    [65, '较多'],
                    [80, '推荐'],
                    [90, '极少'],
                  ].map(([threshold, label]) => (
                    <button
                      type="button"
                      className={
                        preferences.min_activity === threshold
                          ? 'is-selected'
                          : ''
                      }
                      aria-pressed={preferences.min_activity === threshold}
                      onClick={() =>
                        setPreferences((current) => ({
                          ...current,
                          min_activity: threshold as TelegramActivityThreshold,
                        }))
                      }
                      key={threshold}
                    >
                      <b>≥{threshold}</b>
                      <small>{label}</small>
                    </button>
                  ))}
                </div>
              </fieldset>

              <fieldset className="telegram-choice-group">
                <legend>指标筛选</legend>
                <p className="telegram-choice-help">
                  两个都不选就是“空策略”，只用两根 K 线、活跃度与基础保护门槛。
                </p>
                <div className="telegram-choice-grid">
                  {STRATEGIES.map((strategy) => {
                    const checked = preferences.strategies.includes(
                      strategy.id,
                    );
                    return (
                      <label
                        htmlFor={`telegram-strategy-${strategy.id}`}
                        className={
                          'telegram-choice-card ' +
                          (checked ? 'is-selected' : '')
                        }
                        key={strategy.id}
                      >
                        <Checkbox
                          id={`telegram-strategy-${strategy.id}`}
                          checked={checked}
                          onCheckedChange={(next) =>
                            setListValue('strategies', strategy.id, next)
                          }
                        />
                        <span>
                          <b>{strategy.name}</b>
                          <small>{strategy.description}</small>
                        </span>
                      </label>
                    );
                  })}
                </div>
                {!preferences.strategies.length && (
                  <div className="telegram-empty-strategy">
                    当前：空策略 · 不做指标过滤
                  </div>
                )}
                {preferences.strategies.length === 2 && (
                  <div
                    className="telegram-mode-switch"
                    aria-label="指标组合方式"
                  >
                    <button
                      type="button"
                      aria-pressed={preferences.strategy_mode === 'any'}
                      className={
                        preferences.strategy_mode === 'any' ? 'active' : ''
                      }
                      onClick={() =>
                        setPreferences((current) => ({
                          ...current,
                          strategy_mode: 'any',
                        }))
                      }
                    >
                      任一通过
                      <small>信号较多</small>
                    </button>
                    <button
                      type="button"
                      aria-pressed={preferences.strategy_mode === 'all'}
                      className={
                        preferences.strategy_mode === 'all' ? 'active' : ''
                      }
                      onClick={() =>
                        setPreferences((current) => ({
                          ...current,
                          strategy_mode: 'all',
                        }))
                      }
                    >
                      全部通过
                      <small>更严格</small>
                    </button>
                  </div>
                )}
              </fieldset>

              <div className="telegram-switch-row">
                <div>
                  <Bell size={16} />
                  <span>
                    <b>新合约上架提醒</b>
                    <small>
                      {subscriberPaired
                        ? '只提醒绑定后的新上架，不补发旧记录'
                        : '绑定后默认提醒未来新上架，不补发历史'}
                    </small>
                  </span>
                </div>
                <Switch
                  checked={preferences.listing_enabled}
                  disabled={!subscriberPaired}
                  onCheckedChange={(next) =>
                    setPreferences((current) => ({
                      ...current,
                      listing_enabled: next,
                    }))
                  }
                  aria-label="新合约上架提醒"
                />
              </div>
              <div className="telegram-switch-row">
                <div>
                  <Send size={16} />
                  <span>
                    <b>强信号推送总开关</b>
                    <small>绑定完成后再开启即可</small>
                  </span>
                </div>
                <Switch
                  checked={preferences.signals_enabled}
                  disabled={!subscriberPaired}
                  onCheckedChange={(next) =>
                    setPreferences((current) => ({
                      ...current,
                      signals_enabled: next,
                    }))
                  }
                  aria-label="强信号推送总开关"
                />
              </div>

              <div className="telegram-fixed-rules">
                <ShieldCheck size={16} />
                <p>
                  固定保护：先检查支撑、压力、订单块和区间位置 ·
                  压力区不把做多候选当入场 · 支撑区不把做空候选当入场 ·
                  只处理新收盘信号 · 不补发历史记录 · 同一信号只发一次
                </p>
              </div>
            </section>
          </div>

          <aside className="telegram-preview-column">
            <div className="telegram-preview-heading">
              <div>
                <span>3</span>
                <div>
                  <b>纯文字消息预览</b>
                  <p>示例随左侧选项变化，不代表真实行情。</p>
                </div>
              </div>
              <Button variant="outline" size="sm" onClick={copyPreview}>
                <Copy />
                复制
              </Button>
            </div>
            <pre>{preview}</pre>

            <div className="telegram-selection-summary">
              <h3>当前规则预览</h3>
              <div
                className={
                  'telegram-delivery-state ' +
                  (readyToSend && preferences.signals_enabled
                    ? 'is-active'
                    : 'is-off')
                }
              >
                {readyToSend && preferences.signals_enabled ? (
                  <CheckCircle2 />
                ) : (
                  <AlertTriangle />
                )}
                <div>
                  <b>
                    {readyToSend && preferences.signals_enabled
                      ? '强信号推送已开启'
                      : '目前不会收到强信号'}
                  </b>
                  <span>
                    {!subscriberPaired
                      ? '尚未绑定聊天；未来新上架也不会发送。'
                      : !botConnected
                        ? '机器人已断开，恢复连接前不会发送。'
                        : !preferences.signals_enabled
                          ? preferences.listing_enabled
                            ? '强信号开关关闭；仍会提醒绑定后的未来新上架。'
                            : '强信号与新上架提醒都已关闭。'
                          : '将按下方规则推送新产生的信号，不补发历史。'}
                  </span>
                </div>
              </div>
              <dl>
                <div>
                  <dt>口径</dt>
                  <dd>
                    {preferences.signal_scope === 'qualified'
                      ? '完整强信号'
                      : '强活跃异常'}
                  </dd>
                </div>
                <div>
                  <dt>周期</dt>
                  <dd>
                    {preferences.timeframes.length
                      ? preferences.timeframes.join(' + ')
                      : '未选择'}
                  </dd>
                </div>
                <div>
                  <dt>方向</dt>
                  <dd>
                    {preferences.directions.length
                      ? preferences.directions
                          .map((value) => (value === 'long' ? '做多' : '做空'))
                          .join(' + ')
                      : '未选择'}
                  </dd>
                </div>
                <div>
                  <dt>门槛</dt>
                  <dd>活跃度 ≥{preferences.min_activity}</dd>
                </div>
                <div>
                  <dt>指标</dt>
                  <dd>{strategySummary}</dd>
                </div>
                <div>
                  <dt>新合约</dt>
                  <dd>{preferences.listing_enabled ? '提醒' : '不提醒'}</dd>
                </div>
                <div>
                  <dt>信号开关</dt>
                  <dd>{preferences.signals_enabled ? '开启' : '关闭'}</dd>
                </div>
              </dl>
            </div>

            <output
              aria-live="polite"
              className={'telegram-feedback ' + feedback.tone}
            >
              {feedback.tone === 'success' ? (
                <CheckCircle2 />
              ) : feedback.tone === 'error' ? (
                <AlertTriangle />
              ) : (
                <ShieldCheck />
              )}
              <span>{feedback.text}</span>
            </output>

            <Button
              className="telegram-save-preferences"
              variant="outline"
              onClick={savePreferences}
              disabled={saving}
            >
              {saving ? <RefreshCw className="spin" /> : <CheckCircle2 />}
              保存推送规则
            </Button>
          </aside>
        </div>
      </div>
    </section>
  );
}
