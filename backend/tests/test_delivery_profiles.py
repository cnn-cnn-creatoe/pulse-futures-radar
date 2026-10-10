import unittest
from types import SimpleNamespace
from monitor.telegram import normalize_preferences,signal_matches,format_signal,_parse_command
from monitor.delivery_profiles import normalize_profiles
from monitor.delivery_menu import token,overview
from monitor.live_rule_signals import LiveRuleSignals,rule_catalog
from unittest.mock import AsyncMock,Mock
from copy import deepcopy

class DeliveryProfilesTests(unittest.TestCase):
    def setUp(self):
        self.ind=rule_catalog('indicator')[0]['id'];self.quant=self.ind
        self.rows=[dict(kind='indicator',rule_id=self.ind,timeframes=['5m'],symbols=['BTCUSDT'],patterns=[],min_activity=35,min_actionability=60)]
        self.p=normalize_preferences(dict(signals_enabled=True,rule_settings=self.rows))
    def event(self,**changes):
        return dict(source='live',symbol='BTCUSDT',timeframe='5m',rule_kind='indicator',rule_id=self.ind,entry_direction='long',activity_score=40,actionability={'score':70},pattern='阳阳',**changes)
    def test_reset_keeps_selected_rules_periods_and_delivery_switches(self):
        from monitor.telegram import reset_filter_preferences
        reset=reset_filter_preferences(self.p)
        self.assertTrue(reset['signals_enabled'])
        self.assertEqual([r['rule_id'] for r in reset['rule_settings']],[self.ind])
        self.assertEqual(reset['rule_settings'][0]['timeframes'],['5m'])
        self.assertEqual(reset['rule_settings'][0]['min_activity'],0)
        self.assertEqual(reset['rule_settings'][0]['symbols'],[])
    def test_mixed_rules_do_not_share_filters(self):
        signal=self.event();self.assertTrue(signal_matches(signal,self.p)[0])
        for update in ({'symbol':'ETHUSDT'},{'timeframe':'4h'},{'activity_score':34},{'actionability':{'score':59}}):self.assertFalse(signal_matches({**signal,**update},self.p)[0])
        quant={**signal,'symbol':'ETHUSDT','timeframe':'4h','rule_kind':'quant','rule_id':self.quant,'pattern':'阴阳','activity_score':80,'actionability':{'score':20}}
        self.assertFalse(signal_matches(quant,self.p)[0])
        self.assertFalse(signal_matches({**quant,'pattern':'阳阳'},self.p)[0])
        other=normalize_preferences(dict(signals_enabled=True,rule_settings=[]))
        self.assertFalse(signal_matches(signal,other)[0])
    def test_validation_and_all_symbols_missing_scores(self):
        for patch in ({'timeframes':[]},{'min_activity':float('nan')},{'symbols':['BTC USDT']},{'patterns':['bad']}):
            with self.assertRaises(ValueError):normalize_profiles([{**self.rows[0],**patch}])
        with self.assertRaises(ValueError):normalize_profiles(self.rows+self.rows[:1])
        p=normalize_preferences(dict(signals_enabled=True,rule_settings=[{**self.rows[0],'symbols':[],'min_activity':0,'min_actionability':0}]))
        self.assertTrue(signal_matches({**self.event(),'symbol':'SOLUSDT','activity_score':None,'actionability':{}},p)[0])
        self.assertFalse(signal_matches({**self.event(),'actionability':{}},self.p)[0])
    def test_subscription_union_and_symbol_filter(self):
        tg=SimpleNamespace(_accept_events=True,state=SimpleNamespace(subscribers=lambda:[{'preferences':self.p}]))
        live=LiveRuleSignals(None,tg,None)
        self.assertEqual(live.subscriptions('5m','BTCUSDT'),{('indicator',self.ind)})
        self.assertEqual(live.subscriptions('5m','ETHUSDT'),set())
        self.assertEqual(live.subscriptions('4h','SOLUSDT'),set())
    def test_compact_message_preserves_exit_and_no_pattern(self):
        msg=format_signal({**self.event(),'rule_kind':'quant','signal_kind':'平多','rule_name':'测试规则','close_time':0,'pattern':'十字星'},self.p)
        self.assertEqual(len(msg.splitlines()),5)
        for word in ('平多','北京时间','活跃度：40','可做度：70','形态：无','策略：测试规则'):self.assertIn(word,msg)
        self.assertNotIn('筛选',msg)
    def test_menu_tokens_and_coins_command(self):
        key=token('indicator',self.ind)
        self.assertEqual(_parse_command('/coins '+key+' BTCUSDT ETHUSDT'),('coins',key+' BTCUSDT ETHUSDT'))
        self.assertTrue(all(len(b['callback_data'].encode())<=64 for row in overview(self.p)['inline_keyboard'] for b in row))

class DeliveryMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_rule_changes_and_coin_command_are_isolated(self):
        from monitor.delivery_menu import handle,coins
        rule=rule_catalog('indicator')[0]['id'];key=token('indicator',rule)
        saved={1:normalize_preferences({}),2:normalize_preferences({})}
        def update(p,chat_id):saved[chat_id]=normalize_preferences(p);return saved[chat_id]
        service=SimpleNamespace(state=SimpleNamespace(update_preferences=update),config=SimpleNamespace(update=Mock()),_client=SimpleNamespace(call=AsyncMock()),_rate_wait=AsyncMock(),_send_menu=AsyncMock(),_send_text=AsyncMock())
        def member():return dict(chat_id=2,role='user',preferences=deepcopy(saved[2]))
        await handle(service,member(),'q:add:'+key,1)
        await handle(service,member(),'q:tf:'+key+':4h',1)
        await handle(service,member(),'q:tf:'+key+':5m',1)
        await coins(service,member(),key+' BTCUSDT ETHUSDT')
        self.assertEqual(saved[2]['rule_settings'][0]['timeframes'],['4h'])
        self.assertEqual(saved[2]['rule_settings'][0]['symbols'],['BTCUSDT','ETHUSDT'])
        self.assertIsNone(saved[1]['rule_settings']);service.config.update.assert_not_called()
        await handle(service,member(),'q:remove:'+key,1)
        self.assertEqual(saved[2]['rule_settings'],[])

if __name__=='__main__':unittest.main()
