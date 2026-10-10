import unittest
from tempfile import TemporaryDirectory
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from monitor.engine import Candle
from monitor.smc_structure import Structure, RULE_ID
from monitor.smc_live import SmcLive, equity
from monitor.telegram import normalize_preferences, migrate_preferences, signal_matches, format_signal
from monitor.live_rule_signals import LiveRuleSignals


def candle(i,close=100,high=110,low=90):
    return Candle(symbol='TSLAUSDT',timeframe='1m',open_time=i*60000,close_time=i*60000+59999,open=100,high=high,low=low,close=close,volume=10,quote_volume=1000,taker_buy_quote=500,trades=2,closed=True)

def prefs(symbols=None):
    return normalize_preferences(dict(signals_enabled=True,rule_settings=[dict(kind='indicator',rule_id=RULE_ID,timeframes=['1m'],symbols=['TSLAUSDT'] if symbols is None else symbols)]))

class StructureTests(unittest.TestCase):
    def ready(self):
        m=Structure();m.advance(candle(0))
        m.groups['swing'].update(high=105,low=95)
        m.groups['internal'].update(high=103,low=97)
        return m
    def test_preview_rollback_and_strict_cross(self):
        m=self.ready()
        a=m.preview(candle(1,106))
        self.assertEqual([(r['structure'],r['tag'],r['direction']) for r in a],[('internal','BOS','long'),('swing','BOS','long')])
        self.assertEqual(m.count,1)
        self.assertEqual(m.groups['swing']['bias'],0)
        self.assertEqual(m.preview(candle(1,100)),[])
        self.assertFalse(any(s['structure']=='swing' for s in m.preview(candle(1,105))))
        self.assertEqual(m.preview(candle(1,106)),a)
    def test_display_levels_preview_rolls_back(self):
        m=self.ready()
        signals,levels=m.preview_details(candle(1,106))
        self.assertEqual(levels['swing']['resistance'],105)
        self.assertEqual(levels['swing']['support'],95)
        self.assertTrue(levels['swing']['resistance_broken'])
        self.assertFalse(levels['swing']['support_broken'])
        self.assertFalse(m.levels()['swing']['resistance_broken'])
        self.assertEqual(signals,m.preview(candle(1,106)))
        _,retracted=m.preview_details(candle(1,100))
        self.assertFalse(retracted['swing']['resistance_broken'])

    def test_commit_no_repeat_and_reversal(self):
        m=self.ready();m.advance(candle(1,106))
        self.assertEqual(m.advance(candle(2,107)),[])
        r=m.preview(candle(3,94))
        self.assertEqual([v['tag'] for v in r],['CHoCH','CHoCH'])
        self.assertTrue(all(v['direction']=='short' for v in r))
    def test_same_pivot_internal_suppression_and_gap(self):
        m=self.ready();m.groups['internal']['high']=105
        self.assertEqual([r['structure'] for r in m.preview(candle(1,106))],['swing'])
        with self.assertRaises(ValueError):m.preview(candle(2))
    def test_confirmed_leg_lookback_not_future(self):
        m=Structure()
        for i in range(50):m.advance(candle(i,high=110,low=80 if i==0 else 90))
        self.assertIsNone(m.groups['swing']['low'])
        m.advance(candle(50))
        self.assertEqual(m.groups['swing']['low'],80)
        self.assertFalse(m.groups['swing']['low_crossed'])
    def test_previous_pivot_series_used_for_crossover(self):
        m=self.ready();m.groups['swing']['high']=99
        # Price was already above old pivot: Pine crossover must not fire.
        self.assertFalse(any(s['structure']=='swing' for s in m.preview(candle(1,106))))

class LiveTests(unittest.TestCase):
    def test_full_equity_selection_over_30_roundtrips(self):
        symbols=[f'STOCK{i}USDT' for i in range(178)]
        p=prefs(symbols)
        self.assertEqual(p['rule_settings'][0]['symbols'],symbols)
        self.assertEqual(migrate_preferences(p)['rule_settings'][0]['symbols'],symbols)

    def test_equity_only(self):
        meta=dict(st=1,symbol='TSLAUSDT',status='TRADING',quoteAsset='USDT',contractType='TRADIFI_PERPETUAL',underlyingType='EQUITY')
        self.assertTrue(equity(meta))
        self.assertFalse(equity({**meta,'underlyingType':'COIN'}))
        self.assertFalse(equity({**meta,'underlyingType':'HK_EQUITY'}))
        self.assertFalse(equity({**meta,'status':'SETTLING'}))
    def test_preferences_migration_empty_and_closed_exclusion(self):
        p=prefs();self.assertEqual(migrate_preferences(p)['rule_settings'],p['rule_settings'])
        tg=SimpleNamespace(_accept_events=True,state=SimpleNamespace(subscribers=lambda:[dict(preferences=p)]))
        self.assertFalse(LiveRuleSignals(None,tg,None).subscriptions('1m','TSLAUSDT'))
        event=dict(rule_kind='indicator',rule_id=RULE_ID,symbol='TSLAUSDT',timeframe='1m',entry_direction='long',source='live')
        self.assertTrue(signal_matches(event,p)[0]);self.assertFalse(signal_matches(event,prefs([]))[0])
        self.assertFalse(signal_matches({**event,'symbol':'BTCUSDT'},p)[0])
    def test_persistent_dedup_stale_queue_rejection(self):
        p=prefs();meta=dict(st=1,symbol='TSLAUSDT',status='TRADING',quoteAsset='USDT',contractType='TRADIFI_PERPETUAL',underlyingType='EQUITY')
        market=SimpleNamespace(now=lambda:70000,_metadata={'TSLAUSDT':meta})
        tg=SimpleNamespace(_accept_events=True,state=SimpleNamespace(subscribers=lambda:[dict(preferences=p)]),enqueue=Mock(return_value=True))
        sig=[dict(structure='internal',tag='BOS',direction='long',level=103)]
        with TemporaryDirectory() as temp:
            path=Path(temp)/'test.db';live=SmcLive(market,tg,path)
            live.publish(candle(1,106),60000,sig);tg.enqueue.assert_not_called()
            tg.enqueue.return_value=False;live.publish(candle(1,106),69000,sig)
            self.assertEqual(len(live.status()['recent']),1)
            tg.enqueue.return_value=True;live.publish(candle(1,106),69000,sig)
            live.publish(candle(1,106),69500,sig)
            self.assertEqual(tg.enqueue.call_count,1)
            text=format_signal(live.status()['recent'][0],p)
            self.assertIn('内部 BOS',text);self.assertIn('触发价：106',text);self.assertIn('本根已收盘',text)
            live.db.close();live=SmcLive(market,tg,path)
            live.publish(candle(1,106),69900,sig);self.assertEqual(tg.enqueue.call_count,1)
            live.db.close()

class GroupingTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_stream_routes_equal_timestamps_per_period(self):
        import asyncio,json,aiohttp
        from unittest.mock import AsyncMock
        current=90000000;now=current+1000
        group=(('TSLAUSDT','1m'),('TSLAUSDT','5m'))
        def rows(params):
            step=60000 if params['interval']=='1m' else 300000
            return [[current-i*step,'100','110','90','100','10',current-(i-1)*step-1,'1000',2,'5','500'] for i in range(51,0,-1)]
        messages=[]
        for symbol,tf in group:
            step=60000 if tf=='1m' else 300000
            k=dict(t=current,T=current+step-1,i=tf,o='100',h='110',l='90',c='106',v='10',q='1000',Q='500',n=2,x=False)
            messages.append(SimpleNamespace(type=aiohttp.WSMsgType.TEXT,data=json.dumps(dict(data=dict(e='kline',s=symbol,E=now,k=k)))))
        class Socket:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def __aiter__(self):return self
            async def __anext__(self):
                if messages:return messages.pop(0)
                raise asyncio.CancelledError
        market=SimpleNamespace(now=lambda:now,fetch_klines=AsyncMock(side_effect=rows),_pace_ws_connection=AsyncMock(),_valid_candle=lambda c:True,_session=SimpleNamespace(ws_connect=Mock(return_value=Socket())))
        with TemporaryDirectory() as temp:
            live=SmcLive(market,None,Path(temp)/'stream.db');live.publish=Mock()
            with self.assertRaises(asyncio.CancelledError):await asyncio.wait_for(live.watch(group), timeout=5)
            self.assertEqual(live.publish.call_count,2)
            self.assertTrue(all(live.states[k]['state']=='live' for k in group))
            url=market._session.ws_connect.call_args.args[0]
            self.assertIn('tslausdt@kline_1m/tslausdt@kline_5m',url)
            await live.close()

    async def test_all_equities_are_grouped_without_dropping_periods(self):
        import asyncio
        from unittest.mock import AsyncMock, patch
        with TemporaryDirectory() as temp:
            live=SmcLive(None,None,Path(temp)/'groups.db')
            desired={(f'STOCK{i}USDT',tf) for i in range(178) for tf in ('1h','4h','1d')}
            live.desired=lambda:desired
            live.watch=AsyncMock()
            with patch('monitor.smc_live.asyncio.sleep',side_effect=asyncio.CancelledError):
                with self.assertRaises(asyncio.CancelledError):await live.run()
            self.assertEqual(set().union(*(set(g) for g in live.workers)),desired)
            self.assertEqual(len(live.workers),7)
            self.assertTrue(all(len(g)<=80 for g in live.workers))
            await live.close()

if __name__=='__main__':unittest.main()

class DailyHistoryTests(unittest.TestCase):
    def test_beijing_boundary_complete_day_and_persistence(self):
        import json
        from datetime import datetime
        start=int(datetime.fromisoformat('2026-10-08T00:00:00+08:00').timestamp()*1000)
        with TemporaryDirectory() as folder:
            path=Path(folder)/'history.sqlite3'
            market=SimpleNamespace(now=lambda:start+1000,_metadata={})
            tg=SimpleNamespace(_accept_events=False,state=SimpleNamespace(subscribers=lambda:[]))
            live=SmcLive(market,tg,path)
            for i in range(350):
                live.db.execute('INSERT INTO triggers VALUES (?,?,?)',(str(i),start+i,json.dumps({'id':str(i)})))
            live.db.execute('INSERT INTO triggers VALUES (?,?,?)',('before',start-1,json.dumps({'id':'before'})))
            live.db.execute('INSERT INTO triggers VALUES (?,?,?)',('after',start+86400000,json.dumps({'id':'after'})))
            live.db.commit();live.db.close()
            reopened=SmcLive(market,tg,path)
            self.assertEqual(len(reopened.status()['recent']),350)
            self.assertEqual(reopened.status('2026-10-07')['recent'],[{'id':'before'}])
            self.assertEqual(reopened.status('2026-10-09')['recent'],[{'id':'after'}])
            self.assertEqual(reopened.status('2026-10-06')['recent'],[])
            self.assertEqual(reopened.status()['days'],['2026-10-09','2026-10-08','2026-10-07'])
            with self.assertRaises(ValueError):reopened.status('invalid')
            reopened.db.close()


class LocalMonitoringTests(unittest.TestCase):
    def market(self):
        meta=dict(st=1,symbol='TSLAUSDT',status='TRADING',quoteAsset='USDT',contractType='TRADIFI_PERPETUAL',underlyingType='EQUITY')
        return SimpleNamespace(now=lambda:70000,_metadata={'TSLAUSDT':meta,'BTCUSDT':dict(meta,symbol='BTCUSDT',underlyingType='COIN')})

    def test_no_bot_defaults_all_equities_periods_and_restart_preserves_signal(self):
        from dataclasses import replace
        with TemporaryDirectory() as folder:
            path=Path(folder)/'local.db'; market=self.market(); live=SmcLive(market,None,path)
            self.assertEqual(live.desired(),{('TSLAUSDT',p) for p in ('1h','4h','1d')})
            self.assertEqual(len(live.status()['items']),3)
            bar=replace(candle(1,106),timeframe='1h')
            sig=[dict(structure='internal',tag='BOS',direction='long',level=103)]
            live.publish(bar,69000,sig)
            saved=live.status()['recent'];self.assertEqual(len(saved),1)
            live.db.close();live=SmcLive(market,None,path)
            self.assertEqual(live.status()['recent'],saved)
            live.publish(bar,69500,sig);self.assertEqual(live.status()['recent'],saved)
            market._metadata['NVDAUSDT']=dict(market._metadata['TSLAUSDT'],symbol='NVDAUSDT')
            self.assertEqual(len(live.desired()),6)
            live.db.close()

    def test_disabled_or_broken_telegram_cannot_prevent_local_storage(self):
        from dataclasses import replace
        for enabled in (False,True):
            with self.subTest(enabled=enabled), TemporaryDirectory() as folder:
                tg=SimpleNamespace(_accept_events=enabled,state=SimpleNamespace(subscribers=lambda:[]),enqueue=Mock(side_effect=RuntimeError('delivery down')))
                live=SmcLive(self.market(),tg,Path(folder)/'local.db')
                bar=replace(candle(1,106),timeframe='4h')
                sig=[dict(structure='internal',tag='BOS',direction='long',level=103)]
                if enabled:
                    with self.assertLogs('monitor.smc_live',level='ERROR'):live.publish(bar,69000,sig)
                else:
                    live.publish(bar,69000,sig);tg.enqueue.assert_not_called()
                self.assertEqual(len(live.status()['recent']),1)
                live.db.close()
