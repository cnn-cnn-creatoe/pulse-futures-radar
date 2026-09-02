'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CheckCircle2,
  CloudDownload,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';

type UpdateStatus = {
  current_version: string;
  latest_version: string | null;
  update_available: boolean;
  can_update: boolean;
  message: string;
  checked_at?: number | null;
  release_url?: string | null;
};

type Props = {
  api: string | null;
  online: boolean;
};

const CURRENT_VERSION = 'v1.0.1';

async function responseMessage(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown; message?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (typeof body.message === 'string') return body.message;
  } catch {}
  return fallback;
}

export function UpdatePanel({ api, online }: Props) {
  const [status, setStatus] = useState<UpdateStatus>({
    current_version: CURRENT_VERSION,
    latest_version: null,
    update_available: false,
    can_update: false,
    message: '正在读取版本信息…',
  });
  const [checking, setChecking] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  const reloadPoll = useRef<number | null>(null);

  const checkUpdate = useCallback(async () => {
    if (!api || !online) {
      setStatus((previous) => ({
        ...previous,
        can_update: false,
        message: api
          ? '本机服务恢复后可以检查更新。'
          : '在线预览不执行本机程序更新。',
      }));
      return;
    }
    setChecking(true);
    setError('');
    try {
      const response = await fetch(api + '/api/update/status', {
        cache: 'no-store',
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok)
        throw new Error(await responseMessage(response, '版本检查失败。'));
      const value = (await response.json()) as UpdateStatus;
      if (mounted.current) setStatus(value);
    } catch (reason) {
      if (mounted.current)
        setError(reason instanceof Error ? reason.message : '版本检查失败。');
    } finally {
      if (mounted.current) setChecking(false);
    }
  }, [api, online]);

  useEffect(() => {
    mounted.current = true;
    void checkUpdate();
    return () => {
      mounted.current = false;
      if (reloadPoll.current !== null) window.clearInterval(reloadPoll.current);
    };
  }, [checkUpdate]);

  const applyUpdate = async () => {
    if (!api || !status.update_available || !status.can_update) return;
    setUpdating(true);
    setError('');
    try {
      const response = await fetch(api + '/api/update/apply', {
        method: 'POST',
        headers: { 'X-Pulse-Client': 'dashboard' },
        signal: AbortSignal.timeout(15000),
      });
      if (!response.ok)
        throw new Error(await responseMessage(response, '启动更新失败。'));
      const body = (await response.json()) as { message?: string };
      setStatus((previous) => ({
        ...previous,
        message:
          body.message ||
          '正在下载并更新；看板会短暂离线，完成后自动重新打开。',
      }));

      const oldVersion = status.current_version;
      const startedAt = Date.now();
      if (reloadPoll.current !== null) window.clearInterval(reloadPoll.current);
      reloadPoll.current = window.setInterval(async () => {
        try {
          const check = await fetch(api + '/api/update/status', {
            cache: 'no-store',
            signal: AbortSignal.timeout(4000),
          });
          if (!check.ok) return;
          const value = (await check.json()) as UpdateStatus;
          if (
            value.current_version !== oldVersion ||
            Date.now() - startedAt > 120000
          ) {
            if (reloadPoll.current !== null)
              window.clearInterval(reloadPoll.current);
            reloadPoll.current = null;
            window.location.reload();
          }
        } catch {}
      }, 3500);
    } catch (reason) {
      setUpdating(false);
      setError(reason instanceof Error ? reason.message : '启动更新失败。');
    }
  };

  const newest = status.latest_version || status.current_version;
  const badge = status.update_available
    ? `发现 ${newest}`
    : `当前 ${status.current_version || CURRENT_VERSION}`;

  return (
    <section
      className={'panel update-panel ' + (status.update_available ? 'has-update' : '')}
    >
      <div className="update-panel-copy">
        <span className="update-panel-icon" aria-hidden="true">
          {status.update_available ? <CloudDownload /> : <ShieldCheck />}
        </span>
        <div>
          <div className="update-panel-title">
            <h2>应用版本与更新</h2>
            <span>{badge}</span>
          </div>
          <p>
            {updating
              ? '正在从私有 GitHub 仓库下载更新并安排重启，请保持本页打开。'
              : error || status.message}
          </p>
          <small>
            更新只替换程序文件，本机历史行情、回测结果、Telegram 密钥和用户设置都会保留。
          </small>
        </div>
      </div>
      <div className="update-panel-actions">
        {status.update_available && status.can_update ? (
          <Button onClick={() => void applyUpdate()} disabled={updating}>
            {updating ? <RefreshCw className="spin" /> : <CloudDownload />}
            {updating ? '正在更新并重启…' : '下载并重启更新'}
          </Button>
        ) : (
          <Button
            variant="outline"
            onClick={() => void checkUpdate()}
            disabled={!api || !online || checking || updating}
          >
            {checking ? <RefreshCw className="spin" /> : <CheckCircle2 />}
            {checking ? '正在检查…' : '检查更新'}
          </Button>
        )}
      </div>
    </section>
  );
}
