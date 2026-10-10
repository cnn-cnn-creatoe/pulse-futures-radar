import json,sys
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=root/'research/smc-forward-five-2026-10-09'
annual='--annual' in sys.argv
if annual:out=root/'research/smc-year-2026-10-08'
events=json.loads((out/'events.json').read_text(encoding='utf-8'))
results=json.loads((out/'results.json').read_text(encoding='utf-8'))
html='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>入场后五根K线 · 盈亏背景研究</title>
<style>body{background:#101c27;color:#e0ebf4;font:15px system-ui;margin:26px}h1{font-size:26px}h2{font-size:20px;margin-top:28px}p{line-height:1.8;color:#b3c7d7;max-width:1300px}button,select,input{background:#1b3244;color:#e0ebf4;border:1px solid #425e70;border-radius:7px;padding:9px;margin:4px}button,summary{cursor:pointer}table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}th,td{padding:12px;border-bottom:1px solid #344958;text-align:left}th{background:#213747;position:sticky;top:0}small{display:block;color:#a5bbcc;margin-top:5px}.good{color:#67d6ac}.bad{color:#ff99a0}.box{background:#182b3a;padding:15px;border-radius:9px;margin:14px 0}.scroll{overflow:auto;max-height:65vh}.note{color:#f1c77d}summary{color:#8cc7e9}details{max-width:350px;white-space:normal}</style>
<h1>入场后第1—5根K线表现</h1>
<p>这次只研究入场方向，不设置止盈、止损或出场方式。原1h、4h、1d信号保持一致，使用补齐的5分钟行情核对触区入场及后续收盘价。第1根指入场所在K线之后的第一根完整原周期K线：4h信号看后面5根4h线，日线信号看后面5根日线。所有涨跌幅均相对同一个入场价累计计算，不是逐根环比。</p>
<div class="box">共307条信号观察，去除旧表不同止盈和过滤方案的重复行。可能存在同品种相邻信号重叠，因此不是307次独立交易，也不是账户收益。<br>重点复核上一轮组合：4h＋首次触区＋Supertrend同向，共26条。重新按时间划分：前段14条、后段8条、跨分界4条剔除。旧数据已被查看，此处后段仍为探索性验证。</div>
<h2>盈利与亏损，差别在哪里？</h2>
<p>按第3根K线的方向收益扣0.20%成本后是否为正分组。以下是相关特征对比，不能证明因果，也不能把未来几根的走势当成入场时已知条件。</p>
<div class="scroll"><table><thead><tr><th>分组</th><th>笔数</th><th>第1根</th><th>第2根</th><th>第3根</th><th>第4根</th><th>第5根</th><th>前根相对成交额</th><th>前根振幅</th></tr></thead><tbody id="pl"></tbody></table></div>
<p>盈利样本平均能维持顺向走势；亏损样本平均在前1根已逆向，之后继续恶化。但这属于事后走势描述。成交量在前段区分明显，后段盈利组的平均量反而低于亏损组，不能简单认为“越放量越好”。</p>
<h2>固定条件对照：哪些值得继续观察？</h2>
<div id="comparison"></div>
<p class="note">结论：暂不启用新的自动过滤。相对成交额≥0.8倍在前后段都有改善迹象，但仅7条开发、5条验证，未达到预设12/8条最低门槛。避开连续3根反向K线没有稳定收益改善；振幅和空间过滤前后段效果不一致。以下筛选可用于人工检查，不代表已经证实的优化策略。</p>
<h2>逐笔核对</h2>
<input id="q" placeholder="搜索合约，例如 NVDA"><select id="tf"><option>4h</option><option>1h</option><option>1d</option><option value="">所有周期</option></select><select id="mode"><option value="touch">首次触区</option><option value="confirm">触区后收盘确认</option><option value="retest">突破回踩确认</option><option value="">所有类别</option></select><select id="align"><option value="yes">Supertrend同向</option><option value="">所有趋势背景</option><option value="no">Supertrend不同向</option></select><select id="side"><option value="">多空全部</option><option value="long">做多</option><option value="short">做空</option></select><select id="split"><option value="">全部时段</option><option value="train">前段开发</option><option value="test">后段验证</option><option value="purged">跨边界剔除</option></select>
<select id="filter"><option value="baseline">不加辅助过滤</option><option value="volume">相对成交额≥0.8倍（待验证）</option><option value="streak">无连续3根反向K线</option><option value="range">前根振幅≤2 ATR</option><option value="room">反向结构空间≥1 ATR</option></select>
<select id="metric"><option value="price">显示：价格涨跌幅</option><option value="directional">显示：多空方向收益</option><option value="net">显示：方向收益扣0.20%</option></select><select id="sort"><option value="time_desc">时间：新→旧</option><option value="time_asc">时间：旧→新</option></select>
<div><button onclick="toggle('time')">时间 ↕</button><button onclick="toggle('0')">第1根 ↕</button><button onclick="toggle('1')">第2根 ↕</button><button onclick="toggle('2')">第3根 ↕</button><button onclick="toggle('3')">第4根 ↕</button><button onclick="toggle('4')">第5根 ↕</button></div>
<div id="totals" class="box"></div><p id="count"></p><button id="prev">上一页</button><button id="next">下一页</button>
<div class="scroll"><table><thead><tr><th>入场5分钟K线时间</th><th>合约 / 周期</th><th>类别 / 方向</th><th>入场参考价</th><th>后1根</th><th>后2根</th><th>后3根</th><th>后4根</th><th>后5根</th><th>观察结果 / 入场背景</th></tr></thead><tbody id="rows"></tbody></table></div>
<p>默认价格涨跌幅：正数代表价格上涨，做空时下跌才对方向有利。可切换方向收益与扣成本口径。时间均为北京时间，入场只精确到5分钟区间；后续时间为原周期收盘时刻。未计资金费，未假设杠杆。低成交量、走势跳跃等仍可能使实际成交与模拟不同。</p>
<script>const data=__EVENTS__,result=__RESULTS__;let page=0;const names={touch:'首次触区',confirm:'收盘确认',retest:'突破回踩'},splits={train:'前段',test:'后段',purged:'跨界剔除'};
const pct=n=>`<span class="${n>=0?'good':'bad'}">${n>=0?'+':''}${n.toFixed(3)}%</span>`,num=n=>n==null?'未知':Number(n).toLocaleString('en-US',{maximumFractionDigits:5}),dt=n=>new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'}).format(new Date(n));
document.getElementById('pl').innerHTML=result.profitloss.filter(r=>r.split==='all').map(r=>`<tr><td>${r.label}（第3根分组）</td><td>${r.n}</td>${r.mean.map(v=>`<td>${pct(v)}</td>`).join('')}<td>${r.volume_ratio.toFixed(2)}倍</td><td>${r.range_atr.toFixed(2)} ATR</td></tr>`).join('');
function block(s){return !s.n?'无样本':`${s.n}条 · 胜率${s.win[2].toFixed(1)}% · 均值${pct(s.mean[2])}`}
document.getElementById('comparison').innerHTML='<table><thead><tr><th>条件（第3根、扣成本）</th><th>前段开发</th><th>后段验证</th></tr></thead><tbody>'+result.summary.map(s=>`<tr><td>${s.label}</td><td>${block(s.train)}</td><td>${block(s.test)}</td></tr>`).join('')+'</tbody></table>';
for(let h=0;h<5;h++)for(const d of ['desc','asc']){let o=document.createElement('option');o.value=h+'_'+d;o.textContent=`第${h+1}根：${d==='desc'?'大→小':'小→大'}`;document.getElementById('sort').append(o)}
function pass(r,key){return key==='baseline'||key==='volume'&&r.volume_ratio!=null&&r.volume_ratio>=.8||key==='streak'&&!r.counter_streak||key==='range'&&r.range_atr<=2||key==='room'&&r.room_atr!=null&&r.room_atr>=1}
function filtered(){const v=k=>document.getElementById(k).value;return data.filter(r=>r.symbol.includes(v('q').trim().toUpperCase())&&(!v('tf')||r.tf===v('tf'))&&(!v('mode')||r.mode===v('mode'))&&(!v('side')||r.side===v('side'))&&(!v('split')||r.split===v('split'))&&(!v('align')||r.aligned===(v('align')==='yes'))&&pass(r,v('filter')))}
function compare(a,b){const [k,d]=document.getElementById('sort').value.split('_'),metric=document.getElementById('metric').value;return (d==='asc'?1:-1)*((k==='time'?a.time:a[metric][+k])-(k==='time'?b.time:b[metric][+k]))||a.id-b.id}
function render(){let a=filtered().sort(compare),metric=document.getElementById('metric').value;let pages=Math.max(1,Math.ceil(a.length/40));page=Math.min(page,pages-1);document.getElementById('count').textContent=`${a.length}条观察；第${page+1}/${pages}页。`;document.getElementById('prev').disabled=page===0;document.getElementById('next').disabled=page+1===pages;
document.getElementById('totals').innerHTML=a.length?'当前筛选平均值（随显示口径变化）：'+[0,1,2,3,4].map(h=>`第${h+1}根 ${pct(a.reduce((s,r)=>s+r[metric][h],0)/a.length)}`).join('　'):'当前筛选无样本';
document.getElementById('rows').innerHTML=a.slice(page*40,page*40+40).map(r=>{let pattern=r.directional.every(v=>v>0)?'五根均顺向':r.directional.every(v=>v<0)?'五根均逆向':r.directional[0]>0&&r.directional[2]<0?'前期顺向，后转逆向':'多空反复';return `<tr><td>${dt(r.time)}<small>${splits[r.split]}</small></td><td><button onclick="copy(this,'${r.symbol}')">${r.symbol}</button><small>${r.tf}</small></td><td>${names[r.mode]}<small>${r.side==='long'?'做多':'做空'} · ${r.aligned?'顺趋势':'不同向'}</small></td><td>${num(r.entry)}</td>${r[metric].map((v,h)=>`<td>${pct(v)}<small>${num(r.future_prices[h])}</small><small>${dt(r.future_times[h])}</small></td>`).join('')}<td><details><summary>${pattern}（事后描述）</summary>前根相对成交额：${num(r.volume_ratio)}倍<br>前根振幅：${num(r.range_atr)} ATR<br>前序连续${r.streak}根${r.streak_side===1?'阳线':r.streak_side===-1?'阴线':'平线'}<br>反向结构空间：${num(r.room_atr)} ATR<br>背景截至 ${dt(r.context_time)}<br>这些是入场时可见背景，不能单独证明盈亏原因。</details></td></tr>`}).join('')}
function toggle(k){let s=document.getElementById('sort');s.value=k+(s.value===k+'_desc'?'_asc':'_desc');page=0;render()}
async function copy(b,s){try{await navigator.clipboard.writeText(s);b.textContent='已复制';setTimeout(()=>b.textContent=s,800)}catch{prompt('复制合约',s)}}
['q','tf','mode','align','side','split','filter','metric','sort'].forEach(k=>document.getElementById(k).addEventListener(k==='q'?'input':'change',()=>{page=0;render()}));document.getElementById('prev').onclick=()=>{page--;render()};document.getElementById('next').onclick=()=>{page++;render()};render();</script></html>'''
if annual:
 from datetime import datetime,timezone,timedelta
 tz=timezone(timedelta(hours=8))
 def date(t):return datetime.fromtimestamp(t/1000,tz).strftime('%Y-%m-%d') if t else '无数据'
 def pct(v):return f'{v:+.3f}%'
 base=results['summary'][0]
 volume=next(r for r in results['summary'] if r['key']=='volume')
 interpretation=f"<div class='box'><strong>长历史结论：此前短期样本的优势没有稳定延续。</strong><p>4h首次触区＋顺趋势：前段第3根平均净方向收益 {pct(base['train']['mean'][2])}，后段 {pct(base['test']['mean'][2])}。加入成交额≥0.8倍：前段 {pct(volume['train']['mean'][2])}，后段 {pct(volume['test']['mean'][2])}；后段减亏，但前段未改善，不能视为已验证的优势。</p><p>1小时各测试组合后段平均净方向收益均为负。4小时突破回踩确认较值得继续研究，但第3根中位数仍为负、月份间表现不一致。日线只有22条原始信号，预热后时间很短，不能据少数高收益判断稳定。</p></div>"
 html=html.replace('<h1>入场后第1—5根K线表现</h1>','<h1>2026年历史研究 · 入场后第1—5根K线</h1>'+interpretation)
 html=html.replace('共307条信号观察',f'共{len(events)}条信号观察')
 html=html.replace('共26条。重新按时间划分：前段14条、后段8条、跨分界4条剔除。旧数据已被查看，此处后段仍为探索性验证。',f"共{base['all']['n']}条。固定7月1日为分界：前段{base['train']['n']}条、后段{base['test']['n']}条，其余跨边界剔除。部分后段数据在此前研究看过，仍为探索性验证。")
 html=html.replace('盈利样本平均能维持顺向走势；亏损样本平均在前1根已逆向，之后继续恶化。但这属于事后走势描述。成交量在前段区分明显，后段盈利组的平均量反而低于亏损组，不能简单认为“越放量越好”。','盈利与亏损按第3根结果事后分组；表中路径仅用于复盘，不能将未来走势作为入场条件。量价背景与结果相关不等于因果，需结合下面的时间分段与逐月对照。')
 start=html.index('<p class="note">结论：');end=html.index('</p>',start)+4
 html=html[:start]+'<p class="note">研究限制：这10个当前可用合约存在存续品种筛选偏差；不代表全部美股。事件可能重叠，不将逐笔收益相加。固定测试成交额≥0.8倍，未反复优化阈值。后段不是全新未见数据。原交易策略与WT代码保持不变；所有新增条件仅用于研究筛选。</p>'+html[end:]
 coverage=json.loads((out/'download-summary.json').read_text())
 rows=''
 for s in sorted({r['symbol'] for r in coverage}):
  records={r['tf']:r for r in coverage if r['symbol']==s};five=records.get('5m',{})
  rows+=f"<tr><td>{s}</td><td>{date(five.get('first'))}</td><td>{date(five.get('last'))}</td><td>{five.get('n',0):,}</td><td>{records.get('1d',{}).get('n',0)}</td><td>{sum(r.get('gaps',0) for r in records.values())}</td></tr>"
 sections='<h2>2026年1月1日至10月8日：实际数据覆盖</h2><p>使用同一组10个代表性美股代币合约。申请完整区间，但上市前没有合约行情，不以股票或指数行情补齐。原周期使用原生K线，5分钟数据核对入场与后续收盘。日线必须完成200根预热后才有信号，不能把预热期间当成有效回测。截止北京时间10月8日23:59:59，未收盘K线不纳入。</p><div class="scroll"><table><thead><tr><th>合约</th><th>5分钟数据起日</th><th>止日</th><th>5分钟根数</th><th>已收盘日线数</th><th>缺口段数</th></tr></thead><tbody>'+rows+'</tbody></table></div>'
 names={'touch':'首次触区','confirm':'收盘确认','retest':'突破回踩'};filters={'none':'无趋势过滤','trend':'顺趋势','trend_volume':'顺趋势＋成交额≥0.8倍'}
 sections+='<h2>各周期比较（7月1日后，方向收益扣0.20%）</h2><p>每个单元格为平均净方向收益；胜率统一按第3根净收益是否为正。1h、4h、1d对应不同观察时长，不能只凭一个平均数判断谁最稳。</p><div class="scroll"><table><thead><tr><th>周期</th><th>类别</th><th>条件</th><th>信号数</th><th>第3根胜率</th>'+''.join(f'<th>第{h}根</th>' for h in range(1,6))+'</tr></thead><tbody>'
 for r in results['comparisons']:
  if r['split']!='test':continue
  sections+=f"<tr><td>{r['tf']}</td><td>{names[r['mode']]}</td><td>{filters[r['filter']]}</td><td>{r['n']}</td>"+(f"<td>{r['win'][2]:.1f}%</td>"+''.join(f'<td>{pct(v)}</td>' for v in r['mean']) if r['n'] else '<td colspan="6">无成熟信号</td>')+'</tr>'
 sections+='</tbody></table></div><h2>重点组合逐月表现（第3根，扣成本）</h2><div class="scroll"><table><thead><tr><th>月份</th><th>条件</th><th>信号数</th><th>胜率</th><th>平均</th><th>中位数</th></tr></thead><tbody>'
 for r in results['monthly']:
  if r['n']:sections+=f"<tr><td>{r['month']}</td><td>{'原组合' if r['filter']=='baseline' else '加成交额≥0.8倍'}</td><td>{r['n']}</td><td>{r['win'][2]:.1f}%</td><td>{pct(r['mean'][2])}</td><td>{pct(r['median'][2])}</td></tr>"
 sections+='</tbody></table></div><h2>稳定性检查（7月后，第3根，扣成本）</h2><div class="scroll"><table><thead><tr><th>条件</th><th>按周分组重抽样95%区间</th><th>去掉最佳一笔后的平均</th><th>去重叠后笔数 / 平均</th><th>做多笔数 / 平均</th><th>做空笔数 / 平均</th></tr></thead><tbody>'
 for r in results.get('diagnostics',[]):
  def cell(s):return f"{s['n']} / {pct(s['mean'][2])}" if s['n'] else '无样本'
  sections+=f"<tr><td>{'原组合' if r['filter']=='baseline' else '加成交额≥0.8倍'}</td><td>{pct(r['weekly_cluster_ci_h3'][0])} ～ {pct(r['weekly_cluster_ci_h3'][1])}</td><td>{pct(r['without_best_h3']) if r['without_best_h3'] is not None else '无'}</td><td>{cell(r['spaced'])}</td><td>{cell(r['long'])}</td><td>{cell(r['short'])}</td></tr>"
 sections+='</tbody></table></div><p>区间按入场所在的连续7天分组重抽样，仅衡量这批历史样本的不确定性。去重叠版本每个品种保留首个信号，并跳过其后5根原周期K线覆盖期间的其它信号。</p>'
 html=html.replace('<h2>盈利与亏损，差别在哪里？</h2>',sections+'<h2>盈利与亏损，差别在哪里？</h2>')
 html=html.replace('<title>入场后五根K线 · 盈亏背景研究</title>','<title>2026年历史回测 · 后续五根K线</title>')
path=out/('2026年历史回测总结.html' if annual else '后续五根K线研究表.html')
path.write_text(html.replace('__EVENTS__',json.dumps(events,ensure_ascii=False)).replace('__RESULTS__',json.dumps(results,ensure_ascii=False)),encoding='utf-8')
print('rows',len(events),'report',path)
