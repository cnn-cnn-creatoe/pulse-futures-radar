"""Scope expansion and metadata compatibility, using only temporary SQLite."""
import json
from pathlib import Path
import tempfile
import unittest

from monitor.engine import Candle
from monitor.store import Store, contract_metadata_fields


def contract(symbol='BTCUSDT', **changes):
    return {'symbol':symbol,'pair':symbol,'status':'TRADING','futuresType':'U_MARGINED',
            'baseAsset':'BTC','quoteAsset':'USDT','marginAsset':'USDT','contractType':'PERPETUAL',
            'underlyingType':'COIN','underlyingSubType':[],'onboardDate':100,
            'deliveryDate':4133404800000,**changes}


class UniverseStoreTests(unittest.TestCase):
    def test_quote_filter_precedes_limit_and_keeps_unfiltered_store_and_exact_detail(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        for symbol,close,quote in (('BTCUSDT',59999,'USDT'),('ETHUSDC',119999,'USDC')):
            store.save_alert({'symbol':symbol,'timeframe':'1m','close_time':close,'quote_asset':quote,'source':'live'})
        self.assertEqual(store.list_alerts(limit=1)[0]['symbol'],'ETHUSDC')
        self.assertEqual(store.list_alerts(limit=1,quote_asset='USDT')[0]['symbol'],'BTCUSDT')
        old_record = store.list_alerts(limit=1)[0]
        self.assertEqual(store.get_alert(old_record['id']),old_record)
        self.assertEqual(len(store.list_alerts()),2)

    def test_quote_filter_accepts_delivery_and_unicode_but_never_overrides_explicit_quote(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        cases = [
            ('BTCUSDT_260925',{},True),('币安人生USDT',{},True),
            ('BTCUSDT_260925EXTRA',{},False),('BTCUSDT_26092',{},False),
            ('BTCUSDT_２６０９２５',{},False),('ETHUSDC',{},False),
            ('MISLEADINGUSDT',{'quote_asset':'USDC'},False),
            ('MISLEADINGUSDT_260925',{'quoteAsset':'USDC'},False),
            ('SNAKEUSDT',{'quote_asset':'USDC','quoteAsset':'USDT'},False),
            ('NULLUSDT',{'quote_asset':None},False),
            ('METADATAPAIR',{'quote_asset':'USDT','quoteAsset':'USDC'},True),
        ]
        for index,(symbol,extra,_) in enumerate(cases):
            store.save_alert({'symbol':symbol,'timeframe':'1m','close_time':(index+1)*60000-1,'source':'live',**extra})
        wanted = {symbol for symbol,_,included in cases if included}
        self.assertEqual({row['symbol'] for row in store.list_alerts(quote_asset='USDT')},wanted)
        self.assertEqual(len(store.list_alerts()),len(cases))

    def test_expanding_to_older_tradfi_and_delivery_is_not_a_new_listing(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        baseline = contract()
        self.assertEqual(store.reconcile_universe([baseline],1000),[])
        self.assertEqual(store.reconcile_universe([baseline],5000),[])
        # Both were listed after initial boot but before the latest successful
        # narrow-scope snapshot. Expanding scope is discovery, not an IPO.
        old_equity = contract('TSLAUSDT',onboardDate=2500,contractType='TRADIFI_PERPETUAL',
                              underlyingType='EQUITY',underlyingSubType=['TradFi'],baseAsset='TSLA')
        old_delivery = contract('BTCUSDT_260925',onboardDate=4000,contractType='CURRENT_QUARTER',
                                pair='BTCUSDT',deliveryDate=1790323200000)
        self.assertEqual(store.reconcile_universe([baseline,old_equity,old_delivery],6000),[])
        self.assertEqual(store.list_listing_events(),[])
        self.assertEqual(store.universe_snapshot_status()['initialized_at'],1000)
        self.assertEqual(store.universe_snapshot_status()['snapshot_count'],3)

    def test_unknown_new_listing_pending_transition_and_dedup_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'market.sqlite'
            store = Store(path)
            store.reconcile_universe([contract()],1000)
            new = contract('NEWUSDT',onboardDate=1500,contractType='TRADIFI_PERPETUAL',
                           underlyingType='HK_EQUITY',underlyingSubType=['TradFi'])
            events = store.reconcile_universe([contract(),new],2000)
            self.assertEqual([(event['symbol'],event['event_type']) for event in events],[('NEWUSDT','listed')])
            self.assertTrue(events[0]['is_tradfi'])
            pending = contract('BTCUSDT_261225',status='PENDING_TRADING',onboardDate=4000,
                               contractType='NEXT_QUARTER',deliveryDate=1798185600000)
            self.assertEqual(store.reconcile_universe([new,pending],3000)[0]['event_type'],'upcoming')
            store.close()
            store = Store(path)
            try:
                self.assertEqual(store.reconcile_universe([new,pending],3500),[])
                listed = {**pending,'status':'TRADING'}
                events = store.reconcile_universe([new,listed],4500)
                self.assertEqual([event['event_type'] for event in events],['listed'])
                self.assertEqual(events[0]['delivery_date'],1798185600000)
                self.assertEqual(events[0]['contract_type'],'NEXT_QUARTER')
                self.assertEqual(store.reconcile_universe([new,listed],5000),[])
                self.assertEqual(len(store.list_listing_events()),3)
            finally:
                store.close()

    def test_scope_removal_preserves_old_metadata_candles_and_does_not_relist(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        usdt, usdc = contract(), contract('ETHUSDC',baseAsset='ETH',quoteAsset='USDC',marginAsset='USDC')
        store.reconcile_universe([usdt,usdc],1000)
        store.save_candles([Candle('ETHUSDC','1m',0,59999,100,102,99,101)])
        self.assertEqual(store.reconcile_universe([usdt],2000),[])
        self.assertEqual([row['symbol'] for row in store.list_universe()],['BTCUSDT'])
        retained = {row['symbol']:row for row in store.list_universe(active_only=False)}
        self.assertFalse(retained['ETHUSDC']['snapshot_active'])
        self.assertEqual(retained['ETHUSDC']['quote_asset'],'USDC')
        self.assertEqual(len(store.get_candles('ETHUSDC','1m')),1)
        self.assertEqual(store.reconcile_universe([usdt,usdc],3000),[])
        self.assertEqual(store.list_listing_events(),[])

    def test_existing_payload_supplies_aliases_without_schema_migration_or_rewrite(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'existing.sqlite'
            store = Store(path)
            metadata = contract('BTCUSDT_260925',pair='BTCUSDT',contractType='CURRENT_QUARTER',
                                deliveryDate=1790323200000,pricePrecision=1,customOfficialField={'keep':True})
            store.reconcile_universe([metadata],1000)
            original = store._db.execute('SELECT payload FROM universe_symbols').fetchone()[0]
            columns = [row[1] for row in store._db.execute('PRAGMA table_info(universe_symbols)')]
            store.close()
            store = Store(path)
            try:
                row = store.list_universe()[0]
                for field,value in metadata.items():
                    self.assertEqual(row[field],value)
                self.assertEqual(row['contract_type'],'CURRENT_QUARTER')
                self.assertEqual(row['margin_asset'],'USDT')
                self.assertEqual(row['delivery_date'],1790323200000)
                self.assertFalse(row['is_tradfi'])
                self.assertEqual(store._db.execute('SELECT payload FROM universe_symbols').fetchone()[0],original)
                self.assertEqual([row[1] for row in store._db.execute('PRAGMA table_info(universe_symbols)')],columns)
            finally:
                store.close()

    def test_metadata_does_not_turn_crypto_indexes_or_perpetual_sentinels_into_tradfi_expiry(self):
        index = contract('BTCDOMUSDT',underlyingType='INDEX',underlyingSubType=['Index'])
        result = contract_metadata_fields(index)
        self.assertFalse(result['is_tradfi'])
        self.assertIsNone(result['delivery_date'])
        for category in ('EQUITY','HK_EQUITY','KR_EQUITY','CN_EQUITY','COMMODITY'):
            with self.subTest(category=category):
                self.assertTrue(contract_metadata_fields(contract(underlyingType=category))['is_tradfi'])

    def test_old_listing_json_gets_display_metadata_without_mutating_persisted_event(self):
        store = Store(':memory:')
        self.addCleanup(store.close)
        metadata = contract('BTCUSDT_261225',contractType='NEXT_QUARTER',deliveryDate=1798185600000,onboardDate=1500)
        store.reconcile_universe([contract()],1000)
        store.reconcile_universe([metadata],2000)
        legacy = {'id':'BTCUSDT_261225:1500:listed','symbol':'BTCUSDT_261225','event_type':'listed',
                  'onboard_date':1500,'observed_at':2000,'source':'live'}
        payload = json.dumps(legacy)
        with store._db:
            store._db.execute('UPDATE listing_events SET payload=?',(payload,))
        row = store.list_listing_events()[0]
        self.assertEqual(row['contract_type'],'NEXT_QUARTER')
        self.assertEqual(row['delivery_date'],1798185600000)
        self.assertEqual(store._db.execute('SELECT payload FROM listing_events').fetchone()[0],payload)


if __name__ == '__main__':
    unittest.main()
