from unittest.mock import patch as fixed_clock
"""Behavioral tests for pattern-free, causal multi-symbol research."""
import csv
from dataclasses import replace
import math
from pathlib import Path
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from monitor.custom_indicators import CustomIndicatorRegistry
from monitor.daily_journal import DailyArchives, day_start, local_time
from monitor.engine import Candle, analyze
from monitor.policy import Settings
from monitor.research import Research
from monitor.store import Store
from monitor.workbench import Workbench, WorkbenchRequest, signal_stream, segments, install_workbench_routes


def bars(symbol="BTCUSDT", count=180):
    start = day_start("2026-09-01")
    result = []
    for i in range(count):
        close = 100 + math.sin(i / 10) * 8
        result.append(Candle(symbol,"5m",start+i*300000,start+(i+1)*300000-1,
            close,close+.3,close-.3,close,10000,5000,100,100))
    return result


class WorkbenchTests(unittest.TestCase):
    def test_selected_signal_scores_are_causal_and_validate_identity(self):
        data=bars(count=180);self.store.save_candles(data)
        app=FastAPI();install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        params=dict(symbol='BTCUSDT',timeframe='5m',signal_time=data[100].close_time,direction='long')
        with TestClient(app) as client:
            response=client.get('/api/workbench/signal-score',params=params)
            self.assertEqual(response.status_code,200)
            result=response.json()
            self.assertEqual(result['signal_time'],data[100].close_time)
            self.assertIn('activity_score',result)
            self.assertEqual(client.get('/api/workbench/signal-score',params={**params,'direction':'bad'}).status_code,422)
            self.assertEqual(client.get('/api/workbench/signal-score',params={**params,'run_id':'missing'}).status_code,404)
            absent=client.get('/api/workbench/signal-score',params={**params,'signal_time':data[-1].close_time+300000}).json()
            self.assertIsNone(absent['activity_score'])


    def test_market_chart_is_local_closed_data_without_invented_signals(self):
        data=bars(count=288);self.store.save_candles(data+[replace(data[-1],open_time=data[-1].open_time+300000,close_time=data[-1].close_time+300000,closed=False)])
        app=FastAPI();install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            r=client.get('/api/workbench/market-chart',params={'symbol':'BTCUSDT','timeframe':'5m','signal_time':data[100].close_time})
            self.assertEqual(r.status_code,200);self.assertEqual(len(r.json()['candles']),288)
            self.assertEqual(r.json()['start'], day_start('2026-09-01') - 29*86400000)
            self.assertEqual(client.get('/api/workbench/market-chart',params={'symbol':'BTCUSDT','history_days':61}).status_code,422)
            self.assertTrue(r.json()['markers'])
            self.assertTrue(all(m['action'] in ('buy','sell','record') for m in r.json()['markers']))
            self.assertEqual(client.get('/api/workbench/market-chart',params={'symbol':'BTCUSDT','engine':'quant','rule':'supertrend_reversal'}).status_code,422)
            self.assertEqual(next(m for m in r.json()['markers'] if m['selected'])['time'],data[100].close_time)
            event=next(m for m in r.json()['markers'] if m['action']!='record')
            coincident=client.get('/api/workbench/market-chart',params={'symbol':'BTCUSDT','signal_time':event['time']}).json()['markers']
            self.assertEqual([m['action'] for m in coincident if m['selected']],[event['action']])
            self.assertTrue(any(m['action']==event['action'] and m['time']==event['time'] for m in coincident))
            self.assertEqual(client.get('/api/workbench/market-chart?symbol=BTCUSDT&rule=unreviewed').status_code,422)
            self.assertEqual(client.get('/api/workbench/market-chart?symbol=../secret').status_code,422)

    def test_market_candidates_match_archived_time_and_never_invent_trades(self):
        data=bars(count=288);self.store.save_candles(data)
        for index,direction in [(100,'up'),(287,'down')]:
            self.store.save_alert(dict(symbol='BTCUSDT',timeframe='5m',
                second_candle_close_time=data[index].close_time,direction=direction,pattern='阳阳'))
        self.store.save_alert(dict(symbol='ETHUSDT',timeframe='5m',close_time=data[101].close_time,direction='up'))
        self.store.save_alert(dict(symbol='BTCUSDT',timeframe='15m',close_time=data[102].close_time,direction='up'))
        app=FastAPI();install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            params=dict(symbol='BTCUSDT',timeframe='5m',engine='anomaly',rule='two_k',signal_time=data[100].close_time)
            response=client.get('/api/workbench/market-chart',params=params)
            self.assertEqual(response.status_code,200)
            marks=response.json()['markers']
            self.assertEqual([(m['time'],m['action']) for m in marks],[(data[100].close_time,'candidate_long'),(data[287].close_time,'candidate_short')])
            self.assertEqual([m['time'] for m in marks if m['selected']],[data[100].close_time])
            self.assertEqual([m['price'] for m in marks],[data[100].close,data[287].close])
            latest=client.get('/api/workbench/market-chart',params={k:v for k,v in params.items() if k!='signal_time'})
            self.assertFalse(any(m['action'] in ('buy','sell','close_long','close_short') for m in latest.json()['markers']))



    def test_display_scores_do_not_change_detection_or_use_future_data(self):
        from monitor.signal_scores import historical_scores
        from monitor.actionability import assess_inputs
        data=bars(count=1100);self.store.save_candles(data)
        self.assertIsNone(analyze(data[:1000]).get('signal'))
        self.assertIsNotNone(analyze(data[:1000],score_only=True).get('signal'))
        args=(self.store,'BTCUSDT','5m',data[999].close_time,'long',self.settings.model_dump_json())
        before=historical_scores(*args)
        self.store.save_candles([replace(c,open=c.open*3,high=c.high*3,low=c.low*3,close=c.close*3) for c in data[1000:]])
        historical_scores.cache_clear()
        self.assertEqual(before,historical_scores(*args))
        self.assertEqual(before['actionability']['execution_status'],'待验证')
        inputs=dict(status='ready',atr=1,closes=[100]*4,as_of=1)
        a=assess_inputs(dict(close_time=1,spread_bps=9,liquidity_percentile=.3),inputs,None,None)
        self.assertEqual(a['execution_status'],'不通过');self.assertEqual(len(a['execution_reasons']),2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root/"test.db")
        self.registry = CustomIndicatorRegistry(self.root/"custom.json")
        self.research = Research(self.root/"history")
        self.service = Workbench(self.store,self.research,self.registry,self.root/"runs")
        self.options = WorkbenchRequest(start="2026-09-01T00:00",end="2026-09-01T15:00").model_dump()
        self.settings = Settings()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def job(self, **kwargs):
        return dict(id="a"*32,request={**self.options,**kwargs},state="running",processed=0,logs=[],
            created_at=1,policy_snapshot=self.settings.model_dump(),custom_indicator_snapshot=[],indicator_names=["超级趋势翻转"])

    def test_no_pattern_scans_doji_without_anomaly_gate(self):
        candles = bars()
        self.assertIsNone(analyze(candles).get("signal"))
        free = list(signal_stream(candles,self.settings,[],self.options))
        self.assertGreater(len(free),3)
        self.assertEqual({r["pattern"] for r in free},{"十字 / 其他"})
        self.assertFalse(list(signal_stream(candles,self.settings,[],{**self.options,"pattern":"all"})))



    def test_vector_signals_do_not_use_future(self):
        original = bars()
        baseline = list(signal_stream(original,self.settings,[],{**self.options,"indicators":["wt_extreme_cross"],"combination":"any"}))
        changed = original[:100]+[replace(c,open=c.open*2,high=c.high*2,low=c.low*2,close=c.close*2) for c in original[100:]]
        again = list(signal_stream(changed,self.settings,[],{**self.options,"indicators":["wt_extreme_cross"],"combination":"any"}))
        cutoff = original[99].close_time
        self.assertEqual([r for r in baseline if r["close_time"]<=cutoff],[r for r in again if r["close_time"]<=cutoff])
        for row in baseline[:5]:
            prefix = [c for c in original if c.close_time<=row["close_time"]]
            self.assertIn(row,list(signal_stream(prefix,self.settings,[],{**self.options,"indicators":["wt_extreme_cross"],"combination":"any"})))


    def test_gaps_reset_indicator_warmup_and_forward_results(self):
        candles=bars()
        gap=candles[:80]+candles[100:]
        self.assertEqual(len(segments(gap)),2)
        rows=list(signal_stream(gap,self.settings,[],{**self.options,"indicators":["wt_extreme_cross"]}))
        self.assertFalse(any(candles[100].close_time<=r["close_time"]<candles[149].close_time for r in rows))

    def test_multi_symbol_time_bounds_missing_data_and_csv_parity(self):
        self.store.save_candles(bars()+bars("ETHUSDT"))
        job=self.job(scope="multiple",symbols=["BTCUSDT","ETHUSDT","MISSINGUSDT"],start="2026-09-01T05:00",end="2026-09-01T10:00")
        self.service.work(job,self.settings,[])
        self.assertEqual(job["state"],"completed")
        self.assertEqual({r["symbol"] for r in job["items"]},{"BTCUSDT","ETHUSDT"})
        self.assertEqual(job["missing_symbols"],["MISSINGUSDT"])
        for r in job["items"]:
            self.assertTrue("2026-09-01 05:00"<=local_time(r["signal_time"])<="2026-09-01 10:00:00")
            self.assertGreater(r["entry_time"],r["signal_time"])
        with (self.service.root/(job["id"]+".csv")).open(encoding="utf-8-sig",newline="") as f:
            exported=list(csv.DictReader(f))
        self.assertEqual(len(exported),len(job["items"]))
        self.assertIsNone(job["metrics"]["max_drawdown"])
        self.assertIsNone(job["metrics"]["account_return"])
        self.assertEqual(job["coverage"][0]["missing"],0)

    def test_single_all_and_downloaded_dataset_sources(self):
        self.store.save_candles(bars())
        self.research.register(bars("ETHUSDT"),"test")
        single=self.job()
        self.service.work(single,self.settings,[])
        self.assertEqual({r["symbol"] for r in single["items"]},{"BTCUSDT"})
        all_symbols=self.job(scope="all",symbols=[])
        self.service.work(all_symbols,self.settings,[])
        self.assertEqual({r["symbol"] for r in all_symbols["items"]},{"BTCUSDT","ETHUSDT"})

    def test_quant_and_invalid_ranges_are_rejected(self):
        for values in [dict(engine="quant"),dict(end=self.options["start"]),dict(scope="multiple"),dict(indicators=[]),dict(timeframes=["7m"])]:
            with self.assertRaises(ValueError): WorkbenchRequest(**{**self.options,**values})

    @fixed_clock("monitor.daily_journal.time.time", return_value=1788652800)
    def test_repeat_scan_deduplicates_daily_evidence_and_survives_restart(self, _clock):
        self.store.save_candles(bars())
        first=self.job()
        self.service.work(first,self.settings,[])
        count=len(first["items"])
        self.service.work(self.job(holding_bars=10),self.settings,[])
        archives=DailyArchives(self.store,self.root/"daily")
        self.assertEqual(archives.listing()["items"][0]["records"],count)
        path=archives.export("2026-09-01")
        with path.open(encoding="utf-8-sig",newline="") as f:
            rows=list(csv.DictReader(f))
        self.assertEqual(len(rows),count)
        self.assertTrue(all("重算" in r["记录来源"] for r in rows))
        self.assertEqual(self.store.alert_days(),[])  # Never generates live alerts.
        self.store.close()
        self.store=Store(self.root/"test.db")
        self.assertEqual(self.store.indicator_days()[0]["records"],count)

    def test_api_pagination_symbol_filter_and_exact_chart_row(self):
        self.store.save_candles(bars()+bars("ETHUSDT"))
        job=self.job(scope="all",symbols=[])
        self.service.work(job,self.settings,[])
        self.service.jobs[job["id"]]=job
        app=FastAPI()
        install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            result=client.get(f'/api/workbench/runs/{job["id"]}?symbol=ETHUSDT&offset=1&limit=2').json()
            self.assertEqual(len(result["items"]),2)
            self.assertEqual({r["symbol"] for r in result["items"]},{"ETHUSDT"})
            selected=result["items"][0]
            chart=client.get(f'/api/workbench/runs/{job["id"]}/chart?row={selected["row_index"]}').json()
            self.assertEqual(chart["row"]["id"],selected["id"])
            self.assertTrue(chart["candles"])
            self.assertLess(selected["signal_time"],selected["entry_time"])
            self.assertEqual(client.get(f'/api/workbench/runs/{job["id"]}/chart?row=9999').status_code,404)

    def test_stop_interrupts_history_wait_and_active_task_has_identity(self):
        import asyncio
        from unittest.mock import patch
        async def run():
            job=self.job(purpose="backtest")
            job['as_of']=9999999999999
            self.service.jobs[job['id']]=job
            entered=asyncio.Event()
            async def blocked(*args):
                entered.set()
                await asyncio.sleep(3600)
            with patch('monitor.workbench.HistoricalPreparation.run',side_effect=blocked):
                task=asyncio.create_task(self.service.execute(job,self.settings,[]))
                await entered.wait()
                app=FastAPI()
                install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
                with TestClient(app) as client:
                    current=client.get('/api/workbench/active-tasks').json()['items'][0]
                    self.assertEqual(current['id'],job['id'])
                    self.assertEqual(current['request']['symbols'],['BTCUSDT'])
                    client.post('/api/workbench/runs/'+job['id']+'/stop')
                await asyncio.wait_for(task,2)
                self.assertEqual(job['state'],'cancelled')
        asyncio.run(run())

    def test_live_refresh_does_not_download_history(self):
        import asyncio
        from unittest.mock import patch
        self.store.save_candles(bars())
        job=self.job(purpose='signals')
        job['as_of']=9999999999999
        self.service.fetch_page=lambda *args:None
        with patch('monitor.workbench.HistoricalPreparation.run',side_effect=AssertionError('Live must not download')):
            asyncio.run(self.service.execute(job,self.settings,[]))
        self.assertEqual(job['state'],'completed')

    def test_selected_signal_history_download_is_opt_in_and_preserves_scope(self):
        import asyncio
        from unittest.mock import patch, AsyncMock
        for scope in ('single','all'):
            job=self.job(purpose='signals',auto_download=True,scope=scope,timeframes=['1h','4h'])
            job['as_of']=9999999999999
            with patch('monitor.workbench.HistoricalPreparation.run',new_callable=AsyncMock,return_value=True) as preparation,patch.object(self.service,'work') as work:
                asyncio.run(self.service.execute(job,self.settings,[]))
            preparation.assert_awaited_once()
            prepared_job=preparation.call_args.args[0]
            self.assertEqual(prepared_job['request']['scope'],scope)
            self.assertEqual(prepared_job['request']['timeframes'],['1h','4h'])
            self.assertEqual(prepared_job['request']['symbols'],['BTCUSDT'])
            work.assert_called_once()


    def test_open_signal_chart_refresh_reads_new_closed_bars_without_changing_saved_run(self):
        from unittest.mock import patch
        data=bars(count=200)
        self.store.save_candles(data[:150])
        job=self.job(purpose='signals')
        self.service.work(job,self.settings,[])
        job['completed_at']=data[149].close_time
        self.service.jobs[job['id']]=job
        saved_items=list(job['items'])
        app=FastAPI()
        install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client,patch('monitor.workbench.time.time',return_value=(data[179].close_time+1)/1000):
            url=f'/api/workbench/runs/{job["id"]}/chart?refresh=true'
            first=client.get(url).json()
            self.assertEqual(len(first['candles']),150)
            self.store.save_candles(data[150:])
            fresh=client.get(url).json()
            self.assertEqual(len(fresh['candles']),180)
            self.assertTrue(all(m['time']<=data[179].close_time for m in fresh['markers']))
            self.assertFalse(any(m['action'].startswith('close') for m in fresh['markers']))
            self.assertGreater(len(fresh['markers']),len(first['markers']))
            self.assertEqual(job['items'],saved_items)
            self.assertEqual(fresh['row']['timeframe'],'5m')

    def test_signal_chart_can_switch_rule_and_period_without_changing_scan(self):
        data=bars(count=200)
        self.store.save_candles(data)
        start=data[0].open_time
        self.store.save_candles([replace(c,timeframe='15m',open_time=start+i*900000,close_time=start+(i+1)*900000-1) for i,c in enumerate(data[:60])])
        job=self.job(purpose='signals')
        self.service.work(job,self.settings,[])
        self.service.jobs[job['id']]=job
        original=dict(job['request'])
        app=FastAPI();install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            url=f'/api/workbench/runs/{job["id"]}/chart'
            result=client.get(url,params={'timeframe':'15m','rule':'wt_extreme_cross'})
            self.assertEqual(result.status_code,200)
            chart=result.json()
            self.assertEqual(chart['row']['timeframe'],'15m')
            self.assertEqual(len(chart['candles']),60)
            self.assertTrue(all(c['timeframe']=='15m' for c in chart['candles']))
            self.assertTrue(all(m['action'] in ('buy','sell','record') for m in chart['markers']))
            self.assertEqual(job['request'],original)
            self.assertEqual(client.get(url,params={'rule':'ema_cross'}).status_code,422)
            self.assertEqual(client.get(url,params={'timeframe':'invalid'}).status_code,422)
            job['request']['engine']='indicator'
            self.assertEqual(client.get(url,params={'rule':'wt_extreme_cross','timeframe':'15m'}).status_code,200)
            self.assertEqual(client.get(url,params={'rule':'hull_long'}).status_code,422)
            job['request']['purpose']='backtest'
            self.assertEqual(client.get(url,params={'timeframe':'15m'}).status_code,422)

    def test_account_chart_uses_actual_fills_and_never_shows_beyond_request_end(self):
        data=bars(count=180)
        self.store.save_candles(data)
        job=self.job(end=local_time(data[120].close_time).replace(" ","T"))
        row=dict(symbol="BTCUSDT",timeframe="5m",signal_time=data[100].close_time,
            entry_time=data[101].open_time,entry_price=1,exit_time=data[160].close_time,exit_price=2,
            account_entry_time=data[101].open_time,account_entry_price=100,account_exit_time=data[110].close_time,
            account_exit_price=80,account_status="风险强平 · OHLC近似",account_return_pct=-60)
        job.update(state="completed",completed_at=data[-1].close_time,portfolio={"model":"test"},items=[row])
        self.service.jobs[job["id"]]=job
        app=FastAPI()
        install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            result=client.get(f'/api/workbench/runs/{job["id"]}/chart').json()
            self.assertEqual(result["row"]["exit_time"],row["account_exit_time"])
            self.assertEqual(result["row"]["entry_price"],100)
            self.assertIn("近似风险强平",result["note"])
            self.assertLessEqual(max(c["close_time"] for c in result["candles"]),data[120].close_time)
            row.pop("account_entry_time")
            self.assertEqual(client.get(f'/api/workbench/runs/{job["id"]}/chart').status_code,422)

    def test_long_position_chart_preserves_original_bars_and_entry_exit(self):
        data=bars(count=1500)
        self.store.save_candles(data)
        job=self.job(engine="quant",strategy="supertrend_reversal",end=local_time(data[-1].close_time).replace(" ","T"))
        job.update(completed_at=data[-1].close_time,state="completed",items=[dict(symbol="BTCUSDT",timeframe="5m",signal_time=data[100].close_time,entry_time=data[101].open_time,exit_time=data[1400].open_time)])
        self.service.jobs[job["id"]]=job
        app=FastAPI()
        install_workbench_routes(app,self.service,lambda _:None,lambda:self.settings)
        with TestClient(app) as client:
            result=client.get(f'/api/workbench/runs/{job["id"]}/chart').json()
        self.assertNotIn("合并",result["note"])
        self.assertGreater(len(result["candles"]),1000)
        for time in [data[100].close_time,data[101].open_time,data[1400].open_time]:
            self.assertTrue(any(c["open_time"]<=time<=c["close_time"] for c in result["candles"]))


if __name__ == "__main__": unittest.main()
