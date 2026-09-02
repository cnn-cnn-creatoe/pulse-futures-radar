# 后端说明

本机后端由 Python、FastAPI、aiohttp 和 SQLite 组成；不使用交易账户密钥，不包含交易模块。Telegram 是默认关闭的可选纯文字通知通道。完整启动步骤和限制见 [项目说明](../README.md)。

## 接口

默认地址为 [http://127.0.0.1:8765](http://127.0.0.1:8765)，完整交互文档见 [/docs](http://127.0.0.1:8765/docs)。

| 方法与路径 | 内容 |
| --- | --- |
| `GET /api/status` | 采集状态、历史预热、连接、队列、缺口与最近错误；数据库计数最多约每分钟更新一次 |
| `GET /api/signals?mode=live` | 本地已保存的真实信号，按收盘时间倒序；最多返回最近 10000 条 |
| `GET /api/signals?mode=demo` | 当前规则下的合成样本，不读取或写入真实信号表 |
| `GET /api/universe` | 当前通过元数据筛选的合约列表；访问受限时列表可能为空 |
| `GET /api/chart/{symbol}/{timeframe}?mode=live` | 本地已收集的 K 线；未预热或未采集时可能为空 |
| `GET /api/settings` | 规则、历史长度和当前 Telegram 开关状态 |
| `PUT /api/settings` | 保存规则；需 JSON 请求及 `X-Pulse-Client: dashboard` 请求头 |
| `GET /api/telegram/settings` | 脱敏的连接、机器人、绑定会话和筛选状态；永不返回 Bot Token 或完整 chat_id |
| `PUT /api/telegram/settings` | 只写 Bot Token 和/或保存筛选草稿；保存本身不连接、不发消息 |
| `POST /api/telegram/connect` | 验证机器人并连接；未绑定时仅在本次响应返回十分钟有效的一次性链接 |
| `POST /api/telegram/test` | 明确请求后向已绑定会话发送一条纯文字测试消息 |
| `POST /api/telegram/disconnect` | 断开通知运行时；期间事件不缓存、不补发 |
| `POST /api/telegram/unpair` | 解除当前会话绑定，保留非密钥筛选草稿 |

写入配置只接受本机看板来源。不支持账号认证或公网服务，因此不要把这些端口暴露到公网。

## 数据文件与环境变量

| 项目 | 默认值 / 行为 |
| --- | --- |
| `PULSE_DATA_DIR` | 默认 `backend/data`；可改为本机其他数据目录 |
| `PULSE_DISABLE_COLLECTOR` | 值为 `1` 时不启动实时采集；演示、已有记录与设置接口仍可用 |
| `PULSE_TELEGRAM_BOT_TOKEN` | 可选；环境变量优先于网页保存的密文，存在时网页不能覆盖或清除 |
| `pulse.sqlite3` | 真实 K 线、不可重复插入的历史信号、分钟 OI 快照 |
| `settings.json` | 网页保存的规则；重启后读取 |
| `telegram-settings.json` | 不含密钥的 Telegram 开关和筛选草稿 |
| `telegram.sqlite3` | 绑定会话、菜单偏好、update offset 和发送去重记录；不保存 Bot Token |
| `telegram-token.dpapi` | Windows 当前用户 DPAPI 密文；复制到其他账户或机器不能解密 |

SQLite 使用 WAL，运行中可能出现 `pulse.sqlite3-wal` 和 `pulse.sqlite3-shm`。备份前先停止后端，再复制数据目录；不要只复制一个正在写入的数据库文件。

## Telegram 连接与筛选

初始值是 `signal_scope=activity`、最低活跃度 80、空策略、信号关闭、新上架提醒开启。网页可在绑定前保存草稿，但未绑定时没有接收方；打开一次性链接并发送 `/start` 的首位用户成为管理员。之后陌生用户只能提交申请，管理员在 Telegram 中通过或拒绝；获批用户拥有独立偏好，且其信号与新上架提醒均从关闭开始。拒绝或移除是终态，重复消息不会重新申请或通知管理员，只能由管理员 `/add` 恢复。管理员可使用 `/users`、`/pending`、`/add <ID>`、`/remove <ID>` 管理授权。程序层拒绝非私聊消息，建议同时在 BotFather 关闭 `/setjoingroups`。Telegram 内联菜单用于修改当前用户自己的偏好；网页修改管理员偏好：

管理员输入框下方常驻“推送设置、申请列表、授权用户、重置筛选”快捷按钮；普通授权用户只显示“推送设置、重置筛选”。“申请列表”会为每一条待审申请附上通过/拒绝按钮。“重置筛选”恢复为 5m + 15m、多空、活跃度口径、最低 65、空策略、任一通过，但保留该用户当前的信号推送和新上架开关。

- 周期：`5m`、`15m`，至少选择一个；
- 方向：`long`、`short`，至少选择一个；
- 活跃度：65、80、90；
- 信号范围：`activity` 为按活跃度和所选指标提醒，标题为“强活跃异常”；`qualified` 还要求完整门槛通过，标题为“完整强信号”；
- 策略：空数组表示不做指标过滤；可选 Supertrend、SMC，多选时用 `any` 或 `all`；同一事件命中多个策略仍只发送一次；
- 新上架提醒与信号开关相互独立。

每条活跃异常都会明确显示完整门槛是否通过；未通过时显示最多两条原因，避免把按活跃度提醒误读成完整强信号。方向优先使用已保存的交易计划或入场方向证据，再使用信号方向和 K 线形态。消息不设置 Markdown/HTML，也不生成图片。

通知服务只接受行情采集器在本次运行中新保存的实时事件，不扫描 `alerts` 或 `listing_events` 历史。连接前、断线期间和重启前的事件不会补发。发送按会话限速，并按 Telegram 的 429 `retry_after` 做有界重试；成功记录以“会话 + 事件”持久去重。内存发送队列不是生产级消息队列，异常退出时待发事件可能丢失。

正式下载、补缺和评分只处理 5m、15m。1m 仅作内部实时合成源；此前保存的其他周期不删除，也不再补齐、修复或评分。运行中的清理任务仍管理内部 1m 与 OI 的留存周期，不是独立计划任务。

## 默认规则

```json
{
  "volatility_ratio_min": 1.5,
  "baseline_method": "trimmed_mean",
  "trim_percent": 2.5,
  "pattern_mode": "all",
  "allow_doji": false
}
```

`pattern_mode` 可改为 `mixed_only`，只接收阳阴、阴阳。十字星按实体 / 总振幅 ≤ 5% 判断，并用币种 tick size 保护分母；默认不纳入正式信号。

| 周期 | 默认基准天数 | 最低历史 K 线数 |
| --- | ---: | ---: |
| 5m | 7 | 864 |
| 15m | 7 | 288 |

底层一次分析至少接收“最低历史数 + 3”根：历史样本、用于标准化的前一根，以及当前两根。当前两根不会进入自己的波动、实体或成交额基准。所有输入必须同一币种、同一周期、连续且完整；发现缺口直接拒绝判定。

时间戳采用 UTC Unix 毫秒，`close_time = open_time + 周期毫秒数 - 1`。内部 1m 聚合只输出完整的 5m/15m UTC 桶，不补造缺失分钟。

`TR = max(H-L, |H-前收|, |L-前收|)`，`NTR = TR / 前收`，当前综合波动为两根 NTR 之和。实体比较同样按各自前收标准化。默认剔除两端各 2.5% 样本；`winsorized_mean` 仅为旧配置兼容名，不做端点替换。

OI 使用同周期、相隔两根 K 线长度的快照变化，并要求至少 30 个有效历史变化样本。数据缺失、时间错位或基准为零时不计 OI 分；总分仍按原始分值展示，不换算到 100。近中性方向按净位移不超过两根高低区间的 2% 或一个 tick 判断，其主动成交分最高 12。

## 状态与故障说明

- `warming`：历史加载中；`insufficient_history`：样本不够；均不能视为已覆盖。
- `ready`：当前检查范围内数据已就绪；应同时检查分片连接、就绪周期数量与最后收线时间。
- `blocked`：官方服务拒绝访问，例如 HTTP 451；不会回退成虚假实时行情。
- `reconnecting` / `degraded`：连接或部分数据恢复中；缺口期不接受新的正式候选信号。
- 已实时接收的候选若评分排队超过 90 秒，保存时标记 `delayed` 与 `processing_delay_ms`，不能当成刚发生的行情。

WebSocket 接收与评分分开排队；候选队列只保存紧凑引用，评分时重新读取对应收盘时间之前的历史，避免排队期间混入之后的 K 线。每个币种的收线处理串行执行，数据库唯一键保证重复事件不会重复存为信号。

重启后历史补齐只恢复数据，不把过去的异常当作新实时信号。队列目前仍在内存中，异常退出可能丢失尚未保存的候选；正式长期部署前需补充持久化队列、服务守护、备份与实网压力验证。

早期环境曾返回 HTTP 451；2026-08-31 晚间已能通过同一官方接口下载并接收真实行情，但仍在检验全市场持续运行表现。首次历史下载、后续增量采集、本轮性能调整与实测限制见 [采集性能记录](PERFORMANCE_NOTES.md)。离线测试不能代替真实行情验收。

## 运行测试

在 `backend` 目录先安装运行依赖与测试依赖；`requirements-dev.txt` 包含 FastAPI 接口测试需要的 `httpx`：

```powershell
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m unittest discover -s tests -v
```

## 最近一年回测与 CSV

回测只接受 `5m`、`15m`。网页接口的官方月档案下载一次可选最多 12 个已经结束的 UTC 月，5m 一年约 105,120 根，15m 一年约 35,040 根。每个月都核对币安官方 SHA256；重新运行时，有效本地压缩包会直接复用。回测先分别验证 Supertrend、SMC、EMA，再验证三个两两组合和三指标组合，统一使用下一根开盘、手续费、滑点、支撑阻力止损/目标、70/30 时间外验证及三折锚定滚动时间外验证。

完成的任务可下载两个 UTF-8 BOM CSV：

- `GET /api/backtests/{job_id}/summary.csv`：7 个指标组合在训练、时间外、滚动时间外分段的胜率、净期望、盈利因子、最大回撤、成本和样本结论。
- `GET /api/backtests/{job_id}/trades.csv`：逐笔信号、入场/出场、方向、两根 K 线形态、异常倍数、止损/目标、净收益、MFE/MAE 和前瞻收益。

全市场长任务使用可恢复批处理，不读取或修改实时数据库：

```powershell
python -u -m monitor.year_batch --symbols BTCUSDT,ETHUSDT --timeframes 5m,15m `
  --first-month 2025-08 --last-month 2026-07 --output data/year-backtest/example
```

也可用 `--symbols-file` 传入每行一个合约代码。批次在每个月下载后更新 `batch.manifest.json`；中断重跑会复用已校验档案并跳过已完成的标的/周期。710 个标的同时做两个周期的理论上限约为 99,513,600 根 K 线，因此应作为分批、可恢复的离线研究任务运行，不能把尚未完成的结果显示成已验证。

批次完成后可归一化 CSV、合并 5m/15m，并发布到网页能读取的研究报告目录：

```powershell
python -m monitor.research_delivery `
  --batch-root data/year-backtest/BTCUSDT-latest-12m `
  --research-root data/backtest
```

交付程序会从已有逐笔轨迹派生首选的双向 `entry-events.csv` 和 `entry-summary.csv`：分别保留 `Supertrend + SMC` 同时看多的买入做多点和同时看空的卖出做空点，按标的、周期、方向及信号收线时间去除训练、时间外和滚动验证的重复记录，以下一根开盘为参考入场，并统计后第 1/2/3/5 根收盘按方向扣除双边成本后的胜率。这里不研究退出策略；每个方向少于 30 个有效事件一律明确标为样本不足。兼容的只做多 `long-events.csv`、`long-summary.csv` 仍会保留。

这些测试检查规则、聚合、缺口恢复、存储与 HTTP 接口，不发起真实交易或 Telegram 请求。
