# Binance REST / WebSocket 审计（2026-09-02）

审计范围是当前可执行源码、脚本和测试。用户提供的计划书只作为需求资料；其中出现的接口名称不等于程序会调用。`.runtime` 临时克隆、`release` 旧压缩包、历史 JSON 记录和 `dashboard/node_modules` 不计入运行代码。

## 结论

审计开始时存在高风险稳态 REST 设计：正常覆盖约 717 个合约后，程序目标每分钟逐币调用 `/fapi/v1/openInterest`，另每分钟调用一次 `/fapi/v1/time` 和 `/fapi/v1/exchangeInfo`。仅固定部分约为 **719 request weight/分钟**，还未包含历史补齐、缺口恢复和候选信号触发的全市场盘口请求。在共享出口 IP 已有其他 Binance 流量时，存在明显 429 风险；收到 429 后继续尝试会升级为 418 IP ban。

整改后，运行代码只保留三类 `/fapi/*`：

| 接口 | 官方 IP weight | 当前用途 | 稳态轮询 |
|---|---:|---|---|
| `GET /fapi/v1/time` | 1 | 启动校时；控制流断线恢复或合约范围核验时随监督器复核 | 无 |
| `GET /fapi/v1/exchangeInfo` | 1 | 启动核验 UM/USDT 范围；`!contractInfo` 发现新合约/状态变化或控制流重连后核验 | 无 |
| `GET /fapi/v1/klines` | `limit` 1–99: 1；100–499: 2；500–1000: 5；>1000: 10 | 第一次 5m/15m 历史补齐与 WebSocket 缺口恢复 | 无 |

程序每页最多请求 499 根，因此一页只会计 1 或 2 weight。全项目没有可执行代码调用 `/futures/data/*`。官方月度归档下载使用 `data.binance.vision` 文件源，不计入 `fapi` request weight。

以下 REST 行情调用已从运行代码删除：

| 已删除接口 | 官方 IP weight | 原风险 | 替代方案 |
|---|---:|---|---|
| `GET /fapi/v1/openInterest` | 每币 1 | 约 717 次/分钟的逐交易对循环 | 关闭实时 OI REST；缺失时 OI 不计分，保留旧本地样本只为兼容历史 |
| `GET /fapi/v1/ticker/bookTicker` | 单币 2；全市场 5 | 候选突发时反复获取全市场盘口 | 公共 `!bookTicker` WebSocket，按 `st=1` 和已核验范围过滤 |
| `GET /fapi/v1/ticker/24hr` | 单币 1；全市场 40 | 旧启动版本曾读取全市场 | `!miniTicker@arr` WebSocket；当前源码已无此调用 |

计划书中还写有 `/futures/data/openInterestHist`。官方标为 IP weight 0，但另有限制 1000 requests/5min；当前程序没有调用它，也不会用“weight 0”规避独立频率限制。

## 429、418 和响应头

所有 Binance REST 请求现在串行进入同一个进程级请求门，避免一个请求收到 429 时还有多批请求已同时发出。每个响应都会读取并记录全部 `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)` 头，不只读取 `1M`；最近 20 条观察可从 `/api/status` 的 `rest_weight_observations` 查看，当前各区间值在 `rest_weight_headers`。

收到 429 或 418 时：

1. 解析 `Retry-After`（支持秒数和 HTTP 日期；缺失时保守使用 60 秒）。
2. 立刻设置全局 REST 冷却时间。
3. 触发该响应的请求直接抛出限速错误，**不会在 `_request` 内继续重试**。
4. 所有排队请求在同一个冷却门等待，冷却结束前不会发送。
5. 5xx 和网络错误仍使用有限指数退避；它们与 429/418 分开处理。

本进程的调度预算为 `exchangeInfo` 公布的分钟上限的 45%，并在 `X-MBX-USED-WEIGHT-1M` 达到交易所上限的 80% 时主动冷却 65 秒。响应头反映整个出口 IP 的使用量，可能包含本机其他程序或同出口设备，因此它比本程序自己的计数更权威。

## WebSocket 架构

717 个合约时连接数按 `2 × ceil(717/40) + 2 = 38` 计算：

| 连接类型 | 数量 | 每连接 streams 上限 | 数据 |
|---|---:|---:|---|
| K 线分片 | 18 | 40 | 每币 `@kline_1m`，只用 `x=true` 的收盘线 |
| 标记价分片 | 18 | 40 | 每币 `@markPrice` |
| 公共盘口 | 1 | 1 | `!bookTicker`，5 秒推送 |
| 市场控制 | 1 | 2 | `!miniTicker@arr` + `!contractInfo` |

最大每连接 40 streams，低于 Binance 的 1024 限制。源码在构造每币分片时也有 1024 硬检查。合并全市场流在 UM/CM 迁移后会包含两个市场，程序同时要求 `st=1` 且 symbol 已存在于经 `exchangeInfo` 核验的 USDT 范围，不能因接入全市场流而混入 CM。

Binance 规定单连接只保证 24 小时。程序按连接名确定性错峰，在约 22:34:39 至 22:57:56 主动换线，避免几十条连接同一秒重连。异常断线仍使用指数退避。K 线重连后先围栏该分片的信号，下一根已收盘 K 线证明连续才解除；发现跳号才进入 `/klines` 缺口恢复。市场控制流重连后触发一次 `time + exchangeInfo` 核验，以补偿断线期间可能漏掉的合约事件。

## REST weight 测算

以下按约 717 个合约、仅 5m/15m、`/klines limit<=499` 计算。

### 审计前正常稳态

| 时间 | 固定 weight | 说明 |
|---|---:|---|
| 1 分钟 | 约 719 | OI 717 + time 1 + exchangeInfo 1 |
| 5 分钟 | 约 3,595 | 未计盘口和缺口 |
| 1 小时 | 约 43,140 | 未计盘口和缺口 |

若每个 5 分钟边界至少有一次候选盘口快照，再增加约 5 weight/5分钟，即约 60 weight/小时。历史下载和断线缺口会继续叠加。因此旧设计在独占 IP 下也消耗较大，在共享 IP 下可能触发 429；若收到 429 后不彻底停发，则可能触发 418。

### 整改后正常稳态

| 时间 | 正常无缺口 weight |
|---|---:|
| 1 分钟 | **0** |
| 5 分钟 | **0** |
| 1 小时 | **0** |

控制 WebSocket 每约 22.5–23 小时主动换线后会做一次 `time + exchangeInfo`，合计 2 weight。摊到一小时约 **0.087–0.089 weight**；实际任意一小时通常是 0，发生控制流重连的那一小时是 2。新合约或状态变化也会额外触发一次 2 weight 元数据核验。它们是事件驱动核验，不是定时行情轮询。

### 第一次空库下载

成熟合约目标为 5m 2,019 根、15m 675 根：

- 5m：499×4 + 23，共 9 weight/合约。
- 15m：499 + 176，共 4 weight/合约。
- 合计 13 weight/合约。

717 个合约完全空库时，历史约为 `717 × 13 = 9,321` weight，加启动 `time + exchangeInfo = 2`，总计约 **9,323 weight**。它会经过共享预算平滑执行，不会一次并发打满。已有本地 K 线只请求缺少区间；新币上市时间较短时也会更少。

一次普通缺口恢复按实际缺页计费：不足 100 根为 1 weight/页，100–499 根为 2 weight/页。这个可变恢复量不计入“正常无缺口”稳态数字。

## 官方依据

- [USDⓈ-M General Info：429、418、IP 权重头与退避义务](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/general-info)
- [USDⓈ-M REST Market Data：各接口 weight](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [USDⓈ-M WebSocket Connect：24 小时、1024 streams](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
- [市场流：miniTicker、contractInfo、K 线、markPrice](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)
- [公共流：all bookTicker](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/public)
