"""Shared signal-row format. No complete trading strategies are installed."""
CATALOG = []
STRATEGY_INDICATORS = {}
ORDER_RULES = {}

def order_rule(strategy):
    raise ValueError("该策略已移除，请在指标策略中选择 WaveTrend 超买超卖交叉")

def display_signal_rows(rows, strategy):
    if strategy: order_rule(strategy)
    return rows

def signal_rows(signals, strategy=None):
    if strategy: order_rule(strategy)
    return [dict(id=s['id']+':'+s['signal_kind'], symbol=s['symbol'], timeframe=s['timeframe'],
        signal_time=s['close_time'], signal_price=s['price'], direction=s['direction'],
        signal_kind=s['signal_kind'], action=s.get('action'), rule_id=s.get('rule_id'), pattern=s['pattern'], indicator_results=s['indicator_results'],
        entry_time=None, entry_price=None, exit_time=None, exit_price=None,
        net_return_pct=None, best_move_pct=None, worst_move_pct=None,
        status='收盘确认条件 · 非成交记录 · 未下单', action_basis='指标触发条件', source_order_rule=None)
        for s in signals]

def reversal_outcomes(signals, candles, options):
    order_rule(options.get('strategy'))
