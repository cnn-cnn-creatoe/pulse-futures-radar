# 币安 U 本位全币种异常波动监测与 Telegram 实时推送系统计划书

> 实施范围更新（2026-09-01）：正式下载、补缺、回测与信号评分周期仅为 **5m、15m**。后台仍接收 1m 已收盘行情作为 UTC 对齐的内部合成源，但不生成 1m 信号。此前保存的 1m、30m、1h、2h、1d 等历史数据不删除，也不再补齐、修复或评分；本更新覆盖下文早期七周期设计。

## 一、项目目标

开发一套 24 小时运行的币安 U 本位永续合约监测系统，自动覆盖币安当前全部可交易的加密货币永续合约，包括：

- BTC、ETH 等主流币；
- 普通山寨币；
- Meme 币；
- 新上线、小市值、高波动币种；
- USDT 本位永续；
- USDC 本位永续。

系统不再只监测“连续两根阳线”，而是监测所有连续两根已收盘 K 线：

- 阳线 + 阳线；
- 阴线 + 阴线；
- 阳线 + 阴线；
- 阴线 + 阳线；
- 可选包含十字星组合。

当这两根 K 线的综合波动率达到过去若干日平均水平的 **1.5 倍以上**时，系统进入候选信号判断，再结合成交额、主动买卖、持仓量、价格结构和流动性计算 0～100 分活跃度，最后通过 Telegram 实时推送。

正式监测周期固定为：

```text
5m、15m
```

币安官方 U 本位 K 线接口原生支持这些周期。实时采集仍以 1m 已收盘流为内部输入，在 UTC 边界合成 5m、15m；K 线 WebSocket 通过 `x` 字段标明是否已经收盘，数据中还直接包含报价资产成交额和主动买入成交额，可用于活跃度评分。

---

## 二、项目范围定义

### 2.1 “全部 U 本位币种”的准确范围

系统每次启动以及运行过程中，通过：

```text
GET /fapi/v1/exchangeInfo
```

动态获取币安当前的合约列表，不在代码里手工写死币种。

正式纳入的合约需要满足：

```python
status == "TRADING"
contractType == "PERPETUAL"
quoteAsset in {"USDT", "USDC"}
```

同时排除：

```text
币本位合约
季度交割合约
期权
现货交易对
已经停止交易的合约
TradFi 股票类永续合约
```

币安的 `exchangeInfo` 会返回当前交易规则、合约信息和接口限速规则，系统应定时刷新，自动处理新币上线和合约下架。

默认配置为：

```yaml
include_quote_assets:
  - USDT
  - USDC
```

如后续只需要 USDT 永续，可以将配置改为：

```yaml
include_quote_assets:
  - USDT
```

### 2.2 本系统不会监测的币

本系统只能监测已经上线币安 U 本位永续合约的币。

以下对象不在第一版范围：

```text
仅在 Solana、Base、BSC 等链上发行但未上线币安合约的代币
DEX 新建流动池
未上线币安的链上土狗
币安现货有、但没有 U 本位永续合约的币
```

真正的链上新币监测需要另外接入区块链节点、DEX 数据和持币地址数据，建议作为独立系统开发。

---

## 三、核心异常波动信号定义

## 3.1 连续两根 K 线的要求

每次判断必须使用同一交易对、同一周期、时间连续的两根完整 K 线。

必须同时满足：

```text
第一根 K 线已经收盘
第二根 K 线已经收盘
两根 K 线时间完全相邻
中间没有缺失 K 线
当前历史基准数据充足
```

正式判断只接受 WebSocket 数据中的：

```python
kline["x"] is True
```

正在形成中的 K 线可以在后台显示预警，但不能作为正式信号。

### 3.2 K 线组合分类

系统需要标记具体组合：

| 第一根 | 第二根 | 组合标签 | 常见含义 |
|---|---|---|---|
| 阳线 | 阳线 | 阳阳 | 连续向上扩张 |
| 阴线 | 阴线 | 阴阴 | 连续向下扩张 |
| 阳线 | 阴线 | 阳阴 | 冲高回落、方向切换 |
| 阴线 | 阳线 | 阴阳 | 下探反弹、方向切换 |
| 十字 | 阳/阴 | 十字组合 | 高分歧或等待突破 |

默认模式：

```yaml
pattern_mode: all
```

表示四种主要组合全部监测。

同时预留：

```yaml
pattern_mode: mixed_only
```

只监测一阴一阳，也就是：

```text
阴阳
阳阴
```

### 3.3 十字星定义

不能仅通过 `close == open` 判断十字星，因为不同币种价格精度不同。

建议定义：

```text
K线实体 ÷ K线总振幅 ≤ 5%
```

即：

```python
body_ratio = abs(close - open) / max(high - low, tick_size)

is_doji = body_ratio <= 0.05
```

十字星可以进入异常波动判断，但其价格结构得分通常较低。

---

## 四、异常波动率计算方法

## 4.1 为什么不能只使用涨跌幅

如果只计算：

```text
收盘价 ÷ 开盘价 - 1
```

会漏掉以下情况：

- 大幅冲高后回落；
- 大幅下探后拉回；
- 两根 K 线一涨一跌，最后价格回到起点；
- 上下影线非常长，但净涨跌幅很小。

因此建议采用 **标准化真实波幅 True Range**。

### 4.2 单根 K 线真实波幅

对每根 K 线计算：

```text
TR = max(
    High - Low,
    |High - 前一根Close|,
    |Low - 前一根Close|
)
```

再进行价格标准化：

```text
NTR = TR ÷ 前一根Close
```

这样 BTC、ETH 和小币种可以使用同一套比较方式。

### 4.3 连续两根 K 线综合波动

当前两根 K 线分别为 `K1` 和 `K2`：

```text
当前两根综合波动 V2
= NTR(K1) + NTR(K2)
```

同时计算一个便于 Telegram 展示的直观波动：

```text
两根最高最低波幅
= [max(H1,H2) - min(L1,L2)] ÷ K1前一根收盘价
```

正式触发以 `V2` 为准，最高最低波幅作为辅助展示。

### 4.4 历史平均波动基准

对过去若干日内的所有连续两根 K 线窗口计算历史 `V2`：

```text
历史基准波动
= 过去若干日所有两根K线V2的平均值
```

当前异常倍数：

```text
波动异常倍数
= 当前两根V2 ÷ 历史平均V2
```

正式信号条件：

```python
volatility_ratio >= 1.5
```

也就是：

```text
当前连续两根K线波动
≥
过去若干日平均两根K线波动 × 1.5
```

### 4.5 防止历史极端行情污染均值

如果过去几天出现过一次极端插针，普通平均值会被明显抬高，之后很多正常异常信号会被压制。

因此建议同时保存：

```text
原始平均值
稳健平均值
历史中位数
```

正式计算默认使用“缩尾平均值”：

```text
先去掉历史样本最高 2.5%
和最低 2.5%
再计算平均值
```

这仍然属于均值比较，但比普通平均值稳定。

配置：

```yaml
baseline_method: winsorized_mean
trim_percent: 2.5
```

也可以切换为：

```yaml
baseline_method: simple_mean
```

---

## 五、不同周期的历史基准长度

5m、15m 虽然都采用 7 天窗口，但启用所需的最少根数不同：

| 周期 | 默认历史天数 | 约含 K 线数 | 最低启用历史 |
|---|---:|---:|---:|
| 5m | 7 天 | 2016 根 | 至少 3 天 |
| 15m | 7 天 | 672 根 | 至少 3 天 |

配置示例：

```yaml
lookback:
  5m:
    days: 7
    minimum_bars: 864

  15m:
    days: 7
    minimum_bars: 288
```

新上线币种历史不足时，显示：

```text
新上线币种
历史样本不足
暂不生成标准评分
```

不要强行用几根 K 线计算“过去几日平均值”。

可以额外开发“新币模式”，但必须单独标注，不能和成熟币种使用同样标准。

---

## 六、异常波动等级

波动异常倍数独立于活跃度评分。

建议分级：

| 波动倍数 | 等级 | Telegram 标签 |
|---:|---|---|
| 1.50～1.99 | 异常 | ⚡异常波动 |
| 2.00～2.99 | 强异常 | 🔥强异常 |
| 3.00～4.99 | 极强异常 | 🚨极强异常 |
| ≥5.00 | 超级异常 | 🆘超级异常 |

当波动倍数特别大，但成交额、成交笔数非常低时，需要增加：

```text
低流动性插针风险
可能存在价格失真
```

如果波动倍数超过非常规上限，例如 20 倍，还需要进入数据复核流程，防止接口异常、价格精度错误或者错误映射。

---

## 七、活跃度评分系统

异常波动率是强制触发门槛：

```text
波动倍数必须 ≥ 1.5
```

通过门槛后，再计算活跃度评分。

总分 100 分：

| 指标 | 最高分 |
|---|---:|
| 两根 K 线相对成交额 | 35 |
| 价格移动和 K 线结构 | 20 |
| 主动买卖力量 | 20 |
| 持仓量变化 | 15 |
| 流动性质量 | 10 |
| 合计 | 100 |

---

## 八、相对成交额评分：35 分

### 8.1 计算公式

使用 USDT 或 USDC 计价成交额，而不是币的成交数量。

```text
当前两根成交额
= K1报价资产成交额 + K2报价资产成交额
```

历史基准：

```text
过去若干日连续两根K线平均成交额
```

相对成交额：

```text
RVOL2
= 当前两根成交额 ÷ 历史两根平均成交额
```

币安 K 线数据直接提供报价资产成交额，因此不需要逐笔成交重新计算。

### 8.2 建议评分

| RVOL2 | 得分 |
|---:|---:|
| <0.8 | 0 |
| 0.8～1.0 | 4 |
| 1.0～1.3 | 8 |
| 1.3～1.8 | 16 |
| 1.8～2.5 | 24 |
| 2.5～4.0 | 31 |
| ≥4.0 | 35 |

这样可以区分：

```text
只有价格插针，但没有成交额配合
```

和：

```text
价格、成交额同时明显扩张
```

---

## 九、价格移动和 K 线结构评分：20 分

这一部分拆成三项。

### 9.1 两根实体强度：10 分

```text
两根实体总幅度
= |C1-O1| + |C2-O2|
```

标准化后：

```text
实体强度倍数
= 当前两根实体总幅度
÷
过去若干日两根实体总幅度平均值
```

建议评分：

| 实体强度倍数 | 得分 |
|---:|---:|
| <1.0 | 0 |
| 1.0～1.5 | 3 |
| 1.5～2.0 | 6 |
| 2.0～3.0 | 8 |
| ≥3.0 | 10 |

### 9.2 实体效率：6 分

```text
实体效率
=
两根实体绝对值之和
÷
两根真实波幅之和
```

实体效率越高，说明波动主要来自明确方向移动，而不是上下影线反复扫动。

| 实体效率 | 得分 |
|---:|---:|
| <20% | 0 |
| 20%～35% | 2 |
| 35%～55% | 4 |
| 55%～75% | 5 |
| ≥75% | 6 |

### 9.3 最终位移：4 分

```text
最终位移效率
=
|第二根收盘价 - 第一根开盘价|
÷
两根K线最高价和最低价之间的区间
```

| 位移效率 | 得分 |
|---:|---:|
| <20% | 0 |
| 20%～40% | 1 |
| 40%～60% | 2 |
| 60%～80% | 3 |
| ≥80% | 4 |

阴阳、阳阴组合可能实体强度很高，但最终位移较低，这类信号会被标记为：

```text
高波动、高分歧、方向不明确
```

而不是被错误判断为单边趋势。

---

## 十、主动买卖力量评分：20 分

币安 K 线数据提供：

```text
总报价资产成交额
主动买入报价资产成交额
```

计算：

```text
主动买入占比
=
两根主动买入成交额
÷
两根总成交额
```

主动卖出占比：

```text
主动卖出占比
= 1 - 主动买入占比
```

### 10.1 根据两根总体方向确定评分方向

如果：

```text
第二根收盘价 > 第一根开盘价
```

则观察主动买入占比。

如果：

```text
第二根收盘价 < 第一根开盘价
```

则观察主动卖出占比。

例如价格上涨，但主动买入占比只有 43%，则需要标记：

```text
价格上涨与主动成交方向背离
可能存在空头回补或被动卖单撤退
```

### 10.2 评分表

| 顺势主动成交占比 | 得分 |
|---:|---:|
| <50% | 0 |
| 50%～53% | 3 |
| 53%～56% | 7 |
| 56%～60% | 12 |
| 60%～65% | 16 |
| ≥65% | 20 |

如果两根最终方向接近中性：

```text
|最终涨跌幅|很小
```

则使用主动买卖占比较大的一方，但这一项最高限制为 12 分，避免把无方向震荡误判为强趋势。

---

## 十一、持仓量变化评分：15 分

## 11.1 数据来源

币安提供当前持仓量接口：

```text
GET /fapi/v1/openInterest
```

也提供历史持仓量统计接口：

```text
GET /futures/data/openInterestHist
```

当前持仓量接口按交易对查询；历史统计接口支持多个周期，并提供持仓量和持仓价值数据。正式监测只读取 5m、15m。

### 11.2 第一版采集方式

因为历史持仓量接口没有 1m 周期，建议系统每分钟分批轮询全部 U 本位合约的当前 OI，并保存快照：

```text
symbol
timestamp
open_interest_quantity
mark_price
estimated_open_interest_value
```

当两根 K 线结束时：

```text
OI变化率
=
结束时OI - 两根开始前OI
÷
两根开始前OI
```

### 11.3 OI 活跃度标准化

不同币种和不同周期的 OI 变化幅度差异很大，因此不直接使用固定的 1% 或 2%。

使用：

```text
OI异常倍数
=
当前两根|OI变化率|
÷
历史同周期两根|OI变化率|平均值
```

评分：

| OI 异常倍数 | 得分 |
|---:|---:|
| <1.0 | 0 |
| 1.0～1.5 | 3 |
| 1.5～2.0 | 6 |
| 2.0～3.0 | 10 |
| 3.0～5.0 | 13 |
| ≥5.0 | 15 |

### 11.4 价格和 OI 联合分类

| 价格方向 | OI 方向 | 标签 |
|---|---|---|
| 上涨 | 增加 | 新增多头倾向 |
| 下跌 | 增加 | 新增空头倾向 |
| 上涨 | 减少 | 空头回补倾向 |
| 下跌 | 减少 | 多头平仓或清算倾向 |

这些只能作为市场结构描述，不能单独认定真实持仓主体。

---

## 十二、流动性质量评分：10 分

### 12.1 24 小时成交额排名：8 分

根据所有 U 本位合约的 24 小时报价资产成交额进行横向排名。

| 全市场成交额分位 | 得分 |
|---:|---:|
| 后 20% | 0 |
| 20%～40% | 2 |
| 40%～60% | 4 |
| 60%～80% | 6 |
| 80%～95% | 7 |
| 前 5% | 8 |

这样不会用同一个绝对金额标准同时衡量 BTC 和小币种。

### 12.2 买卖价差：2 分

当信号触发后，查询当前最优买卖价格：

```text
spread_bps
=
(ask - bid)
÷
中间价
× 10000
```

建议评分：

| 当前价差 | 得分 |
|---:|---:|
| ≤5 bps | 2 |
| 5～15 bps | 1 |
| >15 bps | 0 |

对于低流动性合约，即使波动倍数很高，也应在 Telegram 中醒目标记：

```text
低流动性
滑点风险较高
```

---

## 十三、最终活跃度等级

| 活跃度得分 | 等级 | 处理方式 |
|---:|---|---|
| 0～49 | 弱活跃异常 | 合并推送 |
| 50～64 | 观察级 | 合并或普通推送 |
| 65～79 | 活跃级 | 实时推送 |
| 80～89 | 强活跃 | 高优先级推送 |
| 90～100 | 极强活跃 | 紧急推送 |

内部可以增加一个推送优先级：

```text
推送优先级
=
活跃度评分
+
波动异常奖励
```

例如：

```python
volatility_bonus = min(10, max(0, volatility_ratio - 1.5) * 5)
priority_score = min(100, activity_score + volatility_bonus)
```

用户界面仍分别显示：

```text
波动倍数：2.34x
活跃度：84/100
```

不要把两个概念混在一起。

---

## 十四、系统总体架构

```text
币安 exchangeInfo
        ↓
U本位永续交易对管理器
        ↓
1分钟 K 线 WebSocket 集群
        ↓
完整1分钟K线缓存
        ↓
本地聚合
5m / 15m
        ↓
连续两根K线判断
        ↓
波动异常倍数判断
        ↓
候选信号
        ↓
成交额 + 主动买卖 + OI + 流动性评分
        ↓
去重、合并、分级
        ↓
Telegram 消息队列
        ↓
Telegram频道、群组或话题
```

---

## 十五、为什么只订阅 1 分钟 K 线

虽然币安原生支持两个正式目标周期，但后台仍统一订阅每个交易对的 1m K 线流，再按 UTC 边界合成 5m、15m。

假设有 `N` 个 U 本位合约：

```text
直接订阅两个正式周期 = N × 2 个流
只订阅1m = N 个流
```

而币安 K 线流在当前 K 线形成过程中最高约每 250ms 更新一次。统一使用 1m 收线源可以减少重复的未收盘数据，并确保 5m、15m 采用同一套闭合与对齐规则。

推荐方式：

```text
所有交易对只订阅 1m K线
收到完整1m收线后
本地只合成5m、15m
```

聚合规则：

```text
Open   = 周期内第一根1m开盘价
High   = 周期内所有1m最高价
Low    = 周期内所有1m最低价
Close  = 周期内最后一根1m收盘价
Volume = 所有1m成交量相加
Quote Volume = 所有1m报价成交额相加
Trades = 所有1m成交笔数相加
Taker Buy Quote = 所有1m主动买入成交额相加
```

聚合时间边界必须按照 UTC：

```text
5m：00、05、10、15……
15m：00、15、30、45
```

Telegram 可以统一显示为北京时间 UTC+8。

---

## 十六、WebSocket 连接设计

币安当前 WebSocket 使用分流路由：

```text
/public
/market
/private
```

K 线、Ticker、Mark Price 等常规市场数据应使用对应的 `/market` 路由。官方文档同时规定单连接最长约 24 小时、单连接最多订阅 1024 个流、客户端发往服务端的消息有频率限制。

推荐分片方式：

```text
每个 WebSocket 连接订阅 200～300 个交易对
连接数量 = 向上取整(交易对数量 ÷ 250)
```

不要因为官方上限是 1024，就把所有交易对全部放进一个连接。

这样可以降低：

```text
单个连接断开导致全市场数据中断
单连接消息堆积
重新订阅时的瞬时压力
```

每个连接必须实现：

```text
自动回复 Ping/Pong
23小时左右主动轮换连接
无数据超时监控
断线指数退避重连
重连后 REST 补齐缺失K线
重复K线去重
连接健康状态上报
```

辅助市场流建议：

```text
!ticker@arr
用于24小时成交额和全市场排名

!markPrice@arr@1s
用于标记价格、资金费率和OI价值估算

!contractInfo
用于监听合约信息变化
```

对于全市场合并数据，需要根据币安返回的市场类型字段确保只保留 U 本位数据。

---

## 十七、REST 接口用途

### 17.1 合约列表

```text
GET /fapi/v1/exchangeInfo
```

用途：

```text
获取全部 U 本位永续合约
新币自动加入
下架币自动停止
读取价格精度、数量精度和限速
```

### 17.2 历史 K 线

```text
GET /fapi/v1/klines
```

用途：

```text
首次启动加载历史基准
断线后补齐缺失K线
定期和本地聚合结果对账
```

币安该接口支持目标周期，单次返回数量存在上限，并根据请求数量消耗不同权重。

### 17.3 当前持仓量

```text
GET /fapi/v1/openInterest
```

用途：

```text
建立每分钟 OI 快照
计算两根K线期间OI变化
```

### 17.4 历史持仓量

```text
GET /futures/data/openInterestHist
```

用途：

```text
校验5m、15m周期OI数据
建立两个正式周期的OI变化基准
```

### 17.5 最优买卖价

```text
GET /fapi/v1/ticker/bookTicker
```

只在候选信号出现后请求，用于计算当前价差，不必对所有币每秒轮询。

---

## 十八、程序启动流程

### 第一次启动

```text
1. 获取 exchangeInfo
2. 筛选全部 U 本位永续
3. 为每个交易对加载5m、15m历史数据
4. 建立波动、成交额、实体、OI基准
5. 标记历史不足的币种
6. 启动1m WebSocket
7. 设置 live watermark
8. live watermark 之后才允许推送
```

必须防止程序第一次启动时，把历史几天内的旧信号全部发送到 Telegram。

### 正常重启

```text
1. 读取数据库最后一根完整1m K线
2. 查询服务器时间
3. 使用REST补齐停机期间数据
4. 重建5m、15m K线
5. 恢复滚动基准
6. 从当前时间之后开始推送
```

---

## 十九、信号计算流程

伪代码：

```python
async def on_closed_1m_candle(candle):
    save_candle(candle)

    for timeframe in ["5m", "15m"]:
        aggregated = aggregator.update(timeframe, candle)

        if not aggregated.just_closed:
            continue

        candles = store.last_closed_candles(
            symbol=candle.symbol,
            timeframe=timeframe,
            count=3,
        )

        if len(candles) < 3:
            continue

        if not are_consecutive(candles[-2], candles[-1], timeframe):
            schedule_rest_backfill()
            continue

        volatility = calculate_two_bar_volatility(
            previous_candle=candles[-3],
            first=candles[-2],
            second=candles[-1],
        )

        baseline = baseline_store.get(
            symbol=candle.symbol,
            timeframe=timeframe,
        )

        if not baseline.is_ready:
            continue

        volatility_ratio = volatility.value / baseline.mean_volatility

        if volatility_ratio < 1.5:
            update_baseline()
            continue

        candidate = build_candidate(
            candles=candles[-2:],
            volatility_ratio=volatility_ratio,
        )

        enrichment = await enrich_candidate(
            open_interest=True,
            liquidity=True,
            token_metadata=True,
        )

        activity_score = score_candidate(candidate, enrichment)

        alert = build_alert(
            candidate=candidate,
            activity_score=activity_score,
        )

        await alert_queue.enqueue(alert)

        update_baseline()
```

注意：

```text
应先完成当前信号计算
再把当前两根数据加入历史基准
```

否则当前异常行情会立即抬高自己的基准。

---

## 二十、去重和连续异常控制

### 20.1 基础去重键

```text
symbol
+ timeframe
+ 第二根K线收盘时间
+ signal_type
```

例如：

```text
PEPEUSDT:15m:1787501700000:TWO_BAR_VOLATILITY
```

数据库唯一索引保证程序重启后也不会重复发送。

### 20.2 连续异常行情控制

如果市场持续剧烈波动，滚动的两根 K 线可能每个周期都触发。

建议设置“异常事件状态”：

第一次超过 1.5 倍：

```text
发送新异常事件
```

后续连续触发时，满足以下任意条件才再次单独推送：

```text
活跃度比上次提高至少8分
波动倍数比上次增加至少0.5倍
市场方向发生反转
波动等级升级
距离上次已超过3根K线
```

其余信号仍然写入数据库，但进入合并消息。

### 20.3 多周期合并

同一个币在 3 秒内同时触发：

```text
5m
15m
```

不要连续发两条消息，应合并为：

```text
触发周期：5m / 15m
```

---

## 二十一、Telegram 推送策略

Telegram 官方建议单个聊天尽量不要持续超过每秒一条；群组通常不能超过每分钟 20 条，超限可能收到 429。

因此“全部币种实时推送”不能设计成：

```text
一个信号 = 一条独立消息
```

否则全市场同时波动时一定会堵塞。

推荐设置 2～3 秒消息合并窗口：

```yaml
telegram:
  batch_window_seconds: 3
  max_alerts_per_message: 8
  max_messages_per_second: 1
```

### 推送等级

```text
90分以上：立即单独推送
80～89分：高优先级推送
65～79分：普通实时推送
50～64分：3秒合并推送
50分以下：低等级合并推送
```

用户要求的“全量模式”可以做到：

```text
所有有效异常不丢失
高分信号单独发
低分信号合并发
全部信号完整保存在数据库
```

### 全市场共振处理

如果 10 秒内超过一定数量的交易对同时触发，例如：

```text
30个以上币种
```

系统先推送一条：

```text
全市场异常波动
共有 47 个 U 本位合约触发
上涨方向 31 个
下跌方向 16 个
```

然后只单独推送其中高评分币种，其余通过一条汇总消息发送。

---

## 二十二、Telegram 消息模板

```text
🚨 极强异常波动｜活跃度 84/100

交易对：1000PEPEUSDT
标准币种：PEPE
分类：Meme
周期：15m
K线组合：阴阳
总体方向：向上

波动倍数：2.34x
两根真实波动：4.28%
过去7日平均：1.83%

两根累计成交额：8.42M USDT
相对成交额：2.71x
主动买入占比：63.4%
持仓量变化：+3.18%
OI结构：新增多头倾向
24h成交额排名：前8%
当前买卖价差：3.2 bps

评分明细：
成交额：31/35
价格结构：16/20
主动买盘：16/20
持仓量：13/15
流动性：8/10

网络：Ethereum
代币地址：0x……
收线时间：2026-08-23 20:15:00 UTC+8

提示：这是异常波动监测，不等同于买入或做多信号。
```

下跌信号示例标签：

```text
阴阴
总体方向：向下
主动卖出占比：68%
OI结构：新增空头倾向
```

一阴一阳示例：

```text
K线组合：阳阴
状态：冲高回落
方向一致性：较低
价格与主动成交：存在背离
```

---

## 二十三、币种地址模块

币安期货交易代码和链上代币地址不是同一个概念。

例如：

```text
1000PEPEUSDT：币安永续合约代码
PEPE：标准代币代码
0x……：以太坊代币合约地址
```

系统需要建立：

```text
期货资产名
→
标准币种名
→
网络
→
代币合约地址
```

### 特殊情况

#### 倍数合约

```text
1000PEPE → PEPE
1000SHIB → SHIB
1000000MOG → MOG
```

需要维护别名表，不建议简单删除所有开头数字。

#### 原生资产

例如：

```text
BTC
ETH
SOL
```

应显示：

```text
原生链资产
无统一代币合约地址
```

不能随便填 Wrapped Token 地址。

#### 多链资产

一个币可能同时存在于：

```text
Ethereum
BSC
Arbitrum
Base
Solana
```

Telegram 必须同时标明网络，不能只显示一个没有网络说明的地址。

可通过币安钱包信息接口读取网络和 `contractAddress`，但该接口属于签名用户数据接口，需要 API Key。

安全要求：

```text
只开读取权限
不开交易权限
不开提币权限
设置服务器IP白名单
密钥只放环境变量
不得提交到GitHub
```

地址获取失败时：

```text
显示“暂无可靠地址映射”
继续发送价格异常信号
```

不能因为地址缺失阻塞整个监测系统。

---

## 二十四、数据库设计

推荐使用：

```text
PostgreSQL
+
Redis
```

PostgreSQL 保存历史数据和信号，Redis 负责实时缓存、去重和 Telegram 消息队列。

### 24.1 交易对表

```text
symbols
```

字段：

```text
symbol
base_asset
normalized_asset
quote_asset
margin_asset
contract_type
status
onboard_date
price_precision
quantity_precision
is_active
updated_at
```

### 24.2 K 线表

```text
candles
```

字段：

```text
symbol
timeframe
open_time
close_time
open
high
low
close
base_volume
quote_volume
trade_count
taker_buy_base
taker_buy_quote
is_complete
```

唯一索引：

```text
symbol + timeframe + open_time
```

### 24.3 指标基准表

```text
metric_baselines
```

字段：

```text
symbol
timeframe
mean_two_bar_volatility
mean_two_bar_quote_volume
mean_two_bar_body
mean_abs_oi_change
sample_count
lookback_start
lookback_end
updated_at
```

### 24.4 OI 快照表

```text
open_interest_snapshots
```

字段：

```text
symbol
timestamp
open_interest_quantity
mark_price
estimated_value
```

### 24.5 信号表

```text
alerts
```

字段：

```text
alert_id
symbol
timeframe
pattern
direction
first_candle_open_time
second_candle_close_time
volatility_ratio
two_bar_range_pct
relative_volume
taker_buy_ratio
oi_change_pct
oi_structure
liquidity_percentile
spread_bps
activity_score
priority_score
alert_level
created_at
sent_at
telegram_message_id
suppressed_reason
```

### 24.6 币种地址表

```text
token_metadata
```

字段：

```text
futures_asset
normalized_asset
network
contract_address
explorer_url
is_native
is_default_network
source
verified_at
```

---

## 二十五、数据保留策略

建议：

```text
原始1m K线：保留7～14天
5m、15m K线：保留至少1年
其他旧周期K线：保留已有数据，但不再补齐、修复或评分
OI分钟快照：保留30～90天
所有异常信号：长期保留
Telegram发送记录：长期保留
```

历史基准采用滚动窗口，不需要每次从全部历史数据重新计算。

可以通过：

```text
新增一条样本
删除最旧样本
更新滚动总和
更新滚动均值
```

降低计算压力。

---

## 二十六、程序技术选型

建议：

```text
Python 3.12
asyncio
aiohttp 或 websockets
orjson
pydantic-settings
PostgreSQL
Redis
FastAPI
Docker Compose
```

币安当前官方 Python 项目已经改为模块化连接器，U 本位合约对应：

```text
binance-sdk-derivatives-trading-usds-futures
```

旧的 `binance-futures-connector-python` 仓库已明确标记为弃用，不建议作为新项目基础。

推荐实际组合：

```text
REST：官方模块化SDK或aiohttp
WebSocket：aiohttp/websockets自行管理
Telegram：直接调用Bot API
```

自行管理 WebSocket 更方便实现：

```text
连接分片
主动轮换
消息延迟监测
缺失K线补齐
连接健康检查
```

---

## 二十七、服务拆分

Docker Compose 中建议包含：

```text
collector
负责币安WebSocket和REST数据采集

signal-engine
负责周期聚合、波动判断、评分

notifier
负责Telegram合并、限速和重试

api
负责健康检查、历史信号查询和配置查看

postgres
负责持久化数据

redis
负责实时缓存和消息队列
```

项目目录：

```text
binance-volatility-monitor/
├── app/
│   ├── collector/
│   │   ├── universe.py
│   │   ├── websocket_manager.py
│   │   ├── rest_client.py
│   │   ├── oi_poller.py
│   │   └── backfill.py
│   │
│   ├── engine/
│   │   ├── aggregator.py
│   │   ├── volatility.py
│   │   ├── baseline.py
│   │   ├── candle_pattern.py
│   │   ├── activity_score.py
│   │   └── alert_builder.py
│   │
│   ├── notifier/
│   │   ├── telegram.py
│   │   ├── batcher.py
│   │   ├── rate_limiter.py
│   │   └── deduplicator.py
│   │
│   ├── metadata/
│   │   ├── token_alias.py
│   │   ├── token_address.py
│   │   └── categories.py
│   │
│   ├── database/
│   ├── api/
│   ├── config.py
│   └── main.py
│
├── config/
│   ├── settings.yaml
│   ├── asset_aliases.yaml
│   └── categories.yaml
│
├── tests/
├── migrations/
├── Dockerfile
├── docker-compose.yml
└── .env.example
```

---

## 二十八、异常恢复机制

系统必须处理：

```text
WebSocket主动断开
服务器网络波动
币安接口短暂不可用
某个交易对缺失K线
Redis重启
PostgreSQL短暂不可用
Telegram返回429
程序整体重启
```

核心措施：

```text
所有K线落库后再确认处理完成
WebSocket和信号引擎解耦
Telegram发送使用持久化队列
接口失败采用指数退避
429严格按照retry_after重试
每分钟执行数据完整性检查
发现缺失K线自动REST补齐
```

健康检查指标：

```text
最后收到WebSocket消息时间
最后完整1m K线时间
各连接订阅数量
缺失K线数量
REST使用权重
待发送Telegram队列长度
过去1小时信号数量
数据库延迟
```

---

## 二十九、回测和效果评估

每个异常信号生成后，自动记录：

```text
1根K线后的收益
2根K线后的收益
3根K线后的收益
5根K线后的收益
最大有利波动 MFE
最大不利波动 MAE
是否延续原方向
是否发生方向反转
```

分别统计：

```text
不同周期
不同K线组合
不同波动倍数
不同活跃度分数
不同成交额分位
不同OI结构
主流币与小币种
```

重点分析：

```text
阳阳异常后延续概率
阴阴异常后延续概率
阴阳异常后的反弹概率
阳阴异常后的回落概率
高成交额和低成交额信号差异
OI增加和OI减少信号差异
```

1.5 倍是初始触发标准，不代表永远是最佳参数。运行积累数据后，可以分别为两个正式周期调整，例如：

```text
5m可能需要比15m更高的阈值
15m可独立验证适合自己的阈值
低流动性币可能需要2倍以上
```

但所有调整都应以历史统计为依据，而不是根据几次个别行情修改。

---

## 三十、测试项目

上线前必须覆盖以下测试：

1. 阳阳、阴阴、阳阴、阴阳是否正确识别。
2. 十字星边界是否正确。
3. 两根 K 线时间不连续时是否拒绝信号。
4. 当前 K 线未收盘时是否禁止正式推送。
5. 波动倍数 1.49 时不触发，1.50 时触发。
6. 当前异常数据是否被错误加入自己的历史基准。
7. 1m 是否正确聚合为 5m、15m。
8. UTC 周期边界是否与币安一致。
9. 程序重启后是否重复推送。
10. WebSocket 断线后是否自动补齐数据。
11. 新币历史不足是否正确标记。
12. OI 数据缺失时是否降低评分，而不是程序报错。
13. Telegram 429 是否按照 `retry_after` 重试。
14. 同一个币多个周期是否正确合并。
15. 全市场同时触发时是否进入市场共振模式。
16. `1000PEPE` 等倍数资产是否映射正确。
17. BTC、ETH 等原生资产是否避免显示错误合约地址。
18. 多网络地址是否标注具体网络。
19. 已下架合约是否停止订阅。
20. 历史回补数据是否不会触发旧消息。

---

## 三十一、项目实施阶段

### 阶段一：规则和基础框架

完成：

```text
项目配置
数据模型
交易对筛选规则
周期规则
波动公式
评分公式
数据库结构
```

验收：

```text
所有公式都有自动测试
所有配置可以通过YAML修改
```

### 阶段二：全市场数据采集

完成：

```text
exchangeInfo
U本位交易对自动更新
1m WebSocket分片
历史K线加载
缺失K线补齐
```

验收：

```text
覆盖全部交易中的U本位永续
连续运行不丢失1m K线
```

### 阶段三：多周期聚合和异常判断

完成：

```text
5m、15m聚合
连续两根K线识别
历史波动基准
1.5倍异常判断
```

验收：

```text
本地聚合结果与币安原生K线一致
```

### 阶段四：活跃度评分

完成：

```text
相对成交额
价格结构
主动买卖
OI
流动性
总评分
```

验收：

```text
每个信号可以输出完整评分明细
```

### 阶段五：Telegram 推送

完成：

```text
消息模板
多周期合并
限速
429重试
市场共振模式
内联按钮
```

验收：

```text
消息不重复
消息不丢失
大行情时不会刷爆Telegram
```

### 阶段六：稳定性和监控

完成：

```text
Docker部署
健康检查
日志
错误告警
数据库备份
连接自动轮换
```

### 阶段七：回测和参数校准

完成：

```text
后续收益统计
MFE/MAE
不同组合胜率
不同评分效果
不同周期参数优化
```

---

## 三十二、验收标准

系统正式上线前应达到：

```text
U本位永续覆盖率：100%
目标周期覆盖率：100%
只使用完整收盘K线：100%
重复Telegram信号：0
缺失K线可自动发现并补齐
程序重启不发送历史旧信号
Telegram限速错误可自动恢复
新币和下架币可以自动处理
信号收线后正常延迟控制在数秒级
所有评分都能回溯计算过程
```

---

## 三十三、推荐的第一版最终配置

```yaml
universe:
  quote_assets:
    - USDT
    - USDC

  contract_type:
    - PERPETUAL

  only_trading: true
  exclude_tradifi: true
  refresh_minutes: 10

timeframes:
  - 5m
  - 15m

signal:
  pattern_mode: all
  closed_candles_only: true
  required_candles: 2
  volatility_ratio_min: 1.5
  baseline_method: winsorized_mean
  trim_percent: 2.5
  allow_doji: true

score:
  relative_quote_volume: 35
  price_structure: 20
  taker_flow: 20
  open_interest: 15
  liquidity: 10

alerts:
  send_all_valid_signals: true
  strong_score: 80
  active_score: 65
  batch_window_seconds: 3
  merge_same_symbol: true
  merge_multiple_timeframes: true
  market_shock_symbol_count: 30
  market_shock_window_seconds: 10

deduplication:
  event_key:
    - symbol
    - timeframe
    - second_candle_close_time
    - signal_type

  repeat_only_when:
    score_increase: 8
    volatility_increase: 0.5
    direction_changed: true
    level_upgraded: true
    bars_elapsed: 3

display:
  timezone: Asia/Shanghai
  include_score_breakdown: true
  include_token_address: true
  include_binance_button: true
  include_chart_button: true
  include_explorer_button: true
```

---

## 三十四、最终系统定位

第一版系统的正式定义应当是：

```text
币安全部U本位加密货币永续合约
5m、15m连续两根完整K线
波动超过历史平均1.5倍
结合成交额、价格结构、主动成交、OI和流动性评分
通过Telegram进行分级、合并、实时推送
```

它不是简单的“两根 K 线变色提醒器”，而是一套：

```text
异常波动发现
+
活跃度验证
+
合约资金变化判断
+
市场流动性过滤
+
多周期共振识别
```

的全市场扫描系统。

第一版只负责监测和推送，不自动开仓。所有信号都应记录后续表现，用真实数据验证哪些周期、哪些组合、哪些评分区间具有更高的参考价值。
