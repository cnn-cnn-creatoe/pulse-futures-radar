import unittest
from unittest.mock import AsyncMock,Mock
from monitor.extra_periods import ExtraPeriods
class ExtraPeriodTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_extra_collection_for_original_periods(self):
        extra=ExtraPeriods(Mock(),Mock(),Mock())
        extra.select(["5m","15m"])
        self.assertIsNone(extra.task)
    async def test_single_worker_reused_and_cancelled_on_deselect(self):
        import asyncio
        extra=ExtraPeriods(Mock(),Mock(),Mock())
        async def run(cancel=None):await asyncio.sleep(100)
        extra.run=run
        extra.select(["30m"]);first=extra.task;token=extra._cancel
        extra.select(["30m"])
        self.assertIs(first,extra.task)
        extra.select([])
        await asyncio.gather(first,return_exceptions=True)
        self.assertTrue(first.cancelled())
        self.assertTrue(token.is_set())
        self.assertEqual(extra.selected,[])
    async def test_reject_seconds_without_starting_worker(self):
        extra=ExtraPeriods(Mock(),Mock(),Mock())
        with self.assertRaises(ValueError):extra.select(["1s"])
        self.assertIsNone(extra.task)

    async def test_cancelled_scan_stops_before_analyzing_or_saving(self):
        import threading
        extra=ExtraPeriods(Mock(),Mock(),Mock())
        extra.store.get_candles.return_value=[Mock()]
        token=threading.Event();token.set()
        extra.scan("BTCUSDT","30m",1706745600000,1706747400000,{},token)
        extra.store.save_alert.assert_not_called()
        self.assertEqual(extra.cursors,{})
