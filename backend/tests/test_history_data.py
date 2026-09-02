"""Strict historical data import and checksum/security behavior."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit
import zipfile

from monitor.history_data import download_binance_month, parse_candles_csv, parse_candles_json, validate_series


CSV = "open_time,open,high,low,close,volume,close_time,quote_volume,count,taker_buy_volume,taker_buy_quote_volume,ignore\n0,100,102,99,101,10,299999,1005,12,6,603,0\n300000,101,103,100,102,12,599999,1218,15,7,710,0\n"
CHINESE_CONTRACTS = ("币安人生USDT", "我踏马来了USDT", "牛来USDT", "龙虾USDT")
DELIVERY_CONTRACTS = ("BTCUSDT_260925", "ETHUSDT_261225", "中文USDT_260925")


class HistoryImportTests(unittest.TestCase):
    def test_official_columns_header_and_milliseconds(self):
        candles = parse_candles_csv(CSV, "BTCUSDT", "5m")
        self.assertEqual(len(candles), 2)
        self.assertEqual(candles[1].close_time, 599999)
        self.assertEqual(candles[0].quote_volume, 1005)
        self.assertEqual(candles[0].taker_buy_quote, 603)
        self.assertEqual(validate_series(candles)["gaps"], 0)

    def test_reject_unclosed_duplicate_invalid_ohlc_and_microseconds(self):
        with self.assertRaisesRegex(ValueError, "unclosed"):
            parse_candles_csv(CSV, "BTCUSDT", "5m", now_ms=400000)
        for broken in (CSV.replace("300000,101", "0,101").replace("599999", "299999"),
                       CSV.replace("100,102,99,101", "100,90,99,101"),
                       CSV.replace("299999", "299999999")):
            with self.subTest(broken=broken):
                with self.assertRaises(ValueError):
                    parse_candles_csv(broken, "BTCUSDT", "5m")

    def test_json_identity_unknown_fields_and_boolean_prices(self):
        data = [c.to_dict() for c in parse_candles_csv(CSV, "BTCUSDT", "5m")]
        self.assertEqual(len(parse_candles_json(json.dumps(data), "BTCUSDT", "5m")), 2)
        for field, value in (("symbol", "ETHUSDT"), ("path", "secret"), ("open", True)):
            with self.subTest(field=field):
                modified = [{**data[0], field: value}]
                with self.assertRaises(ValueError):
                    parse_candles_json(json.dumps(modified), "BTCUSDT", "5m")

    def test_gaps_are_reported_not_filled(self):
        candles = parse_candles_csv(CSV.replace("300000,101", "600000,101").replace("599999", "899999"), "BTCUSDT", "5m")
        result = validate_series(candles)
        self.assertEqual(result["gaps"], 1)
        self.assertEqual(result["missing_bars"], 1)
        self.assertEqual(result["bars"], 2)

    def test_chinese_csv_and_json_import_preserve_identity_through_backtest_validation(self):
        from monitor.backtest import run_backtest
        for symbol in CHINESE_CONTRACTS:
            with self.subTest(symbol=symbol):
                candles = parse_candles_csv(CSV, symbol, "5m")
                payload = json.dumps([c.to_dict() for c in candles], ensure_ascii=False)
                imported = parse_candles_json(payload, symbol, "5m")
                self.assertEqual([c.symbol for c in imported], [symbol, symbol])
                self.assertEqual(validate_series(imported)["symbol"], symbol)
                # This tiny synthetic sample only checks Unicode compatibility;
                # insufficient warmup stays insufficient, never a performance claim.
                result = run_backtest(imported)
                self.assertEqual(result["status"], "insufficient_data")
                self.assertEqual(result["dataset"]["symbol"], symbol)

    def test_unicode_compatibility_does_not_admit_special_characters_or_length_abuse(self):
        bad = ("=币安人生USDT", "+龙虾USDT", "牛来USDT/../../x", "我踏马来了USDT?url=x",
               "币安人生USDT\r\nX:evil", "龙虾USDT\u200b", "牛来USDT🔥", "牛来USDT;DROP TABLE candles",
               "牛", "币" * 41)
        for symbol in bad:
            with self.subTest(symbol=symbol):
                with self.assertRaises(ValueError):
                    parse_candles_csv(CSV, symbol, "5m")
                with patch("monitor.history_data._download") as download:
                    with self.assertRaises(ValueError):
                        download_binance_month(symbol, "5m", "2020-01", ".")
                    download.assert_not_called()

    def test_delivery_symbols_survive_csv_json_and_backtest_validation(self):
        from monitor.backtest import run_backtest
        for symbol in DELIVERY_CONTRACTS:
            with self.subTest(symbol=symbol):
                candles = parse_candles_csv(CSV, symbol, "5m")
                imported = parse_candles_json(json.dumps([c.to_dict() for c in candles]), symbol, "5m")
                self.assertEqual([c.symbol for c in imported], [symbol, symbol])
                self.assertEqual(validate_series(imported)["symbol"], symbol)
                self.assertEqual(run_backtest(imported)["status"], "insufficient_data")
        for symbol in ("BTCUSDT_260925/../../x", "BTCUSDT_260925%2fsecret", "=BTCUSDT_260925",
                       "BTCUSDT_260925;DROP TABLE candles", "BTCUSDT_260925\r\nX:x", "__"):
            with self.subTest(invalid=symbol), self.assertRaises(ValueError):
                parse_candles_csv(CSV, symbol, "5m")

    def _zip(self, member="BTCUSDT-5m-2020-01.csv", archive_name="BTCUSDT-5m-2020-01.zip"):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(member, CSV)
        data = buffer.getvalue()
        checksum = (hashlib.sha256(data).hexdigest() + "  " + archive_name + "\n").encode("utf-8")
        return data, checksum

    def test_chinese_archive_uses_encoded_official_url_and_utf8_checksum_filename(self):
        for symbol in CHINESE_CONTRACTS:
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as directory:
                name = f"{symbol}-5m-2020-01.zip"
                data, checksum = self._zip(name[:-4] + ".csv", name)
                with patch("monitor.history_data._download", side_effect=[checksum, data]) as download:
                    result = download_binance_month(symbol, "5m", "2020-01", directory)
                self.assertEqual(download.call_count, 2)
                urls = [call.args[0] for call in download.call_args_list]
                for url in urls:
                    self.assertTrue(url.isascii())
                    self.assertEqual(urlsplit(url).scheme, "https")
                    self.assertEqual(urlsplit(url).netloc, "data.binance.vision")
                    self.assertEqual(urlsplit(url).query, "")
                    self.assertEqual(urlsplit(url).fragment, "")
                self.assertEqual(unquote(urlsplit(urls[1]).path), f"/data/futures/um/monthly/klines/{symbol}/5m/{name}")
                self.assertEqual(urls[0], urls[1] + ".CHECKSUM")
                self.assertEqual(result["metadata"]["source_url"], urls[1])
                self.assertEqual(result["metadata"]["symbol"], symbol)
                self.assertTrue(result["metadata"]["checksum_verified"])
                self.assertEqual([c.symbol for c in result["candles"]], [symbol, symbol])
                self.assertEqual(Path(result["archive_path"]).name, name)
                self.assertEqual((Path(directory) / (name + ".CHECKSUM")).read_text("utf-8").strip(), checksum.decode("utf-8").strip())

    def test_verified_download_records_source_and_refuses_checksum_mismatch(self):
        data, checksum = self._zip()
        with tempfile.TemporaryDirectory() as directory:
            with patch("monitor.history_data._download", side_effect=[checksum, data]):
                result = download_binance_month("BTCUSDT", "5m", "2020-01", directory)
            self.assertTrue(result["metadata"]["checksum_verified"])
            self.assertFalse(result["metadata"]["live_feed"])
            self.assertTrue(Path(result["archive_path"]).exists())
            with patch("monitor.history_data._download", side_effect=[checksum]) as cached_download:
                cached = download_binance_month("BTCUSDT", "5m", "2020-01", directory)
            self.assertTrue(cached["metadata"]["cache_hit"])
            self.assertEqual(cached_download.call_count, 1)
            Path(result["archive_path"]).write_bytes(b"corrupt local cache")
            with patch("monitor.history_data._download", side_effect=[checksum, data + b"corrupted"]):
                with self.assertRaisesRegex(ValueError, "SHA256"):
                    download_binance_month("BTCUSDT", "5m", "2020-01", directory)

    def test_delivery_archive_uses_exact_official_um_identity(self):
        for symbol in DELIVERY_CONTRACTS:
            with self.subTest(symbol=symbol), tempfile.TemporaryDirectory() as directory:
                filename = f"{symbol}-5m-2020-01.zip"
                data, checksum = self._zip(filename[:-4] + ".csv", filename)
                with patch("monitor.history_data._download", side_effect=[checksum, data]) as download:
                    result = download_binance_month(symbol, "5m", "2020-01", directory)
                address = download.call_args_list[1].args[0]
                self.assertEqual(urlsplit(address).netloc, "data.binance.vision")
                self.assertEqual(unquote(urlsplit(address).path),
                                 f"/data/futures/um/monthly/klines/{symbol}/5m/{filename}")
                self.assertEqual(result["metadata"]["symbol"], symbol)
                self.assertTrue(result["metadata"]["checksum_verified"])
                self.assertEqual(Path(result["archive_path"]).name, filename)

    def test_archive_path_traversal_is_never_extracted(self):
        data, checksum = self._zip("../outside.csv")
        with tempfile.TemporaryDirectory() as directory:
            with patch("monitor.history_data._download", side_effect=[checksum, data]):
                with self.assertRaisesRegex(ValueError, "matching CSV"):
                    download_binance_month("BTCUSDT", "5m", "2020-01", directory)
            self.assertEqual(list(Path(directory).iterdir()), [])
        for symbol in ("../../secret", "BTCUSDT?x=1", "btcusdt"):
            with self.assertRaises(ValueError):
                download_binance_month(symbol, "5m", "2020-01", directory)


if __name__ == "__main__":
    unittest.main()
