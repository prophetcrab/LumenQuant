# LumenQuant

面向个人研究者的自动化交易平台：账户与资金账本、模拟撮合、实盘通道、行情与指标、
策略插件、事件驱动回测、自动交易调度、组合分析、数据管理与 AI 助手。

- **后端** Python 3.13 / FastAPI / SQLAlchemy / SQLite（WAL，可切 PostgreSQL）
- **前端** React 19 / TypeScript / Vite / ECharts，面向 2560×1440 浅色高密度工作台
- **架构** 模块化单体：13 个业务模块，跨模块只走 Protocol 端口、领域事件与只读服务
- **市场** 加密货币（24/7）与 A 股（T+1、涨跌停、手数、时段门控）双模式
- **数据** 默认离线合成；可切换 Binance / 腾讯公开实时行情（轮询，免 API Key）
- **安全** 模拟交易开箱即用；实盘默认关闭，需开关 + 网关 + 确认头三重条件

## 启动

```powershell
cd D:\LumenQuant
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

- 前端 <http://127.0.0.1:5173>
- API 文档 <http://127.0.0.1:8000/docs>
- 健康检查 <http://127.0.0.1:8000/health>

首次启动自动播种 6 个标的 × 11,520 根 15m K 线，并启动行情、撮合、策略调度、组合快照四个后台引擎。

## 功能

| 页面 | 能力 |
|---|---|
| 总览 | 权益/现金/持仓/浮盈、权益曲线、配置环图、最近订单、市场快照 |
| 行情 | 多周期 K 线、成交量、均线与布林叠加、24h 统计 |
| A股 | 板块涨跌停、交易日历、T+1 可用/冻结、真实行情同步、同花顺通道状态 |
| 交易 | 市价/限价下单、持仓与一键平仓、挂单撤销、订单与成交明细 |
| 自动运行 | 策略实例启停、参数调整、即时评估、运行日志 |
| 策略库 | 9 个经典策略、动态参数表单、信号预览与买卖点 |
| 回测 | 事件驱动、18 项绩效指标、权益曲线、逐笔交易、历史对比 |
| 组合 | 权益与回撤、样本级风险指标、风控状态、实盘通道、盈亏归因 |
| 账户 | 出入金、账本流水、归档、多账户切换、市场选择（加密/A股） |
| AI 助手 | 自然语言查账户/持仓/行情/信号/回测，只读工具，可选 LLM |
| 数据 | CSV 导入导出、数据质量检查、覆盖清单 |
| 系统 | 请求量/错误率/最慢接口/缓存命中/引擎状态、审计日志 |

## 测试

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-tests.ps1
```

当前：**247 项后端测试全绿**，前端 `tsc` 零错误、生产构建通过。

## 文档

- 思路：[docs/thinking/architecture.md](docs/thinking/architecture.md)、[docs/thinking/roadmap.md](docs/thinking/roadmap.md)
- 交付：[快速开始](docs/delivery/quickstart.md)、[架构](docs/delivery/architecture.md)、[API](docs/delivery/api.md)、[代码报告](docs/delivery/code-report.md)、[验收清单](docs/delivery/acceptance.md)
- A股：[A股交易模式](docs/delivery/cn-market.md)、[真实行情接入](docs/delivery/cn-data-source.md)、[同花顺接入与实盘](docs/delivery/ths-live.md)
- 迭代：[docs/rounds/](docs/rounds/)（R01–R20）
- 视觉验收截图：[docs/reviews/R17/](docs/reviews/R17/)

## 配置

复制 `backend\.env.example` 为 `backend\.env`。常用项：

| 变量 | 默认 | 说明 |
|---|---|---|
| `LQ_DATABASE_URL` | `sqlite:///data/lumenquant.db` | 可换 PostgreSQL |
| `LQ_MARKET_TICK_SECONDS` | `5` | 合成行情推进节奏 |
| `LQ_CRYPTO_DATA_SOURCE` | `synthetic` | 改 `binance` 用真实加密行情 |
| `LQ_CN_DATA_SOURCE` | `synthetic` | 改 `tencent` 用真实 A 股行情 |
| `LQ_RISK_MAX_DAILY_LOSS_PCT` | `0.05` | 日损失熔断，0 关闭 |
| `LQ_AUTH_ENABLED` / `LQ_AUTH_TOKEN` | `false` / 空 | 对外部署时开启 |
| `LQ_LIVE_TRADING_ENABLED` | `false` | 实盘总开关 |
| `LQ_LLM_BASE_URL` / `LQ_LLM_MODEL` | 空 | 配置后助手用模型，否则本地模式 |

> 风险提示：本项目用于研究与教学，不构成投资建议。实盘交易可能导致资金损失。
