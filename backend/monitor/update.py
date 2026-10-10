"""Bounded, local-only application update discovery and scheduling.

The dashboard never supplies a repository, tag, executable, or filesystem path.
Those values come from the installed application's fixed files, and the GitHub
CLI supplies authentication without exposing credentials to this process.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from urllib.parse import quote
from urllib.request import Request, urlopen
import json
import os
import re
import shutil
import subprocess
import time
from typing import Callable


CURRENT_VERSION = "v3.0.6"
DEFAULT_GITHUB_REPOSITORY = "cnn-cnn-creatoe/pulse-futures-radar"
STATUS_CACHE_SECONDS = 60.0
GH_TIMEOUT_SECONDS = 12.0

_VERSION_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_REPOSITORY_RE = re.compile(
    r"^(?=.{3,200}$)([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?)/"
    r"([A-Za-z0-9](?:[A-Za-z0-9_.-]{0,98}[A-Za-z0-9])?)$"
)


class UpdateError(RuntimeError):
    """A fixed, user-safe update error which never includes command output."""


@dataclass(frozen=True)
class ReleaseConfig:
    version: str
    github_repository: str
    release_channel: str


def _version_parts(version: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


class UpdateService:
    """Check a private GitHub release and launch the fixed updater script."""

    def __init__(
        self,
        application_root: Path,
        *,
        cache_seconds: float = STATUS_CACHE_SECONDS,
        timeout_seconds: float = GH_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = application_root.resolve()
        self.config_path = self.root / "release.json"
        self.update_script = self.root / "scripts" / "update.ps1"
        self.cache_seconds = cache_seconds
        self.timeout_seconds = timeout_seconds
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = Lock()
        self._schedule_lock = Lock()
        self._cache: dict | None = None
        self._cached_at = 0.0
        self._update_process: subprocess.Popen | None = None

    def _base_status(self, current_version: str = CURRENT_VERSION) -> dict:
        return {
            "current_version": current_version,
            "latest_version": None,
            "update_available": False,
            "can_update": False,
            "message": "暂时无法检查更新。",
            "checked_at": int(self._wall_clock() * 1000),
        }

    def _load_config(self) -> ReleaseConfig:
        try:
            if not self.config_path.is_file() or self.config_path.stat().st_size > 65_536:
                raise UpdateError("本机版本配置缺失或大小异常，无法安全检查更新。")
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
        except UpdateError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise UpdateError("本机版本配置无法读取，无法安全检查更新。") from None
        if not isinstance(raw, dict):
            raise UpdateError("本机版本配置格式不正确，无法安全检查更新。")
        version = raw.get("version")
        # `repository` is the one canonical key shared by the Python API and
        # the bundled PowerShell updater.  Keep the older key readable so an
        # existing pre-release installation can still migrate safely.
        repository = raw.get(
            "repository",
            raw.get("github_repository", DEFAULT_GITHUB_REPOSITORY),
        )
        channel = raw.get("release_channel")
        repository_match = _REPOSITORY_RE.fullmatch(repository) if isinstance(repository, str) else None
        if (
            not isinstance(version, str)
            or _version_parts(version) is None
            or repository_match is None
            or any(part in {".", ".."} for part in repository.split("/"))
            or channel != "stable"
        ):
            raise UpdateError("本机版本配置字段不合法，已停止更新检查。")
        return ReleaseConfig(version, repository, channel)

    def _query_latest(self, config: ReleaseConfig) -> tuple[str, str]:
        request = Request(
            f"https://api.github.com/repos/{config.github_repository}/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "PULSE-Updater"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read(1048577)
            if len(raw) > 1048576:
                raise ValueError("oversized response")
            payload = json.loads(raw)
            if payload.get("draft") or payload.get("prerelease"):
                raise ValueError("not stable")
            latest = payload["tag_name"]
        except (OSError, ValueError, TypeError, KeyError):
            raise UpdateError("无法读取 GitHub 公开版本，请检查网络或稍后重试；无需登录。") from None
        if not isinstance(latest, str) or _version_parts(latest) is None:
            raise UpdateError("GitHub 最新 Release 的版本号不符合 v主版本.次版本.修订版 格式。")
        # Construct this from validated components instead of trusting a URL
        # contained in command output.
        release_url = (
            f"https://github.com/{config.github_repository}/releases/tag/"
            f"{quote(latest, safe='')}"
        )
        return latest, release_url

    def _fresh_status(self) -> dict:
        try:
            config = self._load_config()
        except UpdateError as error:
            result = self._base_status()
            result["message"] = str(error)
            return result

        result = self._base_status(config.version)
        try:
            latest, release_url = self._query_latest(config)
        except UpdateError as error:
            result["message"] = str(error)
            return result

        result["latest_version"] = latest
        result["release_url"] = release_url
        current_parts = _version_parts(config.version)
        latest_parts = _version_parts(latest)
        # Both values were validated above; these guards keep the comparison
        # total if validation is changed later.
        if current_parts is None or latest_parts is None:
            result["message"] = "版本号无法比较，已停止更新。"
            return result
        result["update_available"] = latest_parts > current_parts
        if not result["update_available"]:
            result["message"] = (
                "当前已是最新版本。"
                if latest_parts == current_parts
                else "当前版本不低于 GitHub 最新稳定版。"
            )
            return result
        if not self.update_script.is_file():
            result["message"] = f"发现新版本 {latest}，但本机缺少安全更新程序。"
            return result
        result["can_update"] = True
        result["message"] = f"发现新版本 {latest}，可以立即下载并更新。"
        return result

    def status(self, *, force: bool = False) -> dict:
        """Return a cached status; an uncached private-release check is bounded."""
        with self._lock:
            now = self._clock()
            if (
                not force
                and self._cache is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return dict(self._cache)
            result = self._fresh_status()
            self._cache = dict(result)
            self._cached_at = self._clock()
            return result

    def progress(self) -> dict:
        """Read local update stages without making a GitHub request."""
        path = self.root / ".runtime" / "update-status.json"
        try:
            if path.stat().st_size > 65536:
                raise ValueError("oversized")
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            allowed = {"checking", "downloading", "verifying", "extracting", "stopping", "installing", "restarting", "complete", "error"}
            if not isinstance(value, dict) or value.get("status") not in allowed:
                raise ValueError("invalid")
            return {key: value.get(key) for key in ("status", "message", "version", "updatedAt")}
        except (OSError, ValueError, TypeError):
            return {"status": "idle", "message": "尚未开始更新。"}

    def schedule_update(self) -> dict:
        """Detach the fixed updater without accepting any client arguments."""
        # Serialize double-clicks so at most one detached updater is launched.
        with self._schedule_lock:
            if self._update_process is not None:
                try:
                    still_running = self._update_process.poll() is None
                except OSError:
                    still_running = False
                if still_running:
                    return {"scheduled": True, "message": "更新程序已启动，请等待应用自动重启。"}
                # A failed updater may exit before it can restart this API.
                # Clear the completed handle so the user can safely retry.
                self._update_process = None

            status = self.status()
            if not status["update_available"]:
                raise UpdateError("当前没有可安装的新版本。")
            if not status["can_update"]:
                raise UpdateError(str(status["message"]))
            # Re-check the fixed path just before launch.  The request cannot
            # override this location or pass arguments to the script.
            script = self.update_script
            if script.parent != self.root / "scripts" or not script.is_file():
                raise UpdateError("本机缺少安全更新程序，无法开始更新。")
            powershell = shutil.which("powershell.exe") or shutil.which("powershell")
            if powershell is None:
                raise UpdateError("本机未找到 Windows PowerShell，无法开始更新。")

            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            try:
                runtime = self.root / ".runtime"
                runtime.mkdir(parents=True, exist_ok=True)
                (runtime / "update-status.json").write_text(json.dumps({
                    "status": "checking", "message": "正在启动更新检查…"
                }, ensure_ascii=False), encoding="utf-8")
                process = subprocess.Popen(
                    [
                        powershell,
                        "-NoLogo",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-File",
                        str(script),
                        "-Mode",
                        "Apply",
                    ],
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    shell=False,
                    creationflags=creation_flags,
                )
            except OSError:
                raise UpdateError("更新程序无法启动，本次没有更改已安装文件。") from None
            self._update_process = process
            return {"scheduled": True, "message": "更新已安排，下载完成后应用会自动重启。"}
