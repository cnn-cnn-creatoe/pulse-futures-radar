# PULSE · 合约异动雷达

**Windows 桌面发行版：v1.0.0。** 已包含 Supertrend / SMC / EMA 可选确认、强信号过滤、全量币种状态、新上架提醒、历史数据校验、回测页、Telegram 私有审批，以及纯净电脑的一键安装/启动/关闭/更新流程。操作说明见 [安装与更新说明](安装与更新说明.md) 和 [功能说明](backend/功能说明.md)，真实历史实验见 [回测说明与结果](backend/BACKTEST.md)。Telegram 为可选功能，默认关闭且信号推送默认不启用。

发布包**不包含任何行情数据库、回测结果、Telegram Token 或个人设置**。每台电脑首次启动后会自动建立自己的 `backend/data`，页面显示逐币下载进度；随后已收盘的 5m/15m K 线持续写入本机。再次启动时保留已有数据，只检查并补齐停机期间的缺口。

当前范围按用户最新要求为：**币安 U 本位市场中 USDT 计价的全部交易中合约，仅正式监测 5m、15m**，包含加密资产、股票/ETF、港股、A股、韩股、商品、指数等，以及永续和交割合约。不再限制加密资产，也不再包含 USDC、USD1、U、BTC 报价。后台仍接收 1m 已收盘行情，目的只是按 UTC 边界合成 5m/15m；1m 不进入正式信号周期。此前下载的其他报价和其他周期历史均保留，但不再补齐、修复或评分。详见 [USDT 全类别范围说明](backend/SCOPE_USDT.md)。

这是本机预览版。后端负责公开行情采集和信号计算，网页负责筛选、查看图表和评分，也可安全保存 Bot Token、连接 Telegram 并生成一次性绑定链接。**Telegram 只发纯文字监测提醒，不会下单，不需要交易 API Key。**

## 打开看板

本机看板启动方法见下方“Windows 一键安装与启动”。`打开看板.url` 只负责打开页面；真正开始采集请双击 `一键启动.bat`，退出请双击 `一键关闭.bat`。

- 网页看板：[http://127.0.0.1:5173](http://127.0.0.1:5173)
- 后端状态：[http://127.0.0.1:8765/api/status](http://127.0.0.1:8765/api/status)
- 接口说明：[http://127.0.0.1:8765/docs](http://127.0.0.1:8765/docs)

先切换到**演示模式**检查四种组合、筛选条件、K 线图和评分明细，再切回实时模式检查连接状态。默认规则下，演示提供 8 个合成样本，覆盖 5m/15m × 四组合；网页演示样本固定不变，修改真实规则仅影响之后的新信号。

**演示样本由真实信号引擎计算，但价格、成交额和持仓量都是固定种子生成的虚构数据，不代表当前行情、历史回测或收益。演示数据不写入真实行情数据库，实时连接失败也不会自动拿演示数据冒充。**

## 已实现的范围

| 项目 | 当前行为 |
| --- | --- |
| 四种 K 线组合 | 阳阳、阴阴、阳阴、阴阳；默认全部监测，可只看混合组合 |
| 正式信号周期 | 5m、15m；以 UTC 对齐。1m 仅作内部实时合成源，不生成 1m 信号 |
| 正式信号 | 只接受连续、已收盘 K 线；历史不足、缺口或未收盘时拒绝 |
| 波动门槛 | 两根标准化真实波幅之和，达到历史基准的 1.5 倍及以上 |
| 强信号收件箱 | 默认只显示通过所选指标、方向变化、量能、流动性、成本后空间与盈亏比的候选；原始异常保留 |
| 分周期门槛 | 5m=2.5、15m=2；均为待验证起点，非最优参数 |
| 指标 | Supertrend（ATR10、3倍）/ SMC 已确认结构 / EMA20/50，选一个或多个；全部或任一确认 |
| 新上架 | 每分钟核对合约元数据，首次快照建基线，待上架与开放交易事件持久去重；只在看板提醒 |
| 回测 | 官方归档校验与 CSV/JSON 导入；次根开盘、双边费用滑点、结构止损优先、70/30时间切分与参数比较 |
| 历史基准 | 默认剔除最高和最低各 2.5% 后求平均；当前两根不进入自己的基准 |
| 活跃度 | 成交额 35 + 价格结构 20 + 主动成交 20 + OI 15 + 流动性 10 |
| 缺失数据 | 缺失项不编造、不补分；OI 不可用时显示可用满分 85，原始总分不放大 |
| 合约范围 | 官方 U_MARGINED 市场、quoteAsset=USDT、TRADING；全部资产类别及永续/交割，未知类别保留标注 |
| 采集与恢复 | 1m WebSocket 仅作内部合成源；REST 只预热和修复 5m/15m，含去重、限速退避与连接状态 |
| 本地保存 | SQLite 保存真实 K 线、信号与 OI 快照；历史回补不会补发成实时信号 |
| Telegram | 默认关闭；一次性链接绑定首位管理员，其他用户须经管理员审批；每位用户独立选择周期、方向、活跃度、信号范围、策略及新上架提醒；只接收连接后的新实时事件 |

十字星默认关闭，可在规则设置中开启。波动倍数和活跃度分数是两个指标；它们都不是买入、卖出或收益预测。

## 当前限制与验收状态

早期连接曾返回 **HTTP 451**；2026-08-31 晚间复验已能从同一官方接口持续下载并接收实时行情。旧七周期口径当时发现 561 个符合范围的合约、3,927 个合约周期，本机库存超过 131 万根；这些数字仅是历史观察值，不是当前 5m/15m 任务总数，也不代表全部历史已下载完或全市场已就绪。当前正式任务总数始终按“实时发现的 USDT 合约数 × 2”计算；仍需完成 5m/15m 预热、原生周期对账和长时间稳定性验收。遇到访问拒绝会如实显示，程序不会切换代理或镜像绕过限制。

直接打开[逐币行情下载](http://127.0.0.1:5173/?view=downloads)，或点击左侧“行情下载与新币”。页面约每 2 秒显示活动任务、逐币 5m/15m 状态、刚落库的批次内容和最新 20 根已存 K 线；全库唯一根数约每 8 秒更新。该全库库存会包含此前保留的非正式周期，不能当作当前两周期任务进度。未收盘报价独立展示，不用于信号判断。点击“继续下载 / 重试失败”保留已有数据与会话，重启程序也会先恢复缓存，只补 5m/15m 缺失区间；CSV 可导出所选正式周期最近 60,000 根。日志是最近批次而非永久审计记录；接收条数可能包含重复覆盖，不能当作净新增库存。

离线测试覆盖计算规则、缺失数据、聚合、历史隔离、去重与恢复等行为。测试通过不等于已经验证全市场长期运行；首次历史预热和 OI 基准积累都需要时间，未就绪的币种与周期不会生成标准信号。

首次基准下载与后续实时增量采集是不同流程。已存历史保留，重启只核对缓存并补当前窗口缺口；本轮分片、缺口恢复和持仓量采样调整，以及真实延迟观察见 [采集性能记录](backend/PERFORMANCE_NOTES.md)。

后端只在进程运行、电脑醒着且网络可用时采集。关闭网页不等于停止后端；关闭进程、关机或休眠会中断采集。**当前未安装开机服务、系统守护、自动备份或 24 小时托管，不作持续在线承诺。** SQLite 和内存任务队列用于本机预览，尚未实现生产级持久化消息队列及崩溃期间信号的完整恢复。

Telegram 已实现私有白名单、管理员审批、每位用户独立筛选、纯文字模板、逐会话限速、429 `retry_after` 重试和持久去重；它没有生产级持久化发送队列，进程异常退出时尚未发送的内存事件可能丢失，重启后也不会把历史信号补发。仍未实现自动交易、代币地址映射和云端行情服务。回测已实现，但 K 线归档不能还原历史 OI、盘口、下架币全市场名单与实际成交；完整评分和新币提醒历史效果尚未获得实证验证。BTCUSDT 5m 的两个月实验样本不足，不支持推荐最优倍数或盈利结论。

## Windows 一键安装与启动

从 GitHub Release 下载 `PULSE-Windows-v1.0.0.zip`，完整解压到普通文件夹后双击 **`一键启动.bat`**。不要直接在压缩包预览窗口内运行。

首次运行会检查 Python 3.11+、Node.js LTS（至少 22.13）、Git 和 GitHub CLI。缺少组件时优先使用 Windows `winget` 安装；若系统没有 `winget`，启动窗口会用中文说明需要手动安装什么。随后程序会自动创建 `.venv`、安装锁定的网页组件，在后台启动 8765/5173 两个本机服务，确认页面可用后打开浏览器。以后启动会复用已经安装的组件。

停止时双击 **`一键关闭.bat`**。关闭脚本只读取 `.runtime` 内本项目写入的 PID，并再次核对命令行中的项目路径；不会按端口结束其他程序。后端和网页的 PID、日志、安装状态都保存在被忽略的 `.runtime` 目录。

如果端口已被其他程序占用，启动会停止并提示，不会擅自结束占用程序。服务默认只监听 `127.0.0.1`；不要直接绑定公网地址或给未认证接口配置公网转发。

### 私有 GitHub 一键更新

看板的“检查更新”只比较版本，不会关闭服务。“下载并更新”只接受 Release 中成对提供的 `PULSE-Windows-vX.Y.Z.zip` 和 `.sha256` 校验文件；资产缺失或 SHA256 不一致时不会安装。更新器在系统临时目录运行，安全关闭本项目后覆盖程序文件，再自动启动新版本；始终保留 `backend/data`、`.runtime`、虚拟环境、Telegram 密钥和本机设置。

因为仓库是私有的，每台接收电脑必须先获得仓库访问权限，并在首次更新前完成一次 GitHub 登录：

```powershell
gh auth login
```

首次启动会安装 GitHub CLI，但不会代替用户登录。登录成功后，后续可直接在看板中检查和安装新版本。开发用 Git 工作区会拒绝被一键更新覆盖；此功能只面向从 Release 解压的安装目录。

发布者须在根目录 `release.json` 同步填写版本、私有仓库、稳定通道和资产名，再运行以下命令生成不含数据和密钥的 Release 资产与 SHA256 文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 -Version v1.0.0
```

完整步骤、备份和故障处理见 [安装与更新说明](安装与更新说明.md)。

### 开发者手动启动

需要前台调试时，仍可分别启动后端和网页：

```powershell
python -m pip install -r backend/requirements.txt
python -m uvicorn monitor.api:app --app-dir backend --host 127.0.0.1 --port 8765
```

```powershell
cd dashboard
npm ci
npm run dev
```

仅做界面演示、暂时不发起币安连接时，可在启动后端的同一个 PowerShell 窗口设置：

```powershell
$env:PULSE_DISABLE_COLLECTOR = '1'
python -m uvicorn monitor.api:app --app-dir backend --host 127.0.0.1 --port 8765
```

恢复采集前将该变量设为 `'0'`，再重启后端。更详细的接口、数据文件和计算约定见 [后端说明](backend/README.md)。

Telegram 可直接在看板中保存并连接。连接成功只验证机器人身份，不会发送测试或行情消息；看板会返回十分钟有效的一次性链接，打开链接并发送 `/start` 后，首位绑定者成为管理员。陌生用户联系机器人时只会创建待审申请，不能打开菜单或使用功能；管理员会收到一次带“通过/拒绝”按钮的通知，也可使用 `/users`、`/pending`、`/add <ID>`、`/remove <ID>` 管理白名单。拒绝或移除后，用户不能靠重复发消息重新申请，只有管理员 `/add` 才能恢复。获批用户的筛选互相独立，信号和上架提醒都从关闭开始，须本人主动开启。程序拒绝所有群组、超级群组和频道消息；仍建议在 BotFather 用 `/setjoingroups` 关闭加群，`/setprivacy` 不是私有访问开关。Bot Token 不进入普通设置、SQLite、GET 响应或日志；Windows 上由当前用户的 DPAPI 加密。也可在启动后端前设置 `PULSE_TELEGRAM_BOT_TOKEN`，环境变量存在时网页不能覆盖它。

## 检查与目录

在项目根目录打开终端，先安装包含 HTTP 接口测试所需 `httpx` 的测试依赖，再进入后端目录运行测试：

```powershell
python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
cd backend
python -m unittest discover -s tests -v
```

| 路径 | 用途 |
| --- | --- |
| `backend/monitor/engine.py` | 四种组合、基准、评分与 UTC 聚合 |
| `backend/monitor/collector.py` | 币安公开行情采集、预热、补齐和状态 |
| `backend/monitor/store.py` | SQLite 存储与信号去重 |
| `backend/monitor/api.py` | FastAPI 本机接口 |
| `backend/monitor/telegram.py` | Telegram 安全绑定、筛选、纯文字模板、限速与去重 |
| `backend/monitor/demo.py` | 独立于真实行情存储的合成演示样本 |
| `backend/data/` | 默认数据库和设置；不要随意删除 |
| `dashboard/` | Sites / Vinext、React 网页看板 |

## 规则与开源参考

信号阈值和评分分档来自本项目的[计划书](<币安 U 本位全币种异常波动监测与 Telegram 实时推送系统计划书.md>)，不是币安官方交易策略。计划书把“去掉两端样本”称作 `winsorized_mean`；实现使用准确名称 `trimmed_mean`，仅兼容旧名称，实际算法始终是剔除两端后求平均。

币安接口和字段依据官方 [USD-M REST 行情文档](https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data)、[WebSocket 连接文档](https://developers.binance.com/en/docs/products/derivatives-trading-usds-futures/websocket-market-streams/Connect)与[市场流文档](https://developers.binance.info/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/ws-streams/market)。

网页使用 [TradingView Lightweight Charts](https://github.com/tradingview/lightweight-charts) 绘制金融图表，使用 [shadcn/ui](https://github.com/shadcn-ui/ui) 的组件基础；卡片层级与信息布局参考 [Tremor 开源看板](https://github.com/tremorlabs/template-dashboard-oss)。第三方声明与许可链接见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

项目分发条件见 [LICENSE.md](LICENSE.md)。其中 SMC 改编部分采用 CC BY-NC-SA 4.0，仅授权符合许可定义的非商业使用；私有仓库并不会取消署名、相同方式共享等义务。计划收费、销售或用于以商业获利为主要目的的场景前，应先取得 LuxAlgo 的单独许可，或移除并独立替换 SMC 部分。


