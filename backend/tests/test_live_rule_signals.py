import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from test_workbench import bars
from monitor.store import Store
from monitor.policy import Settings
from monitor.live_rule_signals import current_events,rule_catalog
from monitor.workbench import signal_stream
from monitor.daily_journal import local_time
from monitor.quant_research import signal_rows
from monitor.telegram import normalize_preferences,signal_matches,menu_keyboard,TelegramState


class LiveRuleTests(unittest.TestCase):
    def test_all_rules_share_values_without_changing_individual_triggers(self):
        candles=bars(count=288);settings=Settings()
        options=dict(start=local_time(candles[0].open_time),end=local_time(candles[-1].close_time+1),
                     indicators=['wt_extreme_cross'],combination='all',pattern='none',engine='indicator')
        at=list(signal_stream(candles,settings,[],options))[-1]['close_time']
        candles=[c for c in candles if c.close_time<=at]
        choices={(kind,r['id']) for kind in ('indicator','quant') for r in rule_catalog(kind)}
        with patch('monitor.live_rule_signals.historical_scores',return_value={}):
            combined=current_events(None,candles,settings,choices)
            individual=[event for choice in choices for event in current_events(None,candles,settings,{choice})]
        self.assertTrue(combined)
        self.assertEqual(sorted(combined,key=lambda e:e['id']),sorted(individual,key=lambda e:e['id']))

    def test_shared_events_match_workbench_and_only_latest_close(self):
        candles=bars(count=288);settings=Settings()
        options=dict(start=local_time(candles[0].open_time),end=local_time(candles[-1].close_time),
                     indicators=['wt_extreme_cross'],combination='any',pattern='none',engine='indicator')
        events=list(signal_stream(candles,settings,[],options))
        at=events[-1]['close_time'];prefix=[c for c in candles if c.close_time<=at]
        with tempfile.TemporaryDirectory() as folder:
            store=Store(Path(folder)/'test.db');store.save_candles(prefix)
            try:
                actual=current_events(store,prefix,settings,{('indicator','wt_extreme_cross')})
            finally:store.close()
        expected=signal_rows(events)[-1]
        self.assertEqual({r['rule_kind'] for r in actual},{'indicator'})
        self.assertTrue(all(r['close_time']==at for r in actual))
        quant=next(r for r in actual if r['rule_kind']=='indicator')
        self.assertEqual(quant['signal_kind'],expected['signal_kind'])
        self.assertEqual(quant['action_basis'],expected['action_basis'])
        self.assertFalse(any(r['signal_kind'] in ('卖出平多','买回平空') for r in actual))

    def test_pure_filter_accepts_no_anomaly_and_respects_book_and_pattern(self):
        p=normalize_preferences(dict(event_kind='indicator',rules=['wt_extreme_cross'],signals_enabled=True,min_activity=0))
        event=dict(id='a',rule_kind='indicator',rule_id='wt_extreme_cross',source='live',timeframe='5m',
                   entry_direction='long',pattern='十字 / 其他',actionability={'score':80,'execution_status':'不通过'})
        self.assertTrue(signal_matches(event,p)[0])
        self.assertFalse(signal_matches(event,{**p,'require_book':True})[0])
        self.assertFalse(signal_matches(event,{**p,'patterns':['阳阳','阴阴','阳阴','阴阳']})[0])
        self.assertFalse(signal_matches(event,{**p,'event_kind':'anomaly','rules':[]})[0])
        self.assertFalse(signal_matches({**event,'delayed':True},p)[0])
        self.assertFalse(signal_matches(event,{**p,'min_activity':35})[0])

    def test_rules_are_allowlisted_and_bot_menu_has_reachable_pages(self):
        with self.assertRaises(ValueError):normalize_preferences({'event_kind':'quant','rules':['unreviewed']})
        for kind in ('indicator','quant'):
            p=normalize_preferences({'event_kind':kind})
            buttons=[b for page in range(len(rule_catalog(kind))) for row in menu_keyboard(p,page)['inline_keyboard'] for b in row]
            self.assertTrue(all(len(b['callback_data'].encode())<=64 for b in buttons))
            self.assertEqual({b['callback_data'][7:] for b in buttons if b['callback_data'].startswith('p:rule:')},
                             {r['id'] for r in rule_catalog(kind)})

    def test_personal_rule_changes_do_not_modify_other_subscriber(self):
        with tempfile.TemporaryDirectory() as folder:
            state=TelegramState(Path(folder)/'tg.db')
            try:
                code,_=state.issue_pairing();state.consume_pairing(code,1,1,'Owner',normalize_preferences({}))
                state.add_user(2,1,normalize_preferences({'event_kind':'indicator','rules':['wt_extreme_cross']}))
                before=state.subscriber(chat_id=2)['preferences']
                state.update_preferences(normalize_preferences({'event_kind':'indicator','rules':['wt_extreme_cross'],'min_activity':35}),chat_id=1)
                self.assertEqual(state.subscriber(chat_id=2)['preferences'],before)
            finally:state.close()


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedicated_executor_and_health(self):
        import asyncio,threading
        from monitor.live_rule_signals import LiveRuleSignals
        from types import SimpleNamespace
        worker=LiveRuleSignals(None,SimpleNamespace(enqueue=lambda *a:None),Settings,now=lambda:100000)
        worker.subscriptions=lambda *a:{('indicator','wt_extreme_cross')}
        try:
            name=await worker.compute(lambda:threading.current_thread().name)
            self.assertTrue(name.startswith('live-rules'))
            candle=SimpleNamespace(close_time=99999,timeframe='5m',symbol='BTCUSDT')
            with patch('monitor.live_rule_signals.current_events',return_value=[]):
                worker.enqueue([candle])
                await asyncio.wait_for(worker.queue.join(),2)
            self.assertEqual(worker.status()['processed'],1)
            self.assertEqual(worker.status()['dropped'],0)
            worker.enqueue([SimpleNamespace(close_time=0,timeframe='5m',symbol='BTCUSDT')])
            await asyncio.wait_for(worker.queue.join(),2)
            self.assertEqual(worker.status()['drop_reasons']['expired'],1)
            self.assertFalse(worker.status()['active'])
        finally:await worker.close()

    async def test_error_is_visible_and_worker_continues(self):
        import asyncio
        from monitor.live_rule_signals import LiveRuleSignals
        from types import SimpleNamespace
        worker=LiveRuleSignals(None,SimpleNamespace(enqueue=lambda *a:None),Settings,now=lambda:100000)
        worker.subscriptions=lambda *a:{('indicator','wt_extreme_cross')}
        candle=SimpleNamespace(close_time=99999,timeframe='5m',symbol='BTCUSDT')
        try:
            with patch('monitor.live_rule_signals.current_events',side_effect=ValueError('test failure')):
                worker.enqueue([candle]);await asyncio.wait_for(worker.queue.join(),2)
            self.assertEqual(worker.status()['errors'],1)
            self.assertIn('test failure',worker.status()['last_error'])
            with patch('monitor.live_rule_signals.current_events',return_value=[]):
                worker.enqueue([candle]);await asyncio.wait_for(worker.queue.join(),2)
            self.assertEqual(worker.status()['processed'],1)
        finally:await worker.close()
