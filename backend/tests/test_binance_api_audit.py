"""Static guardrails for Binance REST/stream architecture."""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
COLLECTOR = ROOT / "monitor" / "collector.py"


class BinanceApiAuditTests(unittest.TestCase):
    def test_runtime_fapi_calls_are_exactly_allowlisted(self):
        source = COLLECTOR.read_text(encoding="utf-8")
        paths = set(re.findall(r'_request\(\s*["\'](/fapi/[^"\']+)["\']', source))
        self.assertEqual(paths, {
            "/fapi/v1/time",
            "/fapi/v1/exchangeInfo",
            "/fapi/v1/klines",
        })
        self.assertNotIn("/fapi/v1/openInterest", source)
        self.assertNotIn("/fapi/v1/ticker/bookTicker", source)

    def test_no_other_runtime_module_calls_binance_fapi(self):
        offenders = []
        for path in (ROOT / "monitor").glob("*.py"):
            if path == COLLECTOR:
                continue
            if "/fapi/" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_websocket_stream_counts_are_bounded_by_design(self):
        source = COLLECTOR.read_text(encoding="utf-8")
        self.assertIn("KLINE_SYMBOLS_PER_SHARD = 40", source)
        self.assertIn("if len(symbols) > 1024", source)
        self.assertIn('streams = "!miniTicker@arr/!contractInfo"', source)
        self.assertIn('"?streams=!bookTicker"', source)


if __name__ == "__main__":
    unittest.main()
