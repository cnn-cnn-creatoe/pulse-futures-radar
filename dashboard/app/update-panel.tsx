'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CheckCircle2,
  CloudDownload,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

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

const CURRENT_VERSION = 'v1.0.2';
const UPDATE_POLL_MS = 30 * 60 * 1000;

async function responseMessage(response: Response, fallback: string) {
  try {
    const body = (await response.json()) as { detail?: unknown; message?: unknown };
    if (typeof body.detail === 'string') return body.detail;
    if (typeof body.message === 'string') return body.message;
  } catch {}
  return fallback;
}

export function UpdateControl({ api, online }: Props) {
  const [status, setStatus] = useState<UpdateStatus>({
    current_version: CURRENT_VERSION,
    latest_version: null,
    update_available: false,
    can_update: false,
    message: '点击后检查私密 GitHub 仓库中的稳定版本。',
  });
  const [open, setOpen] = useState(false);
  const [checking, setChecking] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState('');
  const mounted = useRef(true);
  const promptedVersion = useRef('');
  const reloadPoll = useRef<number | null>(null);

  const checkUpdate = useCallback(
    async (force = false) => {
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
        const response = await fetch(
          api + '/api/update/status' + (force ? '?force=true' : ''),
          {
            cache: 'no-store',
            signal: AbortSignal.timeout(15000),
          },
        );
        if (!response.ok)
          throw new Error(await responseMessage(response, '版本检查失败。'));
        const value = (await response.json()) as UpdateStatus;
        if (!mounted.current) return;
        setStatus(value);
        if (
          value.update_available &&
          value.can_update &&
          value.latest_version &&
          promptedVersion.current !== value.latest_version
        ) {
          promptedVersion.current = value.latest_version;
          setOpen(true);
        }
      } catch (reason) {
        if (mounted.current)
          setError(reason instanceof Error ? reason.message : '版本检查失败。');
      } finally {
        if (mounted.current) setChecking(false);
      }
    },
    [api, online],
  );

  useEffect(() => {
    mounted.current = true;
    const initial = window.setTimeout(() => void checkUpdate(), 0);
    const timer = window.setInterval(() => void checkUpdate(), UPDATE_POLL_MS);
    return () => {
      mounted.current = false;
      window.clearTimeout(initial);
      window.clearInterval(timer);
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
  const hasUpdate = status.update_available && status.can_update;

  return (
    <>
      <Button
        variant={hasUpdate ? 'default' : 'outline'}
        className={'update-check-button ' + (hasUpdate ? 'has-update' : '')}
        onClick={() => {
          setOpen(true);
          void checkUpdate(true);
        }}
      >
        {checking ? (
          <RefreshCw className="spin" />
        ) : hasUpdate ? (
          <CloudDownload />
        ) : (
          <RefreshCw />
        )}
        {checking ? '正在检查…' : hasUpdate ? `更新 ${newest}` : '查看更新'}
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="update-dialog">
          <DialogHeader>
            <span className={'update-dialog-icon ' + (hasUpdate ? 'has-update' : '')}>
              {hasUpdate ? <CloudDownload /> : <ShieldCheck />}
            </span>
            <DialogTitle>{hasUpdate ? '发现新版本' : '应用更新'}</DialogTitle>
            <DialogDescription>{error || status.message}</DialogDescription>
          </DialogHeader>
          <div className="update-version-row">
            <span>当前版本</span>
            <b>{status.current_version || CURRENT_VERSION}</b>
            <span>最新版本</span>
            <b>{status.latest_version || '检查后显示'}</b>
          </div>
          <p className="update-preserve-note">
            更新只替换程序文件，本机历史行情、回测结果、Telegram 密钥和用户设置都会保留。
          </p>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)} disabled={updating}>
              稍后
            </Button>
            {hasUpdate ? (
              <Button onClick={() => void applyUpdate()} disabled={updating}>
                {updating ? <RefreshCw className="spin" /> : <CloudDownload />}
                {updating ? '正在更新并重启…' : '下载并重启更新'}
              </Button>
            ) : (
              <Button onClick={() => void checkUpdate(true)} disabled={checking || updating || !api || !online}>
                {checking ? <RefreshCw className="spin" /> : <CheckCircle2 />}
                {checking ? '正在检查…' : '重新检查'}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
