from unittest.mock import patch as fixed_clock
import csv
import io
import time
import unittest
from test_api import ApiTests, HEADERS
from monitor.daily_journal import day_start


class DailyEndpointTests(unittest.TestCase):
    setUp = ApiTests.setUp
    tearDown = ApiTests.tearDown

    @fixed_clock("monitor.daily_journal.time.time", return_value=1788652800)
    def test_archive_worker_starts_and_daily_csv_is_available(self, _clock):
        self.assertIsNotNone(self.api.daily_archives.task)
        self.assertFalse(self.api.daily_archives.task.done())
        self.api.store.save_alert({'symbol': 'TESTUSDT', 'timeframe': '5m',
            'close_time': day_start('2026-09-05') + 299999, 'pattern': '阳阳', 'activity_score': 10})
        listing = self.client.get('/api/daily-archives').json()
        self.assertEqual(listing['items'][0]['records'], 1)
        data = self.client.get('/api/daily-records?date=2026-09-05&symbol=TESTUSDT').json()
        self.assertEqual(len(data['items']), 1)
        response = self.client.get('/api/daily-archives/2026-09-05.csv')
        self.assertEqual(response.status_code, 200)
        rows = list(csv.DictReader(io.StringIO(response.content.decode('utf-8-sig'))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['交易对'], 'TESTUSDT')
        self.assertEqual(self.client.get('/api/daily-records?date=bad').status_code, 400)

    def test_replay_uses_async_worker_and_requires_validated_custom_indicator(self):
        body = {'source': 'daily', 'date': '2026-09-05', 'indicators': ['not_saved']}
        self.assertEqual(self.client.post('/api/replays', json=body, headers=HEADERS).status_code, 400)
        body['indicators'] = []
        self.assertEqual(self.client.post('/api/replays', json=body).status_code, 403)
        response = self.client.post('/api/replays', json=body, headers=HEADERS)
        self.assertEqual(response.status_code, 200)
        ident = response.json()['id']
        for _ in range(100):
            job = self.client.get('/api/replays/' + ident).json()
            if job['state'] != 'running':
                break
            time.sleep(.01)
        self.assertEqual(job['state'], 'completed', job)
        self.assertEqual(job['metrics']['selected'], 0)
        self.assertEqual(self.client.get(f'/api/replays/{ident}/results.csv').status_code, 200)
        self.assertTrue((self.api.simple_replay.root / f'{ident}.json').exists())


if __name__ == '__main__':
    unittest.main()
