import unittest
from datetime import datetime,timezone
from monitor.periods import floor_bar,shift,ceil_bar,NATIVE
from monitor.engine import Candle
from monitor.history_data import validate_series
from monitor.workbench_data import missing_ranges
from monitor.workbench import WorkbenchRequest
from monitor.daily_journal import forward_outcome
from monitor.collector import MonitorService

def ts(s):return int(datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()*1000)
def candle(s,tf):
    t=ts(s)
    return Candle("BTCUSDT",tf,t,shift(t,tf)-1,100,105,95,102,1000,500,10,10)
class PeriodTests(unittest.TestCase):
    def test_calendar_month_and_week(self):
        self.assertEqual(shift(ts("2024-02-01"),"1M"),ts("2024-03-01"))
        self.assertEqual(shift(ts("2023-02-01"),"1M"),ts("2023-03-01"))
        self.assertEqual(floor_bar(ts("2026-09-06"),"1w"),ts("2026-08-31"))
        self.assertEqual(ceil_bar(ts("2026-09-06"),"1w"),ts("2026-09-07"))
    def test_monthly_validation_and_missing_february(self):
        rows=[candle("2024-01-01","1M"),candle("2024-03-01","1M")]
        self.assertEqual(validate_series(rows,ts("2025-01-01"))["missing_bars"],1)
        self.assertEqual(missing_ranges(rows,ts("2024-01-01"),ts("2024-03-01"),"1M"),[(ts("2024-02-01"),ts("2024-02-01"))])
    def test_live_validator_accepts_calendar_bars(self):
        for tf,start in [("1M","2024-02-01"),("1w","2026-08-31")]:
            self.assertTrue(MonitorService._valid_candle(candle(start,tf)))

    def test_monthly_forward_result(self):
        rows=[candle("2024-02-01","1M"),candle("2024-03-01","1M")]
        signal=dict(symbol="BTCUSDT",timeframe="1M",close_time=ts("2024-02-01")-1,price=100,direction="long")
        result=forward_outcome(signal,rows,dict(holding_bars=2,fee_bps=0,slippage_bps=0))
        self.assertEqual(result["status"],"已完成")
        self.assertEqual(result["exit_time"],ts("2024-04-01")-1)
    def test_requested_periods_and_seconds_guard(self):
        for tf in NATIVE:
            WorkbenchRequest(start="2026-09-01T00:00",end="2026-09-01T01:00",timeframes=[tf])
        for kw in [dict(scope="all"),dict(end="2026-09-03T00:00")]:
            with self.assertRaises(ValueError):WorkbenchRequest(**{**dict(start="2026-09-01T00:00",end="2026-09-01T01:00",timeframes=["1s"]),**kw})
    def test_second_bar_validation(self):
        self.assertEqual(validate_series([candle("2024-01-01","1s")],ts("2025-01-01"))["bars"],1)

class SecondRequestGuard(unittest.IsolatedAsyncioTestCase):
    async def test_seconds_never_repeat_rejected_rest_request(self):
        from unittest.mock import AsyncMock
        service=MonitorService.__new__(MonitorService)
        service._request=AsyncMock()
        with self.assertRaisesRegex(ValueError,"1 秒"):
            await service.fetch_research_page("BTCUSDT","1s",0,1000)
        service._request.assert_not_awaited()
