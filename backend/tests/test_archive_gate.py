import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
from monitor.archive_gate import ArchiveGate


class ArchiveGateTests(unittest.TestCase):
    def test_cooldown_survives_recreation_and_prevents_request(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'cooldown.json'
            gate = ArchiveGate(path)
            with patch('monitor.archive_gate.time.time',return_value=1000):
                gate.reject(429,'120')
                with self.assertRaisesRegex(ValueError,'冷却'):
                    with ArchiveGate(path).request():
                        self.fail('request must not run')
            with patch('monitor.archive_gate.time.time',return_value=1121):
                with ArchiveGate(path).request():
                    pass

    def test_ban_default_and_shared_pacing(self):
        with tempfile.TemporaryDirectory() as directory:
            gate=ArchiveGate(Path(directory)/'cooldown.json')
            with patch('monitor.archive_gate.time.monotonic',return_value=10), patch('monitor.archive_gate.time.sleep') as sleep:
                with gate.request(): pass
                with gate.request(): pass
                self.assertEqual(sleep.call_args.args,(1.0,))
            gate.reject(418)
            with self.assertRaisesRegex(ValueError,'冷却'):
                with gate.request(): self.fail()
