"""Update checks are fully mocked: no GitHub access or updater is executed."""
from __future__ import annotations

from pathlib import Path
import json
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from monitor.update import UpdateError, UpdateService


class UpdateServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / "scripts").mkdir()
        (self.root / "scripts" / "update.ps1").write_text("# test fixture", encoding="utf-8")
        self.write_config()

    def tearDown(self):
        self.folder.cleanup()

    def write_config(self, **changes):
        config = {
            "version": "v1.0.0",
            "repository": "cnn-cnn-creatoe/pulse-futures-radar",
            "release_channel": "stable",
            **changes,
        }
        (self.root / "release.json").write_text(
            json.dumps(config), encoding="utf-8"
        )

    def test_private_release_check_is_bounded_cached_and_credential_free(self):
        service = UpdateService(self.root, cache_seconds=60, timeout_seconds=2.5)
        completed = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps({
                "tagName": "v1.2.0",
                # The response must not trust or expose this returned URL.
                "url": "https://attacker.example/release?token=secret",
            }),
            stderr="",
        )
        with (
            patch("monitor.update.shutil.which", return_value=r"C:\Program Files\GitHub CLI\gh.exe"),
            patch("monitor.update.subprocess.run", return_value=completed) as run,
        ):
            first = service.status()
            second = service.status()

        self.assertEqual(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(first["current_version"], "v1.0.0")
        self.assertEqual(first["latest_version"], "v1.2.0")
        self.assertTrue(first["update_available"])
        self.assertTrue(first["can_update"])
        self.assertEqual(
            first["release_url"],
            "https://github.com/cnn-cnn-creatoe/pulse-futures-radar/releases/tag/v1.2.0",
        )
        self.assertNotIn("secret", json.dumps(first))
        call = run.call_args
        self.assertEqual(call.args[0][1:4], ["release", "view", "--repo"])
        self.assertEqual(call.args[0][4], "cnn-cnn-creatoe/pulse-futures-radar")
        self.assertEqual(call.kwargs["timeout"], 2.5)
        self.assertFalse(call.kwargs["shell"])
        self.assertIs(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["env"]["GH_PROMPT_DISABLED"], "1")

    def test_cli_failure_returns_fixed_chinese_reason_without_stderr(self):
        service = UpdateService(self.root)
        completed = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="token=ghp_do_not_echo private-url"
        )
        with (
            patch("monitor.update.shutil.which", return_value="gh"),
            patch("monitor.update.subprocess.run", return_value=completed),
        ):
            result = service.status()
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertFalse(result["can_update"])
        self.assertIsNone(result["latest_version"])
        self.assertIn("请确认 gh 已登录", result["message"])
        self.assertNotIn("ghp_do_not_echo", serialized)
        self.assertNotIn("private-url", serialized)

    def test_invalid_repository_never_reaches_a_process(self):
        for repository in (
            "owner/repo;whoami",
            "owner/../../repo",
            "https://github.com/owner/repo",
            "owner/repo/extra",
        ):
            with self.subTest(repository=repository):
                self.write_config(repository=repository)
                service = UpdateService(self.root)
                with patch("monitor.update.subprocess.run") as run:
                    result = service.status()
                self.assertFalse(result["can_update"])
                self.assertIn("字段不合法", result["message"])
                run.assert_not_called()

    def test_schedule_launches_only_fixed_script_without_shell_or_client_arguments(self):
        service = UpdateService(self.root)
        latest = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"tagName":"v1.1.0","url":"ignored"}', stderr=""
        )

        def executable(name):
            return "gh.exe" if name == "gh" else "powershell.exe"

        with (
            patch("monitor.update.shutil.which", side_effect=executable),
            patch("monitor.update.subprocess.run", return_value=latest),
            patch("monitor.update.subprocess.Popen") as popen,
        ):
            popen.return_value.poll.return_value = None
            result = service.schedule_update()
            duplicate = service.schedule_update()

        self.assertTrue(result["scheduled"])
        self.assertTrue(duplicate["scheduled"])
        popen.assert_called_once()
        command = popen.call_args.args[0]
        self.assertEqual(command[-4:], [
            "-File", str(self.root / "scripts" / "update.ps1"), "-Mode", "Apply",
        ])
        self.assertNotIn("cnn-cnn-creatoe/pulse-futures-radar", command)
        self.assertFalse(popen.call_args.kwargs["shell"])
        self.assertEqual(popen.call_args.kwargs["cwd"], str(self.root))

    def test_completed_failed_updater_can_be_retried_without_backend_restart(self):
        service = UpdateService(self.root)
        latest = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"tagName":"v1.1.0","url":"ignored"}', stderr=""
        )
        with (
            patch("monitor.update.shutil.which", side_effect=lambda name: name),
            patch("monitor.update.subprocess.run", return_value=latest),
            patch("monitor.update.subprocess.Popen") as popen,
        ):
            popen.return_value.poll.return_value = 1
            service.schedule_update()
            service.schedule_update()
        self.assertEqual(popen.call_count, 2)

    def test_schedule_refuses_when_release_is_not_newer(self):
        service = UpdateService(self.root)
        latest = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"tagName":"v1.0.0","url":"ignored"}', stderr=""
        )
        with (
            patch("monitor.update.shutil.which", return_value="gh.exe"),
            patch("monitor.update.subprocess.run", return_value=latest),
            patch("monitor.update.subprocess.Popen") as popen,
        ):
            with self.assertRaisesRegex(UpdateError, "没有可安装"):
                service.schedule_update()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
