import unittest
from dataclasses import replace
from monitor.portfolio import simulate
from monitor.workbench import WorkbenchRequest
from test_workbench import bars


class PortfolioTests(unittest.TestCase):
    def run_case(self, leverage=1, fee=0, exit_index=3):
        cs=[replace(c,open=100+i,close=101+i,high=101+i,low=100+i) for i,c in enumerate(bars(count=5))]
        rows=[dict(id="a",symbol="BTCUSDT",timeframe="5m",direction="long",entry_time=cs[1].open_time,exit_time=cs[exit_index].open_time if exit_index else None)]
        opts=WorkbenchRequest(start="2026-09-01",end="2026-09-02",initial_capital=1000,margin_per_trade=100,leverage=leverage,fee_bps=fee,slippage_bps=0).model_dump()
        return simulate(rows,{("BTCUSDT","5m"):cs},opts,cs[0].open_time,cs[-1].close_time),rows

    def test_capital_quantity_leverage_and_fees(self):
        account,rows=self.run_case(leverage=3,fee=5)
        qty=300/101
        self.assertAlmostEqual(rows[0]["quantity"],qty)
        self.assertAlmostEqual(account["net_profit"],qty*(103-101)-qty*(101+103)*.0005)
        self.assertEqual(account["closed"],1)
        self.assertEqual(account["open"],0)

    def test_unclosed_profit_in_equity(self):
        account,rows=self.run_case(exit_index=None)
        self.assertEqual(account["closed"],0)
        self.assertEqual(account["open"],1)
        self.assertAlmostEqual(account["unrealized"],100/101*(105-101))
        self.assertIsNone(rows[0]["account_pnl"])

    def test_shared_wallet_does_not_fund_all_overlapping_signals(self):
        cs=[replace(c,open=100,close=100,high=100,low=100) for c in bars(count=4)]
        market={(s,"5m"):[replace(c,symbol=s) for c in cs] for s in ["BTCUSDT","ETHUSDT"]}
        rows=[dict(id=s,symbol=s,timeframe="5m",direction="long",entry_time=cs[1].open_time,exit_time=None) for s in ["ETHUSDT","BTCUSDT"]]
        opts=WorkbenchRequest(start="2026-09-01",end="2026-09-02",initial_capital=150,margin_per_trade=100).model_dump()
        account=simulate(rows,market,opts,cs[0].open_time,cs[-1].close_time)
        self.assertEqual(account["open"],1)
        self.assertEqual(account["skipped"],1)
        self.assertIn("资金不足",rows[0]["account_status"])

    def test_adverse_intrabar_triggers_risk_before_scheduled_close(self):
        cs=[replace(c,open=100,close=100,high=100,low=100) for c in bars(count=4)]
        cs[2]=replace(cs[2],low=80)
        row=dict(id="a",symbol="BTCUSDT",timeframe="5m",direction="long",entry_time=cs[1].open_time,exit_time=cs[2].close_time)
        opts=WorkbenchRequest(start="2026-09-01",end="2026-09-02",initial_capital=1000,margin_per_trade=100,leverage=10).model_dump()
        account=simulate([row],{("BTCUSDT","5m"):cs},opts,cs[0].open_time,cs[-1].close_time)
        self.assertEqual(account["liquidations"],1)
        self.assertLess(account["return_pct"],0)
        self.assertGreater(account["max_drawdown_pct"],0)

    def test_future_change_cannot_change_earlier_equity(self):
        cs=bars(count=10)
        row=dict(id="a",symbol="BTCUSDT",timeframe="5m",direction="long",entry_time=cs[1].open_time,exit_time=None)
        opts=WorkbenchRequest(start="2026-09-01",end="2026-09-02",initial_capital=1000,margin_per_trade=100).model_dump()
        before=simulate([dict(row)],{("BTCUSDT","5m"):cs},opts,cs[0].open_time,cs[-1].close_time)
        changed=cs[:6]+[replace(c,open=c.open*2,close=c.close*2,high=c.high*2,low=c.low*2) for c in cs[6:]]
        after=simulate([dict(row)],{("BTCUSDT","5m"):changed},opts,cs[0].open_time,cs[-1].close_time)
        self.assertEqual([p for p in before["curve"] if p["time"]<cs[6].open_time],[p for p in after["curve"] if p["time"]<cs[6].open_time])
