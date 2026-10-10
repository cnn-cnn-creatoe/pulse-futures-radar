import csv, json, sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / 'research/smc-long-history-2026-10-09/交易人工复核表.html'
rows = list(csv.DictReader((out.parent / 'trades.csv').open(encoding='utf-8-sig')))
# Preserve the long-history sample; add the two expressly requested reference symbols.
extra = list(csv.DictReader((root / 'research/smc-entry-exit-2026-10-09/trades.csv').open(encoding='utf-8-sig')))
rows += [r for r in extra if r['symbol'] in ('NVDAUSDT', 'MUUSDT')]
five_minute = '--five-minute' in sys.argv
if five_minute:
    out = root / 'research/smc-5m-review-2026-10-09/五分钟复核交易表.html'
    rows = json.loads((out.parent / 'replayed-trades.json').read_text(encoding='utf-8'))
for r in rows:
    for k in ('time', 'exit_time', 'entry', 'exitprice', 'stop', 'target', 'gross_bps', 'net_bps'):
        r[k] = float(r[k])
    r['id'] = '|'.join(str(r[k]) for k in ('symbol','tf','mode','exit','filter','side','time'))
page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>交易人工复核表</title>
<style>body{margin:24px;background:#101c27;color:#dbe8f3;font:15px system-ui}h1{font-size:25px}p{color:#a4bbcf;line-height:1.7}button,select,input{background:#1a3041;color:#e6f2fc;border:1px solid #426075;border-radius:7px;padding:9px;margin:4px}button{cursor:pointer}table{border-collapse:collapse;width:100%;white-space:nowrap;font-size:13px}th,td{padding:12px 9px;border-bottom:1px solid #304658;text-align:left}th{background:#203648;position:sticky;top:0}.up{color:#54d4ae}.down{color:#ff929c}.wrap{overflow:auto;max-height:70vh}small{display:block;color:#a4bbcf;margin-top:5px}.controls{position:relative}details{white-space:normal;min-width:180px}summary{cursor:pointer}#count{margin:12px 0}a{color:#64cfdb}</style>
<h1>交易人工复核表</h1><p>模拟回测成交，不是实盘订单。涵盖8个长历史品种，并补充英伟达 NVDA、美光 MU。原始交易按入场方法、退出方法和趋势过滤分别模拟，同一次行情可能出现多行；不可直接相加作为账户收益。</p>
<p>时间均为北京时间：入场时间为所在K线开盘时间，出场时间为所在K线结束时间，盘中实际成交时刻未知。做多先买后卖；做空先卖后买。价格涨跌幅按出场价÷入场价−1；方向收益考虑多空，净收益另扣往返0.20%成本，不含资金费。新下载的5分钟数据尚未用于本表复核。</p>
<div class="controls"><input id="q" placeholder="搜索币种，例如 NVDA" aria-label="搜索币种"><select id="tf"><option value="">所有周期</option><option>1h</option><option>4h</option><option>1d</option></select><select id="mode"><option value="">所有入场类别</option><option value="touch">首次触区</option><option value="confirm">触区后收盘确认</option><option value="retest">突破回踩确认</option></select><select id="side"><option value="">多空全部</option><option value="long">做多</option><option value="short">做空</option></select><select id="exit"><option value="2R">退出：2R止盈</option><option value="opposing">退出：反向结构位</option><option value="">所有退出方案</option></select><select id="filter"><option value="none">趋势过滤：无</option><option value="supertrend">趋势过滤：Supertrend</option><option value="">所有过滤方案</option></select><select id="split"><option value="test">后段验证样本</option><option value="train">前段研究样本</option><option value="purged">跨边界剔除样本</option><option value="">全部样本</option></select><button onclick="window.print()">打印</button></div>
<div id="count"></div><button id="prev">上一页</button><button id="next">下一页</button><div class="wrap"><table><thead><tr><th>类别 / 方向</th><th>入场K线时间</th><th>出场K线结束时间</th><th>币种 / 周期</th><th>买入价</th><th>卖出价</th><th>价格涨跌幅</th><th>方向收益 / 净收益</th><th>退出原因</th><th>核对详情</th></tr></thead><tbody id="body"></tbody></table></div>
<p>默认只显示后段、2R退出、无趋势过滤，避免把不同退出方案混在一起。后段样本已被多轮研究查看，不代表全新的独立验证。可选择单一入场类别逐笔对照图表。买入价和卖出价为模拟执行价；点击币种复制合约代码。</p>
<script>const data=__DATA__;let page=0;const keys=['q','tf','mode','side','exit','filter','split'];const names={touch:'首次触区',confirm:'触区后收盘确认',retest:'突破回踩确认',stop:'止损',target:'止盈',gap_stop:'跳空止损',gap_target:'跳空止盈',timeout:'超时退出',both:'同根双触发：止损优先'};
const num=n=>Number(n).toLocaleString('en-US',{maximumFractionDigits:8});const pct=n=>`<span class="${n>=0?'up':'down'}">${n>=0?'+':''}${n.toFixed(3)}%</span>`;const dt=n=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(n));
function render(){let a=data.filter(r=>keys.every(k=>{let v=document.getElementById(k).value;return !v||(k==='q'?r.symbol.includes(v.trim().toUpperCase()):r[k]===v)})).sort((a,b)=>b.time-a.time);let pages=Math.max(1,Math.ceil(a.length/50));page=Math.min(page,pages-1);document.getElementById('count').textContent=`匹配 ${a.length} 笔方案记录 / 总计 ${data.length} 笔；第 ${page+1} / ${pages} 页，每页50笔`;document.getElementById('prev').disabled=page===0;document.getElementById('next').disabled=page+1===pages;document.getElementById('body').innerHTML=a.slice(page*50,page*50+50).map(r=>{let long=r.side==='long';return `<tr><td>${names[r.mode]}<small>${long?'做多 · 先买后卖':'做空 · 先卖后买'}</small></td><td>${dt(r.time)}</td><td>${dt(r.exit_time)}</td><td><button onclick="copySymbol(this,'${r.symbol}')">${r.symbol}</button><small>${r.tf}</small></td><td>${num(long?r.entry:r.exitprice)}<small>${long?'开仓':'平仓'}</small></td><td>${num(long?r.exitprice:r.entry)}<small>${long?'平仓':'开仓'}</small></td><td>${pct((r.exitprice/r.entry-1)*100)}</td><td>${pct(r.gross_bps/100)}<small>净：${pct(r.net_bps/100)}</small></td><td>${names[r.reason]||r.reason}<small>持有 ${r.holding} 根K线</small></td><td><details><summary>展开</summary>入场 ${num(r.entry)}<br>出场 ${num(r.exitprice)}<br>止损 ${num(r.stop)}<br>目标 ${num(r.target)}<br>退出 ${r.exit}<br>趋势过滤 ${r.filter}<br>同根双触发 ${r.ambiguous==='True'?'是':'否'}</details></td></tr>`}).join('')||'<tr><td colspan="10">当前筛选无记录</td></tr>';}
async function copySymbol(b,s){try{await navigator.clipboard.writeText(s);b.textContent='已复制';setTimeout(()=>b.textContent=s,900)}catch{prompt('复制合约代码',s)}}
keys.forEach(k=>document.getElementById(k).addEventListener(k==='q'?'input':'change',()=>{page=0;render()}));document.getElementById('prev').onclick=()=>{page--;render()};document.getElementById('next').onclick=()=>{page++;render()};render();</script></html>'''
if five_minute:
    page = page.replace('交易人工复核表', '5分钟复核 · 交易人工复核表')
    page = page.replace('时间均为北京时间：入场时间为所在K线开盘时间，出场时间为所在K线结束时间，盘中实际成交时刻未知。', '时间均为北京时间：入场时间为成交所在5分钟K线开盘时间，出场时间为退出所在5分钟K线结束时间，具体成交秒数未知。信号仍按原1小时、4小时和日线规则生成；5分钟数据只用于模拟执行。')
    page = page.replace('新下载的5分钟数据尚未用于本表复核。', '已使用补齐的5分钟数据重新模拟；只纳入完整覆盖12根原周期K线观察窗口的交易。5分钟内双触发仍按止损优先，首次触区成交K线内不能确认在成交后到达的目标按保守规则处理。不是逐笔成交数据。')
    page = page.replace('入场K线时间</th>', '入场5分钟K线时间</th>').replace('出场K线结束时间</th>', '出场5分钟K线结束时间</th>')
    page = page.replace("r.ambiguous==='True'", "(r.ambiguous===true||r.ambiguous==='True')")
    page = page.replace('同根双触发：止损优先', '5分钟内双触发：止损优先')
page = page.replace('<button onclick="window.print()">', '<label>排序：<select id="sort"><option value="time_desc">入场时间：新→旧</option><option value="time_asc">入场时间：旧→新</option><option value="exit_desc">出场时间：新→旧</option><option value="exit_asc">出场时间：旧→新</option><option value="move_desc">价格涨跌幅：大→小</option><option value="move_asc">价格涨跌幅：小→大</option><option value="net_desc">净收益：大→小</option><option value="net_asc">净收益：小→大</option></select></label><button onclick="window.print()">')
page = page.replace('.sort((a,b)=>b.time-a.time)', '.sort(compareRows)')
page = page.replace('<div id="count"></div>', '<button onclick="toggleSort(\'time\')">入场时间 ↕</button><button onclick="toggleSort(\'exit\')">出场时间 ↕</button><button onclick="toggleSort(\'move\')">涨跌幅 ↕</button><button onclick="toggleSort(\'net\')">净收益 ↕</button><div id="count"></div>')
page = page.replace('function render(){', "function toggleSort(field){const s=document.getElementById('sort');s.value=field+(s.value===field+'_desc'?'_asc':'_desc');page=0;render()}\nfunction render(){")
page = page.replace("minute:'2-digit'", "minute:'2-digit',second:'2-digit'")
page = page.replace('function render(){', "function compareRows(a,b){const [field,direction]=document.getElementById('sort').value.split('_');const value=r=>field==='time'?r.time:field==='exit'?r.exit_time:field==='move'?(r.exitprice/r.entry-1):Number(r.net_bps);return (direction==='asc'?1:-1)*(value(a)-value(b))||a.symbol.localeCompare(b.symbol)||a.id.localeCompare(b.id)}\nfunction render(){")
page = page.replace("keys.forEach(k=>", "document.getElementById('sort').onchange=()=>{page=0;render()};keys.forEach(k=>")
out.write_text(page.replace('__DATA__', json.dumps(rows,ensure_ascii=False)),encoding='utf-8')
print(json.dumps({'path':str(out),'rows':len(rows),'symbols':sorted(set(r['symbol'] for r in rows))},ensure_ascii=False))
