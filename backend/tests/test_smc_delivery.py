import unittest
from unittest.mock import AsyncMock,Mock,patch
from types import SimpleNamespace
from monitor.telegram import normalize_preferences,signal_matches,format_signal
from monitor.delivery_menu import handle,token

ROW=dict(kind='indicator',rule_id='smc_equity_structure',timeframes=['4h'],symbols=['NVDAUSDT'],patterns=[],min_activity=0,min_actionability=0)
EVENT=dict(rule_kind='indicator',rule_id='smc_equity_structure',symbol='NVDAUSDT',timeframe='4h',entry_direction='short',source='live',event_type='zone_rejection',zone_context={'atr_ready':True},zone={'side':'supply','lower':100,'upper':110},assessment={'ready':True,'volume_ratio':2,'atr_pct':1},signal_price=99,observed_at=1000,intrabar=True)
class SmcDeliveryTests(unittest.TestCase):
    def test_filters_and_wait_conflict(self):
        p=normalize_preferences(dict(signals_enabled=True,rule_settings=[{**ROW,'smc_directions':['short'],'smc_events':['zone_rejection']}]))
        self.assertTrue(signal_matches(EVENT,p)[0])
        self.assertFalse(signal_matches({**EVENT,'zone_conflict':'conflict'},p)[0])
        self.assertFalse(signal_matches({**EVENT,'event_type':'zone_inside'},p)[0])
        p['rule_settings'][0]['smc_directions']=['wait'];p['rule_settings'][0]['smc_events']=[]
        self.assertTrue(signal_matches({**EVENT,'zone_conflict':'conflict'},p)[0])
    def test_content_only_changes_message(self):
        p=normalize_preferences(dict(signals_enabled=True,rule_settings=[{**ROW,'smc_content':[]}]))
        self.assertTrue(signal_matches(EVENT,p)[0]);text=format_signal(EVENT,p)
        self.assertNotIn('成交额',text);self.assertNotIn('供给压力区',text)
        p['rule_settings'][0]['smc_content']=['zones','volume']
        text=format_signal(EVENT,p);self.assertIn('110 — 100',text);self.assertIn('2.00倍',text)
    def test_invalid_options_rejected(self):
        with self.assertRaises(ValueError):normalize_preferences(dict(rule_settings=[{**ROW,'smc_directions':['invalid']}]))

class SmcMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_callback_persists_only_member_and_has_short_payloads(self):
        p=normalize_preferences(dict(signals_enabled=True,rule_settings=[ROW]))
        member=dict(chat_id=123,role='member',preferences=p)
        service=SimpleNamespace(state=SimpleNamespace(update_preferences=Mock(side_effect=lambda value,**kw:value)),config=Mock())
        t=token('indicator','smc_equity_structure')
        with patch('monitor.delivery_menu.show',new_callable=AsyncMock) as show:
            await handle(service,member,'q:sd:'+t+':long',1)
            saved=service.state.update_preferences.call_args.args[0]
            self.assertEqual(saved['rule_settings'][0]['smc_directions'],['short','wait'])
            self.assertEqual(service.state.update_preferences.call_args.kwargs,{'chat_id':123})
            service.config.update.assert_not_called()
            for row in show.call_args.args[-1]:
                for button in row:self.assertLessEqual(len(button['callback_data'].encode()),64)


class EquityPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_toggle_clear_and_empty_catalog(self):
        from copy import deepcopy
        available=['NVDAUSDT','MUUSDT']+[f'TEST{i}USDT' for i in range(20)]
        member=dict(chat_id=321,role='member',preferences=normalize_preferences(dict(signals_enabled=True,rule_settings=[ROW])))
        service=SimpleNamespace(equity_symbols=lambda:available,state=SimpleNamespace(update_preferences=Mock(side_effect=lambda p,**kw:p)),config=Mock())
        t=token('indicator','smc_equity_structure')
        with patch('monitor.delivery_menu.show',new_callable=AsyncMock) as show:
            await handle(service,member,'q:eqall:'+t+':0',1)
            saved=deepcopy(service.state.update_preferences.call_args.args[0]);self.assertEqual(set(saved['rule_settings'][0]['symbols']),set(available))
            member['preferences']=saved
            await handle(service,member,'q:eqcoin:'+t+':'+token('equity','MUUSDT')+':0',1)
            saved=deepcopy(service.state.update_preferences.call_args.args[0]);self.assertNotIn('MUUSDT',saved['rule_settings'][0]['symbols'])
            member['preferences']=saved
            before=service.state.update_preferences.call_count
            await handle(service,member,'q:eqpage:'+t+':1',1)
            self.assertEqual(service.state.update_preferences.call_count,before)
            self.assertIn('已选 21',show.call_args.args[-2])
            for row in show.call_args.args[-1]:
                for b in row:self.assertLessEqual(len(b['callback_data'].encode()),64)
            service.equity_symbols=lambda:[]
            await handle(service,member,'q:eqall:'+t+':0',1)
            self.assertEqual(service.state.update_preferences.call_count,before)
            await handle(service,member,'q:eqclear:'+t+':0',1)
            self.assertEqual(service.state.update_preferences.call_args.args[0]['rule_settings'][0]['symbols'],[])
            service.config.update.assert_not_called()


class ManualCoinTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_codes_and_invalid_input_preserve_selection(self):
        from monitor.delivery_menu import coins
        p=normalize_preferences(dict(signals_enabled=True,rule_settings=[ROW]))
        member=dict(chat_id=321,role='member',preferences=p)
        service=SimpleNamespace(equity_symbols=lambda:['NVDAUSDT','MUUSDT'],state=SimpleNamespace(update_preferences=Mock(side_effect=lambda p,**kw:p)),config=Mock(),_send_text=AsyncMock())
        t=token('indicator','smc_equity_structure')
        with patch('monitor.delivery_menu.handle',new_callable=AsyncMock):
            await coins(service,member,t+' nvda，MUUSDT nvda')
            self.assertEqual(service.state.update_preferences.call_args.args[0]['rule_settings'][0]['symbols'],['NVDAUSDT','MUUSDT'])
            service.state.update_preferences.reset_mock()
            await coins(service,member,t+' NVDA WRONG')
            service.state.update_preferences.assert_not_called()
            self.assertIn('原名单未修改',service._send_text.call_args.args[1])
