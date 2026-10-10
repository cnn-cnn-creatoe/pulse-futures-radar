"""WaveTrend-only library, causal thresholds, old-setting migration and route contracts."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from monitor.community_indicators import CATALOG, states
from monitor.custom_indicators import CustomIndicatorRegistry
from monitor.daily_journal import day_start
from monitor.delivery_profiles import normalize_profiles, profiles
from monitor.engine import Candle
from monitor.indicators import indicator_catalog, IndicatorConfig, indicator_context
from monitor.live_rule_signals import rule_catalog, current_events
from monitor.policy import Settings, migrate_saved_settings
from monitor.quant_research import CATALOG as QUANT, signal_rows
from monitor.research import Research
from monitor.rule_library import passes_scores, RULE_ID
from monitor.source_browser import source_for
from monitor.signal_scores import historical_scores
from monitor.script_inventory import inventory
from monitor.store import Store
from monitor.telegram import migrate_preferences, normalize_preferences, signal_matches, menu_keyboard
from monitor.workbench import Workbench, WorkbenchRequest, install_workbench_routes, signal_stream


def bars(count=300):
    import math
    start=day_start('2026-09-01')
    return [Candle('BTCUSDT','5m',start+i*300000,start+(i+1)*300000-1,
        100+math.sin(i/10)*8,101+math.sin(i/10)*8,99+math.sin(i/10)*8,
        100+math.sin(i/10)*8,10000,5000,100,100) for i in range(count)]


class UnifiedLibraryTests(unittest.TestCase):
    def test_all_catalogs_and_source_inventory_agree(self):
        for catalog in (CATALOG,indicator_catalog()):
            self.assertEqual([r['id'] for r in catalog],[RULE_ID,'nan_strategy1_oversold'])
        self.assertEqual([r['id'] for r in rule_catalog('indicator')],[RULE_ID,'nan_strategy1_oversold','smc_equity_structure'])
        self.assertEqual(QUANT,[])
        self.assertEqual(rule_catalog('quant'),[])
        self.assertEqual(len(inventory()['items']),2)
        self.assertEqual(inventory()['items'][0]['kind'],'indicator')
        self.assertIn('study',source_for(RULE_ID)['code'])
        root=Path(__file__).resolve().parents[1]/'sources/tradingview'
        self.assertEqual(sorted(p.name for p in root.rglob('*.pine')),['nan-strategy1.pine','wavetrend.pine'])
        for retired in ('supertrend_flip','wt_cross','wt_direction','ema_cross','quant:supertrend_reversal','../settings.json'):
            with self.subTest(retired=retired),self.assertRaises(ValueError):source_for(retired)

    def test_request_rejects_retired_rules_bad_scores_and_unselected_settings(self):
        base=dict(start='2026-09-01',end='2026-09-02')
        self.assertEqual(WorkbenchRequest(**base).indicators,[RULE_ID])
        for update in (dict(engine='quant',strategy='supertrend_reversal'),dict(indicators=['wt_cross']),
                       dict(indicators=['ema_cross']),dict(indicators=[]),
                       dict(rule_settings={'old':{}}),dict(rule_settings={RULE_ID:{'min_activity':-1}}),
                       dict(rule_settings={RULE_ID:{'min_actionability':101}}),
                       dict(rule_settings={RULE_ID:{'min_activity':float('nan')}})):
            with self.subTest(update=update),self.assertRaises(ValueError):WorkbenchRequest(**base,**update)

    def test_formula_has_events_and_preserves_prefixes(self):
        original=bars(400);whole=states(original,[RULE_ID])
        self.assertGreater(sum(r.get(RULE_ID) in ('up','down') for r in whole),5)
        for count in (71,110,180,300):
            self.assertEqual(states(original[:count],[RULE_ID]),whole[:count])
        self.assertTrue(all(not r for r in whole[:70]))
        self.assertEqual(states(original,['wt_cross']),[{} for _ in original])

    def test_flat_prices_never_generate_crosses(self):
        flat=[replace(c,open=100,high=100,low=100,close=100) for c in bars()]
        self.assertFalse(any(r for r in states(flat,[RULE_ID])))

    def test_gaps_unclosed_bars_and_retired_direct_streams(self):
        data=bars(300);opts=WorkbenchRequest(start='2026-09-01',end='2026-09-03').model_dump()
        baseline=list(signal_stream(data,Settings(),[],opts))
        self.assertTrue(baseline)
        gap=data[:120]+data[150:]
        actual=list(signal_stream(gap,Settings(),[],opts))
        self.assertFalse(any(data[150].close_time<=r['close_time']<data[220].close_time for r in actual))
        prefix=data[:200];last=replace(data[200],closed=False)
        self.assertEqual(list(signal_stream(prefix+[last],Settings(),[],opts)),list(signal_stream(prefix,Settings(),[],opts)))
        with self.assertRaises(ValueError):list(signal_stream(data,Settings(),[],{**opts,'indicators':['ema_cross']}))

    def test_score_gate_requires_both_and_fails_closed(self):
        threshold=dict(min_activity=65,min_actionability=80)
        self.assertTrue(passes_scores(dict(activity_score=65,actionability={'score':80}),threshold))
        for scores in ({},{'activity_score':100},{'actionability':{'score':100}},
                       dict(activity_score=64,actionability={'score':100}),dict(activity_score=100,actionability={'score':79}),
                       dict(activity_score=float('nan'),actionability={'score':100})):
            with self.subTest(scores=scores):self.assertFalse(passes_scores(scores,threshold))
        self.assertTrue(passes_scores({},dict(min_activity=0,min_actionability=0)))

    def test_saved_policy_migration_does_not_relax_write_validation(self):
        old=Settings().model_dump();old['indicators']['enabled_indicators']=['supertrend','smc']
        original=deepcopy(old)
        self.assertEqual(Settings(**migrate_saved_settings(old)).indicators.enabled_indicators,[])
        self.assertEqual(old,original)
        with self.assertRaises(ValueError):Settings(**old)
        self.assertEqual(Settings().indicators.enabled_indicators,[RULE_ID])

    def test_old_subscriptions_never_expand_to_unfiltered_notifications(self):
        for old in (dict(event_kind='quant',rules=['supertrend_reversal'],signals_enabled=True),
                    dict(strategies=['supertrend'],signals_enabled=True),
                    dict(event_kind='indicator',rules=['ema_cross'],signals_enabled=True),
                    dict(rule_settings=[dict(kind='quant',rule_id='old')],signals_enabled=True)):
            source=deepcopy(old);p=normalize_preferences(migrate_preferences(old))
            self.assertFalse(p['signals_enabled']);self.assertEqual(old,source)
            self.assertFalse(profiles(p))

    def test_wavetrend_subscriber_settings_survive_migration(self):
        row=dict(kind='indicator',rule_id=RULE_ID,timeframes=['5m','4h'],symbols=['BTCUSDT'],patterns=['阴阳'],min_activity=65,min_actionability=80)
        p=normalize_preferences(dict(rule_settings=[row],signals_enabled=True))
        self.assertEqual(normalize_preferences(migrate_preferences(p)),p)
        self.assertEqual(normalize_profiles([row]),[row])

    def test_telegram_uses_same_thresholds_and_no_retired_menu_choices(self):
        row=dict(kind='indicator',rule_id=RULE_ID,timeframes=['5m'],symbols=[],patterns=[],min_activity=65,min_actionability=80)
        p=normalize_preferences(dict(rule_settings=[row],signals_enabled=True))
        signal=dict(source='live',symbol='BTCUSDT',timeframe='5m',rule_kind='indicator',rule_id=RULE_ID,
                    entry_direction='long',pattern='阳阳',activity_score=65,actionability={'score':80})
        self.assertTrue(signal_matches(signal,p)[0])
        for update in (dict(activity_score=64),dict(actionability={'score':79}),dict(actionability={}),dict(rule_id='ema_cross')):
            self.assertFalse(signal_matches({**signal,**update},p)[0])
        buttons=[b for row in menu_keyboard(p)['inline_keyboard'] for b in row]
        self.assertFalse(any('quant' in b['callback_data'] for b in buttons))
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for b in buttons))


class ResearchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();root=Path(self.tmp.name)
        self.store=Store(root/'test.db');self.store.save_candles(bars())
        self.service=Workbench(self.store,Research(root/'history'),CustomIndicatorRegistry(root/'custom.json'),root/'runs')
        self.settings=Settings()
        self.opts=WorkbenchRequest(start='2026-09-01',end='2026-09-02',purpose='signals').model_dump()
        self.app=FastAPI();install_workbench_routes(self.app,self.service,lambda _:None,lambda:self.settings)
    def tearDown(self):self.store.close();self.tmp.cleanup()
    def job(self,**options):
        return dict(id='a'*32,state='running',created_at=1,processed=0,logs=[],custom_indicator_snapshot=[],indicator_names=[CATALOG[0]['name']],request={**self.opts,**options},policy_snapshot=self.settings.model_dump())
    def test_imported_history_scores_match_live_storage_without_future_bars(self):
        data=bars(1000);self.store.save_candles(data);at=data[900].close_time;config=self.settings.model_dump_json()
        expected=historical_scores(self.store,'BTCUSDT','5m',at,'long',config,1)
        empty=Store(Path(self.tmp.name)/'empty.db')
        try:
            future=[replace(c,close=c.close*100,high=c.high*100) for c in data[901:]]
            self.service.research.register(data[:901]+future,{'source':'test'})
            actual=historical_scores(empty,'BTCUSDT','5m',at,'long',config,1,self.service.research)
            self.assertIsNotNone(actual['activity_score'])
            self.assertEqual(actual,expected)
        finally:empty.close()
    def test_research_filters_before_metrics_pagination_and_chart(self):
        full=self.job();self.service.work(full,self.settings,[])
        all_rows=full['items'];self.assertGreater(len(all_rows),3)
        threshold={RULE_ID:dict(min_activity=65,min_actionability=80)}
        def score(store,symbol,tf,at,*args):return dict(activity_score=65,actionability={'score':80 if at==all_rows[1]['signal_time'] else 70})
        with patch('monitor.workbench.historical_scores',side_effect=score):
            filtered=self.job(rule_settings=threshold);self.service.work(filtered,self.settings,[])
            self.service.jobs[filtered['id']]=filtered
            self.assertEqual(filtered['metrics']['selected'],1)
            self.assertEqual(filtered['items'][0]['id'],all_rows[1]['id'])
            with TestClient(self.app) as client:
                result=client.get('/api/workbench/runs/'+filtered['id']).json()
                self.assertEqual(result['total_items'],1);self.assertEqual(len(result['items']),1)
                chart=client.get('/api/workbench/runs/'+filtered['id']+'/chart?row=0&refresh=true')
                self.assertEqual(chart.status_code,200,chart.text)
                self.assertEqual(len([m for m in chart.json()['markers'] if m['action'] in ('buy','sell')]),1)
    def test_no_positive_threshold_does_not_require_missing_scores(self):
        job=self.job(rule_settings={RULE_ID:dict(min_activity=0,min_actionability=0)})
        with patch('monitor.workbench.historical_scores',side_effect=AssertionError('unnecessary scoring')):
            self.service.work(job,self.settings,[])
        self.assertGreater(job['metrics']['selected'],0)
    def test_positive_threshold_excludes_missing_scores(self):
        job=self.job(rule_settings={RULE_ID:dict(min_activity=1,min_actionability=0)})
        with patch('monitor.workbench.historical_scores',return_value={}):self.service.work(job,self.settings,[])
        self.assertEqual(job['metrics']['selected'],0)
    def test_routes_default_to_wave_and_reject_retired_source(self):
        with TestClient(self.app) as client:
            catalog=client.get('/api/workbench/catalog').json()
            self.assertEqual([i['id'] for i in catalog['indicators']],[RULE_ID,'nan_strategy1_oversold']);self.assertEqual(catalog['strategies'],[])
            chart=client.get('/api/workbench/market-chart',params=dict(symbol='BTCUSDT',signal_time=bars()[180].close_time))
            self.assertEqual(chart.status_code,200,chart.text)
            self.assertTrue(chart.json()['markers'])
            self.assertEqual(client.get('/api/workbench/source?rule_id=ema_cross').status_code,404)
            self.assertEqual(client.get('/api/workbench/source?rule_id='+RULE_ID).status_code,200)
    def test_live_and_research_confirm_the_same_close(self):
        opts={**self.opts,'end':'2026-09-03'}
        events=list(signal_stream(bars(),self.settings,[],opts));at=events[-1]['close_time']
        prefix=[c for c in bars() if c.close_time<=at]
        with patch('monitor.live_rule_signals.historical_scores',return_value={}):
            live=current_events(self.store,prefix,self.settings,{('indicator',RULE_ID)})
        self.assertEqual(len(live),1);self.assertEqual(live[0]['close_time'],at)
        self.assertEqual(live[0]['entry_direction'],events[-1]['direction'])
    def test_retired_saved_run_is_not_reexecuted(self):
        job=self.job(engine='quant',strategy='old');self.service.jobs[job['id']]=job
        with TestClient(self.app) as client:
            self.assertEqual(client.get('/api/workbench/runs/'+job['id']).status_code,410)
            self.assertEqual(client.get('/api/workbench/runs').json()['items'],[])

if __name__=='__main__':unittest.main()
