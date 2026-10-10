# 第三方组件与参考声明

## Supertrend 与 Smart Money Concepts

SMC 计算改编自用户提供的 © LuxAlgo《Smart Money Concepts》Pine v5 源码，适用 CC BY-NC-SA 4.0（署名、非商业、相同方式共享）；商业使用需另行取得许可。改编部分保留同一许可，完整改编差异与出处见 [指标声明](backend/INDICATORS_LICENSE.md)。看板的 `public/licenses/` 提供许可声明；当前基础评分实现见 `backend/monitor/indicators.py`。图表是已确认结构的实现，不是原 TradingView 脚本全部功能的逐项复刻。

Supertrend 按用户给出的独立公式实现，默认 HL2 / ATR10 / 3倍，支持 Wilder 与 SMA。代码片段未带作者许可头，不额外声称第三方授权。

本文件记录主要第三方组件及设计参考；不是所有传递依赖的完整许可清单。其他依赖的版权和许可应以随依赖分发的 LICENSE / NOTICE 文件为准。以下项目及商标不表示其作者为本看板或监测规则背书。

## TradingView Lightweight Charts™

本看板使用 Lightweight Charts 绘制 K 线与成交量图表。编写本声明时，本地安装版本为 **5.2.1**，许可为 **Apache License 2.0**；本项目没有修改该库源码。

- 项目：[tradingview/lightweight-charts](https://github.com/tradingview/lightweight-charts)
- 对应版本许可：[v5.2.1 LICENSE](https://github.com/tradingview/lightweight-charts/blob/v5.2.1/LICENSE)
- 对应版本声明：[v5.2.1 NOTICE](https://raw.githubusercontent.com/tradingview/lightweight-charts/v5.2.1/NOTICE)
- 作者网站：[TradingView](https://www.tradingview.com/)

以下两行按该版本 NOTICE 原文保留：

```text
TradingView Lightweight Charts™
Copyright (с) 2025 TradingView, Inc. https://www.tradingview.com/
```

本地 npm 分发包包含 `dashboard/node_modules/lightweight-charts/LICENSE`；NOTICE 从相同版本的官方仓库核对。网页还需保留用户可见的 TradingView 署名和网站链接，本声明不能替代页面内的署名。

## shadcn/ui

网页的组件基础使用 shadcn/ui。项目采用 MIT License；使用和再分发时保留相应版权与许可文本。

- 项目：[shadcn-ui/ui](https://github.com/shadcn-ui/ui)
- 许可：[LICENSE.md](https://github.com/shadcn-ui/ui/blob/main/LICENSE.md)

## Tremor 开源看板

卡片层级和信息布局参考 Tremor 的开源看板模板。该项目作为视觉与交互组织参考，不是本项目的市场数据源或信号计算引擎，也不是安装后运行的整套看板。

- 项目：[tremorlabs/template-dashboard-oss](https://github.com/tremorlabs/template-dashboard-oss)
- 许可：Apache License 2.0，见 [LICENSE.md](https://github.com/tremorlabs/template-dashboard-oss/blob/main/LICENSE.md)

## TanStack Table

`@tanstack/react-table` 是网页声明的表格依赖，采用 MIT License。是否包含在最终页面产物中取决于实际导入和构建结果。

- 项目：[TanStack/table](https://github.com/TanStack/table)
- 许可：[LICENSE](https://github.com/TanStack/table/blob/main/LICENSE)

## 币安接口文档

行情字段和接口行为参考币安的公开开发文档。币安数据访问仍受其服务条款、地域规则与接口限制约束；本项目不声称获得币安官方认证，不包含账户交易接口。

- [USD-M REST 行情文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)
- [WebSocket 连接与路由](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)
- [市场数据流](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)

2026-10-08：`backend/monitor/smc_structure.py` 新增内部5/波段50的BOS、CHoCH盘中适配，基于用户提供的 © LuxAlgo Pine v5，适配代码保持 CC BY-NC-SA 4.0。预览使用未收盘价格并回滚到上根收盘状态；去重与Telegram传输由本项目新增。当前独立实时 SMC 还包含 ATR200 过滤的供需订单块、触区、受阻、反弹及失效观察；不包含 FVG、EQH/EQL。
