# 公开脚本核对记录

核对日期：2026-09-06。门槛是点赞不少于 5,000，不是用户人数。加锁、邀请制及不可查看源码的脚本跳过。本清单只列已实际核对的项目，不代表全站清点完成。

|作者 / 原脚本|点赞（核对时）|本地处理|来源|
|---|---:|---|---|
|LazyBear / WaveTrend Oscillator WT|60,551|公开源码已查看；公式移植为双线交叉、超买超卖交叉、方向筛选|https://www.tradingview.com/script/2KE8wTuF-Indicator-WaveTrend-Oscillator-WT/|
|LazyBear / Squeeze Momentum|115,837|公开源码和作者更正说明已核对；采用 BB 倍数 2.0 的修正版|https://www.tradingview.com/script/nqQ1DT5a-Squeeze-Momentum-Indicator-LazyBear/|
|KivancOzbilgic / SuperTrend STRATEGY|25,033|56 行源码已核对并保存；翻转入场、反向翻转反手接入策略研究|https://www.tradingview.com/script/P5Gu6F8k/|
|ChartArt / Bollinger + RSI Double Strategy v1.1|约 47,900|完整源码已保存；原版同步交叉条件与不加仓反手接入研究|https://www.tradingview.com/script/uCV8I4xA-Bollinger-RSI-Double-Strategy-by-ChartArt-v1-1/|

## 实现与原版差异

- 原作者：LazyBear。源码可在来源页的 Source code 标签查看。本地公式计算与界面改编保留作者来源；没有发布到 TradingView，也没有发送给作者。
- WaveTrend：使用 HLC3 → EMA10 → EMA10 绝对偏差 → CI → EMA21 → SMA4。偏差为零时无有效值；为控制递归预热误差，前 70 根不发信号。双线交叉和 ±53 条件按收盘确认；原版绘图本身没有仓位或退出规则。
- Squeeze：作者在原页面声明旧源码 BB 倍数误用 1.5，修正版在 https://pastebin.com/UCpcX8d7 。本地使用修正版 2.0，KC20 倍数 1.5；回归值按过去 20 根残差计算。挤压后首次完全释放被明确化为事件，中性阶段保留已挤压状态。
- 上述移植不包含原版绘图对象。缺口会重置所有递归状态。信号只用当时已收盘数据；没有把未来拐点回填到过去。
- 尚未进行 TradingView 导出数值逐点一致性验收，因此称为“公式移植”，不声称与平台逐点完全一致。
- 公开源码许可说明参考 https://www.tradingview.com/pine-script-docs/writing/publishing/ 。作者有明确许可时以其许可为准；当前仅用于用户本地研究，不作为另一个作者的原创脚本公开发布。

## 研究约束

超级趋势策略原版许可 MPL-2.0（见 sources/tradingview/supertrend_strategy.pine）；沿用 HL2 / ATR10 / 倍数3 的默认公式，可使用本机监测规则参数。原版仅 strategy.entry 反手，无固定止损。移植版收盘确认、下一根开盘计成交，添加双边费用与滑点；末尾未平仓保留，缺口不计算完整收益。早期逐笔对比未模拟共享账户资金；新版账户研究已添加共享资金和 OHLC 近似风险强平，仍未计资金费率。不能把早期逐笔百分比相加称为账户收益。

可操作度 v1 为位置、成本后空间、价差、流动性和止损距离的规则评分，不是胜率。只用当时存在的数据；缺盘口历史不能拿今日盘口代替。阈值未校准前不得据此宣称实盘有效。

GitHub 参考： https://github.com/wonsukchoi/domain-experts/blob/main/roles/investment-fund-manager/SKILL.md 。仅参考费用、风险、相关性及区分能力和运气的研究原则，不采用角色经历或业绩自述作为证据。


## 第二批核对

- MACD＋SMA200（ChartArt，榜单约 31K）：完整源码保存于 `sources/tradingview/macd_sma200.pine`。它使用 SMA12 / SMA26 / SMA9，并比较 26 根前的收盘价与当前 SMA200。只接入原版入场条件；原版含账户日内最大亏损 50% 规则，未实现账户模型前不称为完整策略。
- 布林带＋RSI v1.1：RSI6 穿越 50，与收盘价穿回 BB200 的轨道必须同时发生。买入止价为低于当前价的下轨，卖出止价为高于当前价的上轨；按 TradingView 文档，这类止价在提交时即激活，按下一根开盘模拟成交。同向信号不加仓，反向信号反手。仍未与 TradingView 逐笔导出做一致性验收。
- 委托语义参考：https://www.tradingview.com/pine-script-docs/concepts/strategies/#stop-and-stop-limit-orders 。没有将图形颜色作为真实委托。
- 榜单库存保存在 `sources/tradingview/catalogue-inventory.json`，已扫描策略分类到低于 5K；56 项包含待核对和跳过项，不代表 56 条已接入。显示整 5K 的项目要再次核对精确点赞。指标榜尚未清点完成。
- 图表超过 1,000 根时合并展示 OHLC，并明确提示；买卖标记及收益仍为原始计算。超过 20 万根的图表明确报错，不静默截掉入场点。


## 资金回测与第三批公开指标

- UT Bot Alerts：QuantNomad（原始思路 HPotter / Yo_adriiiiaan），57K 以上。保存原版 Pine，采用默认普通收盘、ATR10、灵敏度1；新增原生触发和持续方向。为隔离预热误差，前70根不发信号。
- ADX and DI：BeikabuOyaji，45K 以上，MPL-2.0。原版 ADX 是 DX 的 SMA14，DI 的递推从零开始；没有冒充标准 Wilder ADX。为消退初始值影响，前140根不发信号。DI交叉为本地事件定义，原脚本只画线。
- Williams Vix Fix：ChrisMoody，65K 以上。原版参数22 / 20 / 2 / 50 / 0.85；恐慌高亮属于无方向环境，必须与方向指标使用全部满足，不单独产出买卖指令。
- 对应公开链接及原文件保存在 sources/tradingview/ut_bot_alerts.pine、adx_di.pine、williams_vix_fix.pine 和程序目录内；保留作者来源，未在第三方平台重新发布。
- 资金模型新增自定义初始资金、杠杆、固定每笔保证金、最多持仓。按时间事件共用钱包，扣除双边费用，持仓浮盈亏计入权益。风险强平仅为成交价OHLC近似，缺标记价／历史档位／资金费率，不声称精确复现币安。
- 参考规则：https://www.binance.com/en-AU/support/faq/detail/360033525271 与 https://www.tradingview.com/support/solutions/43000717375-how-to-simulate-trading-with-leverage-in-pine-script/ 。


## 第四批原版默认公式（2026-09-06）

- InSilico / Hull Suite，22,101 赞：sources/tradingview/hull_suite.pine 保存完整 52 行原始源码。当前接入默认 HMA55（半长整型 27，平方根舍入 7），比较 HULL[0] 与 HULL[2]，分别提供原生转向报警和方向筛选。EHMA、THMA、高周期覆盖及绘图尚未移植，不宣称完整工具箱可用。
- ChrisMoody / MacD Custom Indicator，81,566 赞：sources/tradingview/cm_macd_ult_mtf.pine 保存完整 54 行源码。EMA12 / EMA26 的差，SMA9 信号线；保留当前周期默认公式及交叉圆点，不启用旧 Pine security 高周期覆盖。不得将旧版高周期历史回填效果当实时信号。前 140 根用于递归预热。
- 两个原作者公开页均明确为开源，完整来源写在指标菜单中。移植没有任意执行下载的脚本，尚未完成 TradingView 导出数值逐点一致性验收。
- 研究新增 6 组，连同原有共 27 组；使用相同冻结日期和费用假设，所有结果保留。已完成与未完成交易数量分别列出。

## Hull 策略与指标清单补充（2026-09-06）

- DashTrader / Hull Suite Strategy，页面精确 14,607 赞，公开源码 70 行保存于 `sources/tradingview/hull_suite_strategy.pine`。来源：https://www.tradingview.com/script/Q9OQye4C-Hull-Suite-Strategy/ 。原作者基于 InSilico Hull，默认 HMA55、只做多；另有只做空和双向选项。
- 持续的 HULL > HULL[2] / HULL < HULL[2] 发起入场，同向不加仓。单向模式的反向请求只平仓，不反手；依据官方语义：https://www.tradingview.com/pine-script-docs/concepts/strategies/#risk-management 。三个模式分列菜单，属于同一原脚本。
- 当前采用用户设置的固定保证金、杠杆、费用和研究日期。原版采用 100% 权益和零成本；资金口径不同。仅移植默认 HMA55，未实现 EHMA / THMA 变体，未做 TradingView 逐点验收。源码自带日期限制由研究区间替代，结束时仍持仓的浮盈亏保留。
- 额外 3 组按既定 12 币种、5m / 15m 的短样本协议回测，结果保存在 `data/hull-strategy-review-2026-09-06/`。另有 BTC / ETH / SOL 的共享钱包示例，保存在 `data/hull-account-review-2026-09-06.json`，已写入页面研究记录。
- 指标列表现已滚动核对到 4.9K，保存 974 个显示 >=5K 的条目，其中 13 个明确加锁，29 个显示整 5K 的边界项。13 个加锁项排除；其他项仍需逐项打开源码核验。数字是近似点赞数，不是使用人数，更不代表完成接入。此前“指标榜尚未清点完成”已推进至此状态。

## 第五批（2026-09-07）

- ChartArt / Golden Cross，精确 11,270 赞，56 行公开 Pine v2 原文保存在 sources/tradingview/golden_cross.pine。来源：https://www.tradingview.com/script/6ZReHYKn-Golden-Cross-SMA-200-Moving-Average-Strategy-by-ChartArt/ 。SMA50/200 收盘交叉，下一根开盘做多或平多，不开空。同名指标双向交叉用于研究，不能把死叉误认作原策略开空。
- Mihkel00 / QQE MOD（原始思路 Glaz），精确 13,585 赞。v6 完整原文 sources/tradingview/qqe_mod.pine，来源：https://www.tradingview.com/script/TpUW4muw-QQE-MOD/ 。采用 2024-12-11 版本默认参数；与旧 v4 双重平滑不同，v6 RSI 变化只经一次 EMA11。默认主次 RSI 同参数同源，次 QQE 的1.61因子仅影响绘图线，不影响彩柱确认。新增彩柱持续筛选和原版零轴报警，前150根预热；不是完整策略。尚未完成 TradingView 数值逐点验收。
- 新增8组固定协议研究（含 Hull 只多对照），见 data/qqe-golden-review-2026-09-07。训练和验证分开，单边5bps手续费、3bps滑点及双倍成本。未请求币安接口。结果不能证明长期盈利。
- 源码区只读原文，允许复制为本地编辑草稿；草稿保存不会执行或改变回测规则。API仅允许登记规则对应的文件，不接收任意路径。


## 2026-09-07 新增移植

Chandelier Exit、HalfTrend：© Alex Orekhov (everget)，原文保留 GPL-3.0 声明。community_indicators.py 中对应两段函数按公开 v6 默认参数移植为 Python，仅保留闭合 K 线上的买卖布尔条件；图表装饰未移植，收盘确认价与原版止损线绘图价分开。Chandelier 额外保留150根预热，HalfTrend 等待ATR100就绪；不使用未来数据。

完整源码分类见 sources/tradingview/source-classification.json；逐份兼容计算结果见 source-runtime-review.json。兼容计算不代表语义已与 TradingView 对齐，不作为高胜率排名输入。


## 第六批默认策略核心（2026-09-07）

- KivancOzbilgic / PMax Explorer STRATEGY & SCREENER：[原作者](https://www.tradingview.com/script/nHGK4Qtp/)。MPL-2.0。默认 HL2、EMA10、Wilder ATR10×3；EMA 与 PMax 交叉反手。移植单币交易条件，不移植原版30个外汇符号筛选表，避免错误替代跨市场数据。PineTS 对函数最后一条赋值的隐式返回存在差异，适配副本显式返回同一变量；原文保持不变。
- KivancOzbilgic / AlphaTrend Strategy：[原作者](https://www.tradingview.com/script/3wdQu7P3/)。MPL-2.0。默认 MFI14 方向、TR 的 SMA14、系数1；线与两根前的线交叉下单，绘图标签额外过滤不参与原版委托条件。
- KivancOzbilgic / MACD ReLoaded STRATEGY：[原作者](https://www.tradingview.com/script/VO6DY8s1/)。MPL-2.0。默认 VAR12/26/9，CMO9 决定自适应权重；柱线穿零反手。其他均线选项未移植。
- 三个默认核心已用同一1000根本地 BTC 5分钟数据对照适配 Pine 的交易入场序列；预热后分别10、29、36个事件完全一致。另比较750根前缀与完整序列，已闭合历史输出一致。这是兼容引擎交叉验证，尚不等于 TradingView 实机导出逐点验收，也不是高胜率认证。
- 所有策略按用户页面日期、金额、杠杆与费用运行；收盘确认、下一根开盘模拟成交、同向不叠加。源码保留作者与许可，复制适配未去掉版权头。

## 第七批默认交易条件（2026-09-07）

- ChartArt / EMA Slope：[原作者](https://www.tradingview.com/script/XLGBqrZq/)，原文 `archive_c82150ce743755644d4e.pine`。默认 EMA2/4/20，保留原版逆向交叉及斜率分支，不能按名称误改为顺势交叉；前100根预热。接入买卖事件与下一根开盘反手策略。
- VinayKumarKV / Super Scalper：[原作者](https://www.tradingview.com/script/4ORGCiTh/)，原文 `archive_c65117824c343299bb08.pine`。默认真波幅 WMA14 与 RSI25/100；EMA21/65 为绘图辅助，不作为实际委托条件。前500根预热；源码虽然定义止损止盈变量，但未提交相应退出委托，移植不自行添加固定止盈止损。
- 同一1000根本地 BTC 5分钟序列上，EMA Slope 145个、Super Scalper 22个预热后入场事件逐项一致；750根前缀与完整计算保持一致。Super Scalper 的兼容对照副本显式展开默认 WMA 返回值，并将 `when` 条件并入外层 `if`，用于修复兼容引擎遗漏，不能称为未经改写的原文已通过。原作者文件及版权头保持不变。
- 本批默认条件接入实时信号展示和模拟反手规则，没有启用交易所自动下单。其他参数、绘图、日期控制及平台账户语义未宣称完整复现；仍需 TradingView 实机导出对照。独立回测存于 `data/ema-scalper-review-2026-09-07/`，不修改先前冻结的10项指标名单。

## UT Bot 默认策略与兼容性修复（2026-09-07）

- QuantNomad / UT Bot Strategy，来源 https://www.tradingview.com/script/VKMgZdYr/ ，原文 `archive_6d12345eb957f4979c98.pine`。默认普通收盘价、ATR10、灵敏度1；已接入默认反手策略，日期范围使用页面选择。含回测日期输入的同族版本归为同类，不重复推荐。
- 对照兼容副本显式保留原版 `when` 条件，旧版布尔委托方向转换为相同的 long/short 常量；其他参数含状态函数时不自动改写。1000根本地BTC5分钟数据预热后130个事件一致，750根前缀一致，仍不是TradingView导出验收。
- 固定12合约、5/15分钟的独立验证段2275笔完成交易，扣费胜率23.47%，每笔平均净收益-0.1814%，盈亏因子0.456，27笔未完成。双倍成本下平均净收益-0.3415%。结果见 `data/ut-bot-review-2026-09-07/`；可使用默认规则，不宣称高胜率。
- 兼容引擎的有限周期名单已在单独副本中扩展，原依赖包不改写；本地严格聚合完整3分钟、6小时、8小时、12小时数据。新增Heikin-Ashi输入仅供条件计算，非实际成交价。官方依据：https://www.tradingview.com/pine-script-docs/concepts/non-standard-charts-data/ 和 https://www.tradingview.com/pine-script-docs/concepts/timeframes/ 。

## 2026-09-08：MAMA/FAMA 图形与交叉

- LazyBear 原文 `archive_7ce88d80dfea403f8387.pine`，https://www.tradingview.com/script/foQxLbU3/ 。默认 HL2、快限0.5、慢限0.05；保留原文相位单位与递推式，不按其他 MAMA 版本改写。
- 原稿包含两条曲线和可选交叉标记（原开关默认关）；本地 `mama_cross` 明确启用该标记。没有 strategy 委托、独立平仓或止盈止损，不作为完整量化策略。
- 原递推变量 p 的提前历史读取在兼容副本中先声明 na、原位置赋值；原文件未修改。1000 根本地 BTC 5m 双线最大绝对差 4.48e-7，116 个等价显式交叉条件一致。PineTS 的额外 ta.crossover 探针曾返回全零，因此采用原交叉定义显式比较核对；不把这个兼容引擎结果冒充 TradingView 在线核验。
- 图表和研究图表显示原双线与交叉箭头；缺口分段重算，不据未来柱补画历史交叉。

## 2026-09-08：标准化 MACD HA 原版交叉

- EliCobra 原稿 `archive_259d092409cb6a4d5dc5.pine`，https://www.tradingview.com/script/g5qN1YDp/ ，CC BY-NC-SA 4.0。
- `standard_macd_cross` 提取原稿 MACD Bull/Bear Cross；默认 EMA12/26、波幅 EMA26 标准化、HA 收盘与 EMA9。只接入指标触发；原稿无委托或退出，不增加平仓。
- 1000 根相同行情对照兼容副本的振荡器收盘与信号线，最大误差约 3.06e-10；77 次显式交叉完全一致。300 根固定样本和前缀不变性作为回归验证。该结果不是 TradingView 平台导出验收或盈利验证。
- 在线历史图表接口已返回 BTCUSDT 2026-09-07 的 288 根 K 线与 20 个原版交叉标记。买卖采用实际币种参考价，不把振荡器 HA 值用作成交价。
- 图形兼容：明确构造器接收类型的连续方法调用转为带接收参数的同体函数，保留原方法及调用状态；SimpleTrendlines 3/4 完整链接四种数据类型与三个函数。图形脚本通过运行不自动列为完整交易策略。

## 2026-09-08：阶梯 ATR 曲线与翻转

- Trendoscope 原文 `archive_49d2c5c3a740a436862a.pine`，https://www.tradingview.com/script/8EZaD3CW/ ，CC BY-NC-SA 4.0；依赖原作者 arrayutils/10 的浮点数组重载。
- 默认 HMA7、倍率4；分别维护阳线和其他K线的真波幅数组，保留库中旧值优先的权重以及 7/2=3.5 的原计算。翻转按前一根高低价与前一根停止线比较，不能改为当前收盘价。
- 1000 根同样行情，43 次方向变化一致，停止线最大差约 5.97e-10；固定400根样本验证和前缀测试通过。图表及研究图表已接入双色停止线，在线接口返回288根K线与对应曲线。该线为原指标计算线，不自动生成退出委托；原指标无独立平仓。

## 2026-09-08：布林带＋RSI 只做多 v1.2

- ChartArt 原文 `archive_0c1e81bf0097a4781d82.pine`，https://www.tradingview.com/script/HDSdFMa3/ 。新增 `bb_rsi_long`，复用已核对的 RSI6 与 BB200/2 同步交叉条件，但独立保留原版只做多和 `strategy.close` 平多语义。无持仓时的退出条件不画平仓箭头，不增加空仓。
- 1000根固定行情的3个原始条件完全一致，并验证800根前缀；包括入场和退出条件。止买价低于信号收盘价，因此已激活，默认下一根开盘模拟成交；官方依据 https://www.tradingview.com/pine-script-docs/concepts/strategies/#stop-and-stop-limit-orders 。兼容引擎曾错误使用止买价成交，因此其成交价不作为本地回测的正确性依据，仅对照条件。
- 本地验证覆盖空仓退出、持仓退出、重复入场、下一根开盘与缺口处理；资金、手续费与滑点使用页面设置。未宣称收益率或 TradingView 导出验收；没有交易所下单。

## 2026-09-08：分形突破原版只做多

- ChartArt 原稿 `archive_e308e9b0d6406bc78662.pine`，https://www.tradingview.com/script/EjLwVtgp/ 。新增指标条件 `fractal_breakout_conditions` 与持仓策略 `fractal_breakout_long`，源码面板、研究库和推送规则目录共用同一目录。
- 严格保留 occurrence=1 的 valuewhen：第二近分形确认时的 HL2，而不是分形顶点的 high。三根历史分形价均值上升且上一根 HL2 突破时进入；三根前均值上升而当前不再上升时显式平多。参数3不表示固定持有三根，不增加止盈止损或做空。研究资金与成本按页面设置。
- 原绘图 offset=-2 仅影响装饰位置，确认与交易箭头不提前两根。1000根同样行情中87个入场条件、166个退出条件与兼容源码逐根一致，750根前缀一致。持仓过滤重复入场和空仓退出；模拟下一根开盘成交。固定样本覆盖分形确认延迟与第二近取值，不代表 TradingView 平台导出或盈利验收。

## 2026-09-08：用户调整范围、自选规则与一年比较

- 剩余未接入范围以 `data/remaining-port-scope.json` 为准：指标存档点赞严格大于10000、策略全部；原有接入项不移除。兼容通过与真实规则接入分开统计。
- Bunghole 的 Flawless Victory（archive_c92fb6db967527b6029a，https://www.tradingview.com/script/i3Uc79fF/ ，MPL-2.0）默认v1接入。BB20一倍标准差、RSI14阈值42/70，原文113/114行明确开多/平多。v2/v3默认关闭，未冒充已接入的止盈止损版本。1000根条件与750根前缀对照通过。
- `data/rule-year-review.json`：BTCUSDT 5m，2025-09-01至2026-08-31（UTC），105120根无缺口，固定参数，单边手续费5bp、滑点3bp；不含资金费率。18个策略使用源码退出，28个指标只作5根观察，不把观察期限当原生退出。提供全年及末25%区间统计，未进行参数优化；同一数据排序不属于独立样本外选优。
- Flawless v1：647笔，胜率59.81%，平均每笔-0.164%，盈利因子0.68；不可单凭胜率称为盈利策略。
- 自选10项支持混合指标/策略，按原持仓动作记录策略确认；新增选择不伪造历史记录。配置持久保存在 `data/rule-shortcuts.json`；菜单支持搜索及按点赞/一年胜率排序。初始化优先明确平仓，再按点赞。图表可操作度和活跃原分同时筛选；缺失评分不通过，原策略退出保留，筛选不改变回测委托。
# 2026-09-08：新增默认规则与盘中预览

- ChrisMoody，CM Sling Shot System，存档 `archive_f3e007681a0eb903c2f7.pine`，原作者链接 https://www.tradingview.com/script/GE7tSQK1/ 。接入保守入场条件和 EMA38/62 图线。原式前一收盘与当前快线比较，未改成标准 crossover；黄色回调和持续趋势三角不作入场，无平仓。500 根预热后对照本地 Pine 适配运行结果，前缀一致。预热初期两引擎 EMA 初始化存在差别，因此不输出该段信号。
- allanster，How To Set Backtest Range，MPL-2.0，存档 `archive_7df3fc487f2552a0980d.pine`，链接 https://www.tradingview.com/script/62hUcP6O/ 。默认 HLC3 EMA12/26，金叉开多、死叉明确平多，500 根预热。用户日期、资金和成本为显式覆盖；原版默认日期及成本已在菜单说明。不是把示例原始固定日期当成当前日期，亦未添加做空或止盈止损。
- 新增规则已加入一年固定 BTCUSDT 5m 样本报告；指标为 5 根观察，策略使用源码退出。扣费表现不佳，未作为高胜率策略推荐。
- 图表盘中预览使用现有实时一分钟行情，聚合为用户选中的周期。临时计算副本不保存成已收盘 K 线。前端每 2 秒检查，记录当前会话最近 10 条首次观察，不保证捕获检查间隔中的短暂条件，不作为 TG 推送或后台持久记录。历史图表和回测继续使用真实收盘数据。


## 2026-09-08：经典指标批次及明确成交时机

- `monitor/classic_ports.py` 的 SPECS 逐项记录原始源码 ID、原作者、参数和触发性质；EWO 两版本、MACD 4C/VXI、OBV、CM EMA/RSI、Tony EMA、VAPI、Trend Magic、四/三均线、Colored EMA、MavilimW、Coral、QQE、LuxAlgo SR。图形衍生触发明确标注，不添加原文不存在的平仓。各原文件及原许可未改。预热500根后1000根固定样本对照，750根前缀一致。
- LuxAlgo SR：15根右侧确认后延迟1根；历史显示offset不当作提前信号。TradingView实际Pine v6测试：每8根中第1/2根同为10，其余为0；pivothigh(x,2,2)与pivotlow(-x,2,2)的最后确认phase均为4，允许左侧相等但右侧严格。PineTS原本漏掉等高平台。仅此源码的适配副本补等价枢轴辅助函数，Python按同语义实现。
- Tony旧版cross==1改为原布尔条件，OBV函数的变化量全局于函数作用域逐根计算一次；均保持原逻辑并重新验证适配副本。
- QuantNomad UT Bot日期范围版：默认1/10/普通收盘价，日期映射研究设置，实际只反手，无独立平仓；信号与原适配源码一致。
- TradeDots默认Long：保留价量各60、SMA200、最近5收盘与当前SMA比较，明确平多，不开空。均线逐根求值修复兼容短路问题。原风险资金参数在菜单显示，回测使用用户输入覆盖。
- LuxAlgo Moon Phases：按原固定时间/周期模运算，不替换为天文预测。默认Full/New Moon，原process_orders_on_close=true，逐笔与账户模拟均用本根收盘；合成日时间样本仅用于时间公式对照，盈亏使用另存真实一年BTC 5m样本。

2026-09-08 加密适配与 OTT 批次：

- 原 `archive_2d7e17d0cd8273b5535f` (LazyBear) 的 NYSE 上涨/下跌成交量不可得。用户明确授权改用加密数据。`crypto_swenlin_flow` 保留 EMA4→SMA5，对真实主动成交额差/总成交额计算；不再声称衡量市场广度，周期跟随所选周期。原 `archive_f22a5ea6dfdda6f551ed` 多空持仓图改为 `crypto_taker_balance` 主动买卖成交额交叉，明确“非持仓”。原文未改，当前可执行源码为 `monitor/crypto_adaptations.py`，不是原 Pine 等价兼容证明。
- OTT `archive_07bf5d7fd4f93c26e209` 与 Twin OTT `archive_528a27f351512ef3cfba`：KivancOzbilgic / Anil Ozeksi，MPL-2.0，原作者与许可完整保留。分别默认 VAR2/1.4% 与 VAR40/1%/.001 双轨，保持两根延迟。OTT 外部外汇美股筛选展示改由平台选择加密币种；单币策略委托条件不改。Twin OTT 图形标签的重复过滤没有添加到委托条件。Python 核心 `monitor/ott_ports.py`，原版策略仅反向 entry，无独立 close/exit。
- OTT 原始入场块的兼容运行结果与本地逐根对照，真实1000根及额外明确标注的合成波动案例均通过，750根前缀不变。外部筛选展示未冒充完整原版运行通过。
- 四个新增指标及两策略均有固定一年 BTC5m 成本后评估；指标5根观察退出只用于评估，不作为新增平仓委托，结果不支持高胜率认证。

2026-09-08 图形、原版报警及原生括号单续批：
- graphic_ports.py 已接入20份图形衍生触发，每项SPECS均有原始ID、作者和精确触发口径；原文没有退出的未添加平仓。1000根公式对照及750根前缀检查通过。
- UT初版灵敏度3、AlphaTrend Potential原始报警及QQE signals原始4.238系数完成逐根对照。Gann仅适配副本将SMA移到逐根求值，原档案未改。
- Hammers & Stars（archive_ef1c0512d7f506ea3b2e）：默认ATR14、1R目标、上下三分之一实体过滤、空仓才开仓；原生止盈止损按固定信号K线价格保留，下一根开盘进场。历史OHLC路径按离开盘较近的极值先到，跳空按开盘；等距双触价保守止损优先并标注。平仓记录时间为该根收盘确认，未伪造毫秒成交时刻。页面资金覆盖原始1%风险仓位，非原版资金曲线。账户模拟使用实际模拟退出价和退出前路径，不把退出后极值当爆仓。
- 三份加密替代新增crypto_flow_trend：Global Liquidity Index原宏观数据改为真实累计主动成交额差，保留EMA30/60趋势切换。不是央行流动性、资金净流入或全市场资金量，不复刻宏观相关/Beta表；500根预热，缺失或行情断档重置。替代规则可执行Python与原Pine均可查。
- 本批一年BTC5m扣费评估均已保存，无高胜率认证。指标五根观察不是新增退出。最新相关检查155项通过，前端类型检查通过；新增加密趋势和缺口检查通过。

2026-09-08 趋势、固定网络及默认枢轴策略续批：
- classic_ports.py新增CM Ultimate V1/V2默认SMA20变色、LuxAlgo Trendlines with Breaks原报警、VFI原130周期双线交叉（衍生）、Twin Range Filter原报警、Fibonacci levels默认EMA100中线交叉（衍生）、CM SAR默认0.02/0.02/0.2变色（衍生）。每项SPECS保留原作者、源码ID及明确条件；不是把同默认条件的两个CM版本当独立证据。
- ANN v2与SeaSide420仅将可选参考周期显式设为用户选中周期（原默认D）；并非宣称默认日线公式。ANN原固定1→5→33→1权重保留，原源码权重并入回测快照；SeaSide保持开盘价、Hull14、云9/26/52、Hull MACD12/26/9全部条件。两者原来只有entry，未启用注释止损。
- Pivot Point SuperTrend指标和策略默认参数均接入。策略原文55行close_all，60/62行entry；usecenter默认false，未启用半仓变体。保留确认时刻、图线不充作价格止损。TradingView实际临时验证：每10根先close_all再相反entry，固定数量1，MAX_POSITION=1，CLOSED_TRADES=92；临时策略已移除，编辑器恢复默认Untitled，未真实下单。
- Lux趋势线及两个Pivot ST适配副本修复等高平台枢轴：左侧允许相等、右侧严格，原文件不改；局部辅助函数不更改全局运行器。正式兼容复验均computed。各新增规则1000根条件与750根前缀对照通过；一年固定BTC5m样本扣费结果已保存，未作高胜率认证。

2026-09-08 Full CRYPTO 与括号单续批：
- Full CRYPTO（SoftKill21，DbRYTl6G）默认普通K线、HL2 EMA9/MACD12/26/9、RSI14和close OBV/SMA10，默认只做多与明确平多。按页面日期覆盖原2020—2021区间。除公式1000/750对照外，另将原Pine日期窗口设为第500根起，核对原longOpeneda/exitLongCondition与本地实际开平过滤逐项一致。原HA和做空均未开启。
- ATR Stop Loss Finder只画参考线，本地明确“前值突破（图形衍生）”，没有将其当实际平仓策略。默认ATR14×1.5，图表提供上下参考线；Fibonacci默认EMA100中线也加入图表。
- 源码清单不再允许“仅接入策略入场指标”标成整份策略完成；有回归检查。
- 3Commas Bot（Bjorgum，MvlwAzSg）默认EMA21/50、空仓、五根极值外ATR14和1R固定目标完成原生规则。原跟踪/反手/20%回撤保护开关关闭；资金日期用页面覆盖，未连接外部机器人。与Hammers共用明确OHLC括号单成交路径，未新增未经授权退出。
- 修复兼容迁移中的空格：strategy.entry  (...)的when也正确转换为外层条件；不条件化带状态函数的其他实参。仅本3Commas副本重新生成，原文件未改，正式复验computed；之前无条件下单的兼容结果已作废更新。
- 本批固定一年样本评估完成，均无盈利认证。3Commas1187笔46.08%胜率、平均-0.15945%；Full CRYPTO1381笔27.66%胜率、平均-0.17848%。均已计固定手续费滑点，不含资金费率。

2026-09-08 原版形态和CM报警续批：
- CM Price Action（archive_8684888ba55b4aaac00e）分别明确启用原默认关闭的Pin Bars和Shaved Bars；保留66%影线/6根极值与5%收盘区间，零振幅Shaved双向条件保留，不添加退出。
- CM Stochastic MTF（archive_af8135b2fd4c5ea842cb）保留14/3/3及原严格20/80买卖；CM Ultimate RSI MTF（archive_a2124536a52bea8c3844）保留14及严格穿越30/70。均使用原默认当前图表周期，不启用第二周期图线。
- TMA Overlay（archive_fdc02acc45491e022cdb）的吞没报警、三线打击箭头分别接入；原时段底色和均线不是报警过滤，未添加相关限制或平仓。
- 六条规则与完整兼容Pine逐根1000条对照及750条前缀一致。CM两份关闭的第二周期图线在对照夹具中设为5分钟，主周期与触发公式未改。固定一年BTC5m扣成本观察均已保存，指标五根观察不构成原生平仓或盈利认证。
- 3Commas补充完整Pine委托成交对照：相同日期范围、固定单位仓位、无成本下，进出场K线及价格与本地括号单引擎一致；测试资金设置有明确覆盖记录。
- Madrid Ribbon（archive_5a7883d813f898249cc1，Madrid，MPL-2.0）新增EMA5主线原颜色衍生触发：相对EMA100且斜率满足原亮绿/红色条件的首次进入。图表画出两条条件均线；不是完整色带复制或原生退出。1000根公式和750根前缀对照通过，一年成本后观察已保存，相关29项测试通过。
