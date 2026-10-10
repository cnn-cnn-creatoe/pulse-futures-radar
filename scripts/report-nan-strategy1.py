"""Readable, local report from the frozen backtest outputs."""
import json,html,statistics
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[1];folder=ROOT/'outputs/nan-strategy1-btc-3y'
job=json.loads((folder/'result.json').read_text('utf-8'));p=job['portfolio'];rows=job['items'];comps=json.loads((folder/'comparisons.json').read_text('utf-8'))
tz=timezone(timedelta(hours=8))
def dt(t):return datetime.fromtimestamp(t/1000,tz).strftime('%Y-%m-%d %H:%M:%S')
def money(v):return '≈ 0.00' if 0<v<.005 else f'{v:,.2f}'
annual={};balance=1000;crossings={};loss_run=max_loss_run=0
for r in sorted(rows,key=lambda r:r['account_exit_time'] or 10**20):
    pnl=r.get('account_pnl')
    if pnl is None:continue
    year=dt(r['account_exit_time'])[:4]
    group=annual.setdefault(year,dict(trades=0,wins=0,pnl=0))
    group['trades']+=1;group['wins']+=pnl>0;group['pnl']+=pnl
    balance+=pnl
    # Derive balance independently via multiplicative returns to avoid cancellation near zero.
    balance=r['margin']*(1+3*.0005)+pnl
    group['ending']=balance
    for threshold in (500,100,10,1):
        if balance<threshold and threshold not in crossings:crossings[threshold]=dt(r['account_exit_time'])
    loss_run=loss_run+1 if pnl<0 else 0;max_loss_run=max(max_loss_run,loss_run)
hold=[(r['exit_time']-r['entry_time'])/60000 for r in rows if r.get('exit_time') is not None]
comparison_rows='\n'.join(f"| {r['label']} | {money(r['portfolio']['final_equity'])} | {r['portfolio']['return_pct']:.2f}% | {100*r['portfolio']['win_rate']:.2f}% | {r['portfolio']['max_drawdown_pct']:.2f}% |" for r in comps)
annual_rows='\n'.join(f"| {year} | {g['trades']:,} | {100*g['wins']/g['trades']:.2f}% | {g['pnl']:.4f} | {money(g['ending'])} |" for year,g in annual.items())
markdown=f"""# 楠策略1超买超卖 · BTC 三年回测

**结论：这组历史样本中，策略不扣成本仍亏损；扣除设定的手续费和滑点后，本金几乎全部损失。没有为改善结果调整原代码条件或参数。**

## 回测口径

- 市场：币安 BTCUSDT U 本位永续，5 分钟 K 线。
- 区间：北京时间 2023-09-14 00:00:00 至 2026-09-14 00:00:00（右端不包含），三个完整日历年。
- 数据：315,648 根区间内 K 线，另有 2,020 根预热，无缺失、无内部断档。2023-09 至 2026-08 使用币安官方月度存档及 SHA256 校验，尾段使用本机已保存的已收盘行情。
- 本金 1,000 USDT，3 倍杠杆，每次全部可用权益复利，预留入场手续费；单币单仓，不加仓。
- 主结果按信号后的下一根开盘模拟成交；单边手续费 0.05%，单边不利滑点 0.03%。这些是原脚本未规定的回测假设，非脚本自带参数。
- 原代码是 indicator，不是下单 strategy。复现其历史收盘保留信号；不能从历史 5 分钟 OHLC 恢复消失的盘中预览和首次盘中触发价。
- 不启用活跃度、可做度、形态等额外入场筛选；没有额外止盈、止损或固定持仓周期。

## 主结果

| 项目 | 结果 |
|---|---:|
| 初始资金 | 1,000.00 USDT |
| 最终权益 | {money(p['final_equity'])} USDT |
| 净损益 | {p['net_profit']:,.2f} USDT |
| 总收益率 | 约 -100.00% |
| 最大回撤（开盘/收盘估值） | 约 100.00% |
| 已平仓交易 | {p['closed']:,} |
| 净盈利交易胜率 | {p['win_rate']*100:.2f}% |
| 盈利因子（实际资金盈亏之比） | {p['profit_factor']:.4f} |
| 手续费合计 | {p['fees']:.2f} USDT |
| 期末未平仓 / 跳过 / 近似强平 | {p['open']} / {p['skipped']} / {p['liquidations']} |
| 平均 / 中位持仓时间 | {statistics.mean(hold):.2f} / {statistics.median(hold):.2f} 分钟 |
| 最长连续净亏损 | {max_loss_run} 笔 |

数学模型最终权益为 {p['final_equity']:.8e} USDT，显示四舍五入为 0，不代表某一笔强平归零。模型允许无限细分数量；真实账户会更早受最小下单数量、名义金额等约束而无法继续。不能把后段极微小金额交易当作实际可成交订单。

## 不改信号的成本对照

全部采用 1,000 USDT、3 倍权益复利和同一批开平信号，没有重新优化参数。

| 成交及成本口径 | 期末权益 USDT | 收益率 | 净胜率 | 最大回撤 |
|---|---:|---:|---:|---:|
{comparison_rows}

按代码横线的当根收盘参考价且不扣任何费用，最终为 {comps[-1]['portfolio']['final_equity']:.2f} USDT。主结果的重大恶化主要来自频繁交易叠加双边成本；零成本对照也没有显示正收益。

## 分年记录

2023 年和 2026 年为区间内部分年份；此表按平仓年份统计已实现净盈亏，期末数值未加入跨年持仓的未实现变化。

| 年份 | 平仓笔数 | 净胜率 | 已实现盈亏 USDT | 年内末次平仓后余额 USDT |
|---|---:|---:|---:|---:|
{annual_rows}

## 原代码适配核对

1. HLC3，EMA 通道 10，EMA 平均 21，WT2 为 SMA4；默认阈值 ±53。
2. 做多：上一根 WT1 为下拐极值、位于 ≤ -53 且低于当时 WT2，当前 WT1 抬高；做空对称。没有改成金叉/死叉入场。
3. 已持多遇向下转折平多，已持空遇向上转折平空；不要求到达另一侧区域。
4. 同根先平仓，再以完整条件判断反手；区域去重按代码默认关闭；不重复加仓。
5. 信号落在当前检测 K 线，不回填前一根极值；ATR 仅用于原图展示，未加入交易过滤。
6. 全样本另用独立逐根标量算法核对：317,668 根含预热 K 线，19,166 个含预热开平动作逐一一致。该核对不是宣称经过 TradingView 引擎导出对比。
7. 469 项后端回归通过；新规则已加入研究库、源码选择、设置、图表及 TG 目录。未替用户开启新 TG 订阅或发送测试消息。

## 复现和边界

未计资金费率、真实标记价格、历史保证金档位、数量精度与成交深度；近似强平检查不等于交易所强平回放。原代码未规定账户规则，资金结果依赖上述公开假设。

研究记录 ID：`{job['id']}`。在「历史回测 → 每日 CSV / 研究记录」打开「楠策略1超买超卖」即可查看保存结果。

同目录包含 `trades.csv`（完整逐笔明细）、`result.json`（冻结设置与源码快照）、`comparisons.json`（成本对照）、`parity.json`（独立核对）和 `original.pine`（用户提供原文）。复现脚本位于 `scripts/backtest-nan-strategy1.py`。

数据规则参考：[币安官方历史数据说明](https://github.com/binance/binance-public-data)。历史与实时执行差异：[TradingView 执行模型](https://www.tradingview.com/pine-script-docs/language/execution-model/)。
"""
(folder/'回测报告.md').write_text(markdown,'utf-8')
# A self-contained HTML report, with inline SVG equity and no external assets.
curve=p['curve'];t0=curve[0]['time'];t1=curve[-1]['time'];maximum=max(v['equity'] for v in curve)
points=' '.join(f"{50+900*(v['time']-t0)/(t1-t0):.1f},{260-220*v['equity']/maximum:.1f}" for v in curve)
svg=f'<svg viewBox="0 0 1000 300" role="img" aria-label="扣费后权益曲线"><path d="M50 30V260H950" fill="none" stroke="#bcc8ce"/><polyline points="{points}" fill="none" stroke="#bf4755" stroke-width="2.5"/><text x="50" y="285">2023-09-14</text><text x="840" y="285">2026-09-14</text><text x="5" y="36">{maximum:.0f}</text><text x="20" y="263">0</text></svg>'
# Small Markdown renderer for this generated report's headings, tables, paragraphs and lists.
import re
parts=[];table=False
for line in markdown.splitlines():
 if line.startswith('|'):
  if not table:parts.append('<table>');table=True
  if re.fullmatch(r'[| :\-]+',line):continue
  parts.append('<tr>'+''.join('<td>'+html.escape(c.strip())+'</td>' for c in line.strip('|').split('|'))+'</tr>');continue
 if table:parts.append('</table>');table=False
 if not line:continue
 if line.startswith('# '):parts.append('<h1>'+html.escape(line[2:])+'</h1>'+svg)
 elif line.startswith('## '):parts.append('<h2>'+html.escape(line[3:])+'</h2>')
 else:parts.append('<p>'+html.escape(line).replace('**','').replace('`','')+'</p>')
page='<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>楠策略1 · BTC三年回测</title><style>body{background:#edf1f2;color:#23313a;font:16px/1.7 system-ui,"Microsoft YaHei",sans-serif;margin:0}main{max-width:1060px;margin:32px auto;background:white;padding:44px;border-radius:18px}h1{font-size:30px}h2{margin-top:36px;border-top:1px solid #ddd;padding-top:22px}table{border-collapse:collapse;width:100%;font-size:14px}td{padding:9px;border-bottom:1px solid #ddd}tr:first-child{background:#e7eef0;font-weight:bold}svg{width:100%;height:auto;font:13px system-ui}p{overflow-wrap:anywhere}@media(max-width:650px){main{padding:18px;margin:0}td{padding:6px}h1{font-size:24px}}</style><main>'+''.join(parts)+'</main></html>'
(folder/'回测报告.html').write_text(page,'utf-8')
print('REPORT_READY',job['id'])
print('CAPITAL_THRESHOLDS',crossings)
