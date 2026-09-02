# PULSE v1.0.1

本版本修复长时间运行后网页信号停止刷新，并完成 Binance REST 限速架构整改。

## 修复内容

- 删除逐合约持仓量 REST 轮询，正常实时运行不再循环请求 700 多个交易对。
- 盘口、成交额和新合约发现分别改用 `!bookTicker`、`!miniTicker@arr` 和 `!contractInfo` WebSocket。
- REST 仅保留启动元数据、第一次历史补齐及断线缺口恢复；正常无缺口运行的固定 request weight 为 0。
- 记录全部 `X-MBX-USED-WEIGHT-*` 响应头；收到 429/418 后严格按 `Retry-After` 全局停发，不重试触发限速的请求。
- WebSocket 每连接最多 40 个 streams，并在 24 小时上限前错峰平滑重连。
- 保留官方额度 45% 的本机调度预算，并在出口 IP 权重偏高时主动冷却。
- 修复 Windows PowerShell 5.1 解析 GitHub Release 列表时的数组兼容问题，保证后续版本可以从看板一键更新。

## 数据处理

- 保留现有本地行情、回测结果、Telegram 配置和用户设置。
- 更新后自动检查并补齐停机及限速期间的缺口。
- 缺口未补齐前继续禁止生成正式信号，避免使用不完整行情。
