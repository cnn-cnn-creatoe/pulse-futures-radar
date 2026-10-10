"""Separate, process-wide pacing for the official historical archive origin."""
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import threading
import time


class ArchiveGate:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.next_request = 0.0

    @contextmanager
    def request(self):
        # Serialize archive transfers as well as their checksum requests.
        with self.lock:
            if self.path.exists():
                try:
                    until = float(json.loads(self.path.read_text())['until'])
                    if not math.isfinite(until):
                        raise ValueError('invalid cooldown')
                except (OSError, ValueError, KeyError, TypeError):
                    raise ValueError('历史下载保护记录无法读取，请检查本地文件')
                if until > time.time():
                    raise ValueError(f'历史下载正在限流冷却，约 {int(until-time.time())+1} 秒后可重试')
            time.sleep(max(0, self.next_request-time.monotonic()))
            try:
                yield
            finally:
                self.next_request = time.monotonic()+1.0

    def reject(self, status, retry_after=None):
        if status not in (418, 429, 403, 451):
            return
        delay = 259200 if status == 418 else 900 if status in (403, 451) else 60
        if retry_after:
            try:
                seconds = float(retry_after)
                if math.isfinite(seconds):
                    delay = max(delay, seconds)
            except (ValueError, TypeError):
                try:
                    delay = max(delay, parsedate_to_datetime(retry_after).timestamp()-time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps({'until':time.time()+delay,'status':status}))
        temporary.replace(self.path)


archive_gate = ArchiveGate(Path(os.getenv('PULSE_DATA_DIR', str(Path(__file__).resolve().parents[1]/'data')))/'archive-cooldown.json')
