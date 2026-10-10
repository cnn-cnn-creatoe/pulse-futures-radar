import unittest,math,random
from dataclasses import replace
from monitor.nan_strategy1 import actions,events,outcomes,RULE_ID
from monitor.engine import Candle
from monitor.workbench import WorkbenchRequest,signal_stream
from monitor.policy import Settings
from monitor.portfolio import simulate
from monitor.source_browser import source_for
from monitor.telegram import normalize_preferences,migrate_preferences,signal_matches
from monitor.delivery_profiles import profiles

def candles(n=100):
    return [Candle('BTCUSDT','5m',i*300000,(i+1)*300000-1,100+i*.01,102+i*.01,98+i*.01,101+i*.01,10000,5000,10,100) for i in range(n)]

class NanStrategyTests(unittest.TestCase):
    def test_exact_turns_close_before_reverse_and_zone_reentry(self):
        cs=candles(44);wt=[0.]*44;line=[-50.]*44
        wt[33:]=[-59,-60,-59,-58,-59,-60,-59,60,59,58,59];line[40]=50
        self.assertEqual(actions(cs,wt,line),[(35,'long'),(37,'close_long'),(39,'long'),(41,'close_long'),(41,'short'),(43,'close_short')])
        self.assertNotIn((39,'long'),actions(cs,wt,line,one_per_zone=True))
    def test_prefix_causality_and_independent_scalar_reference(self):
        rng=random.Random(42);cs=candles(1600);value=100
        for i,c in enumerate(cs):
            value+=rng.uniform(-2,2)
            cs[i]=replace(c,open=value,high=value+2,low=value-2,close=value+math.sin(i/5))
        actual=actions(cs)
        for n in (36,80,250,1000):self.assertEqual(actions(cs[:n]),[(i,a) for i,a in actual if i<n])
        # Independent scalar Pine-style EMA updates and scalar var state.
        esa=deviation=average=None;series=[];signal=[];pos=0;expected=[]
        for i,c in enumerate(cs):
            price=(c.high+c.low+c.close)/3
            esa=price if esa is None else (2/11)*price+(9/11)*esa
            distance=abs(price-esa)
            deviation=distance if deviation is None else (2/11)*distance+(9/11)*deviation
            ci=(price-esa)/(.015*deviation) if deviation>0 else None
            if ci is not None:average=ci if average is None else (2/22)*ci+(20/22)*average
            series.append(average if ci is not None else None)
            window=series[-4:];signal.append(sum(window)/4 if len(window)==4 and all(v is not None for v in window) else None)
            if i<35 or series[i-2] is None or signal[i-1] is None:continue
            up=series[i-2]>series[i-1]<series[i]
            down=series[i-2]<series[i-1]>series[i]
            if pos==1 and down:expected.append((i,'close_long'));pos=0
            elif pos==-1 and up:expected.append((i,'close_short'));pos=0
            if not pos:
                if up and series[i-1]<=-53 and series[i-1]<signal[i-1]:expected.append((i,'long'));pos=1
                elif down and series[i-1]>=53 and series[i-1]>signal[i-1]:expected.append((i,'short'));pos=-1
        self.assertGreater(len(expected),20);self.assertEqual(actual,expected)
    def test_explicit_exits_and_all_equity_fee_reserve(self):
        cs=candles(10)
        def signal(i,action):return dict(id=str(i)+action,symbol='BTCUSDT',timeframe='5m',close_time=cs[i].close_time,price=cs[i].close,direction='long' if action in ('long','close_long') else 'short',pattern='阳阳',signal_kind=action,action=action,indicator_results={RULE_ID:True})
        opts=dict(initial_capital=1000,margin_per_trade=1000,leverage=3,capital_mode='equity',fee_bps=5,slippage_bps=3,max_positions=1,maintenance_margin_pct=.5)
        signals=[signal(1,'long'),signal(3,'close_long'),signal(3,'short'),signal(6,'close_short')]
        rows=outcomes(signals,cs,opts)
        self.assertEqual(rows[0]['exit_time'],cs[4].open_time);self.assertEqual(rows[1]['entry_time'],cs[4].open_time)
        result=simulate(rows,{('BTCUSDT','5m'):cs},opts,0,cs[-1].close_time)
        self.assertEqual(result['closed'],2);self.assertEqual(result['skipped'],0)
        balance=1000
        for row in rows:
            margin=balance/(1+3*.0005);qty=margin*3/row['entry_price'];side=1 if row['direction']=='long' else -1
            pnl=side*qty*(row['exit_price']-row['entry_price'])-qty*(row['entry_price']+row['exit_price'])*.0005
            balance+=pnl
        self.assertAlmostEqual(result['final_equity'],balance)
        close_rows=outcomes(signals,cs,{**opts,'fill_mode':'signal_close'})
        self.assertEqual(close_rows[0]['entry_time'],cs[1].close_time)
    def test_catalog_request_and_subscriber_persistence(self):
        WorkbenchRequest(start='2023-09-14',end='2026-09-14',indicators=[RULE_ID])
        with self.assertRaises(ValueError):WorkbenchRequest(start='2023-09-14',end='2026-09-14',indicators=[RULE_ID,'wt_extreme_cross'])
        self.assertIn('WaveTrend B',source_for(RULE_ID)['code'])
        p=normalize_preferences(dict(rule_settings=[dict(kind='indicator',rule_id=RULE_ID,timeframes=['5m'],min_activity=80,min_actionability=80)],signals_enabled=True))
        self.assertEqual(migrate_preferences(p),p);self.assertEqual(profiles(p),p['rule_settings'])
        s=dict(rule_kind='indicator',rule_id=RULE_ID,source='live',timeframe='5m',symbol='BTCUSDT',signal_kind='卖出平多')
        self.assertTrue(signal_matches(s,p)[0])
        self.assertFalse(signal_matches({**s,'signal_kind':'做多'},p)[0])
