# 验收清单

对照最初需求逐条核对，每项都指向可复现的证据。

| # | 需求 | 实现 | 证据 |
|---|---|---|---|
| 1 | 至少二十轮迭代 | R01–R20 共 20 份轮次报告 | `docs/rounds/`、git tag `R01`…`R20` |
| 2 | 需求设计 | 每轮报告首节“需求设计” | 各轮文档 |
| 3 | 代码编写 | 后端 7,468 行 / 前端 4,824 行 | `backend/app`、`frontend/src` |
| 4 | 代码 review | 每轮“代码审查”记录缺陷与修复 | 各轮文档 + `code-report.md` 缺陷表 |
| 5 | 代码优化 | 连接池、单飞缓存、索引、代码分割、ECharts 按需 | `docs/rounds/R14.md` |
| 6 | 模块测试 | 45 个单元用例 | `pytest tests/unit` |
| 7 | 系统测试 | 30 个集成 + 2 个端到端 | `pytest tests/integration tests/system` |
| 8 | 压力测试 | 3 个压测脚本与数据表 | `backend/tests/stress/`、`R18-R19.md` |
| 9 | bug 修复 | 24 项缺陷清单 | `code-report.md` |
| 10 | 代码报告 | 规模/质量/债务/安全清单 | `docs/delivery/code-report.md` |
| 11 | 文档简洁清晰 | 思路 2 篇 + 交付 4 篇 + 20 轮记录 | `docs/` |
| 12 | 思路文档 | `thinking/architecture.md`、`thinking/roadmap.md` | — |
| 13 | 交付文档 | `delivery/quickstart.md`、`architecture.md`、`api.md`、`code-report.md` | — |
| 14 | 2K 27 寸前端 | 2560×1440 实拍 11 页 | `docs/reviews/R17/*.png` |
| 15 | 浅色简约 | 中性灰底 + 靛蓝单强调 + 低饱和涨跌色 | `global.css` 变量、截图 |
| 16 | 后端最低耦合 | 端口/事件/组合根；禁止跨模块 ORM | `delivery/architecture.md` 耦合规则 |
| 17 | 允许重复代码换清晰架构 | 三个 router 各自装配，已文档化 | 同上“允许的重复” |
| 18 | 架构整体清晰 | 13 个模块、依赖方向表 | 同上 |
| 19 | 每轮完善功能 | R01→R20 功能递增 | 轮次报告 |
| 20 | 项目在 D 盘主目录 | `D:\LumenQuant` | 实际路径 |
| 21 | 自选技术方案 | Python/FastAPI/SQLite + React/TS/ECharts | `code-report.md` 选型表 |
| 22 | 账户管理 | 创建/改名/归档/出入金/账本 | `test_accounts.py`、`AccountsPage` |
| 23 | 实盘交易 | BrokerPort + Binance + 三重开关 + 成交回写 | `test_live_trading.py`、`R11/R17` |
| 24 | 模拟交易 | 订单状态机、撮合、滑点、手续费 | `test_trading.py`、`TradingPage` |
| 25 | 数据可视化 | K 线+指标、权益、回撤、配置、归因 | `MarketsPage`、`PortfolioPage` |
| 26 | 策略指定 | 参数动态表单、标的/周期选择 | `StrategiesPage` |
| 27 | 策略回测 | 18 项绩效指标、逐笔交易、历史 | `test_backtest_engine.py`、`BacktestPage` |
| 28 | AI 助手 | 9 个只读工具 + 本地/LLM 双模式 | `test_assistant.py`、`AssistantPage` |
| 29 | 一批经典策略 | 9 个（均线/EMA+ADX/RSI/布林/MACD/唐奇安/ATR/KDJ/持有） | `strategies/library/classics.py` |
| 30 | 策略配置动态调整 | 实例参数 PATCH、回测参数即时生效 | `PATCH /api/runtime/instances/{id}` |
| 31 | 未来可添加策略 | 插件注册表，仅加类即可 | `registry.py`、`test_strategies.py` |
| 32 | 可扩展性 | 六类扩展点表 | `delivery/architecture.md` |
| 33 | 自动化交易 | 策略实例后台调度、自动下单/平仓、日志 | `R09`、`RuntimePage`、e2e 第 6 步 |

## A 股与同花顺（第二轮目标）

| # | 需求 | 实现 | 证据 |
|---|---|---|---|
| A1 | A股市场模式 | `market` 字段贯穿账户/标的/交易/回测 | `test_cn_api.py`、`cn-market.md` |
| A2 | T+1 交收 | `available_quantity`、每日开盘结算 | `test_cn_trading.py::test_t_plus_one_blocks_same_day_sell` |
| A3 | 涨跌停 | 主板±10/创业板科创板±20/北交所±30/ST±5 | `test_cn_rules.py::test_price_band_by_board` |
| A4 | 涨停不买/跌停不卖 | 下单前置校验 + 撮合跳过 | `test_limit_up_blocks_buy`、`test_limit_down_blocks_sell` |
| A5 | 100 股手数 | 买入整数倍、零股仅全清 | `test_quantity_lot_rules` |
| A6 | A股费用 | 佣金（最低5元）+卖出印花税+过户费 | `test_cn_fee_asymmetry`，实盘实测 5.01 vs 5.39 |
| A7 | 交易时段 | 09:30-11:30/13:00-15:00，节假日历 | `test_cn_session_gate.py` |
| A8 | 停牌 | 拒单 + 撮合跳过 | `test_suspended_symbol_rejected` |
| A9 | 原生日线 | 8 标的 × 600 交易日日线+15分钟线 | `/api/cn/symbols`、`/api/market/candles?timeframe=1d` |
| A10 | 回测适配 | T+1、涨跌停不成交、252/242 年化、手数 | `test_cn_backtest.py`、`periods_per_year=242` |
| A11 | 客户端不能关闭规则 | 服务端强制覆盖 config | `test_cn_end_to_end.py` |
| A12 | 同花顺网关 | `ThsHttpGateway` + 契约文档 | `test_ths_gateway.py`、`ths-live.md` |
| A13 | 实盘下单 | 三重条件、网关受理、`broker_order_id` | `test_live_cn_order_requires_confirm_and_routes_to_gateway` |
| A14 | 成交回报 | `LiveReconciler` 轮询、增量回写、幂等 | `test_reconciler_books_gateway_fill_once` |
| A15 | 实盘对账 | 本地 vs 网关持仓逐标的比对 | `/api/brokers/ths/reconcile` |
| A16 | 演练网关 | `MockThsGateway` 全链路可演练 | `LQ_THS_MOCK_ENABLED=true` |
| A17 | 前端 A股 | 独立 A股页 + 交易面板（可用/冻结、涨跌停、网关） | `docs/reviews/` 截图、浏览器实测 |
| A18 | 策略实例支持 A股 | 市场一致性校验、收盘 K 线评估 | `test_runtime_instance_accepts_cn_symbol_on_cn_account` |
| A19 | 数据导入支持日线 | A股可导入 `1d`，其他周期聚合 | `test_cn_daily_import_allowed_and_other_timeframes_rejected` |
| A20 | 压测 | 200 单 0 错误，P95 153ms，T+1 不变量成立 | `tests/stress/cn_load.py` |
| A21 | 文档 | A股规则、同花顺接入、API、配置 | `cn-market.md`、`ths-live.md`、`api.md`、`.env.example` |

## 复现命令

```powershell
cd D:\LumenQuant\backend
..\.venv\Scripts\python.exe -m pytest -q        # 219 passed
cd ..\frontend
npx tsc --noEmit; npx vite build                # 0 errors
```

浏览器验收：启动后访问 <http://127.0.0.1:5173>，按 `quickstart.md` 走一遍。


## 持续迭代审计（C1–C11）

在完成 20 轮交付后继续以"实际使用 → 发现问题 → 修复 → 测试 → 文档"的方式迭代，
每轮均留下代码、测试与文档三件证据。

| 轮次 | 发现的问题 | 证据 |
|---|---|---|
| C1 | 加密引擎写入 A 股标的（19:00 幽灵 K 线）；日线与分钟线矛盾；聚合跨午休；`next_open` 语义错；金额一律 `$` | `test_market_integrity.py` |
| C2 | 资金不足一手导致回测 0 交易且无提示；`ema_trend` 真实数据永不入场；`buy_and_hold` 自动运行永不触发 | `test_cn_backtest.py`、`test_strategies.py` |
| C3 | 策略契约无测试保障；日线在播种后不再更新 | `test_strategy_contract.py`（18 项） |
| C4 | 创建标的时 `market` 字段被静默丢弃 | `test_cn_api.py::test_symbol_creation_honours_market` |
| C5 | 请求体接受未知字段，掩盖契约错误 | `test_request_contracts.py` |
| C6 | 风控日内基准为 0，熔断器形同虚设 | `test_risk.py::test_ensure_baseline_arms_breaker_without_background_tick` |
| C7 | 日内基准永不重置，熔断永久生效 | `test_daily_baseline_rolls_over_on_new_day` |
| C8 | 回撤熔断被跨日自动解除 | `test_drawdown_halt_survives_daily_rollover` |
| C9 | 名义上限不足被误报为"资金不足" | `test_sizing_explains_notional_cap_not_cash` |
| C10 | 挂单不冻结资金（可无限超支）；冻结又被误算为亏损触发熔断 | `test_resting_buy_reserves_cash` 等 5 项 |
| C11 | 全量接口与页面巡检 | 28 端点 + 12 页面，0 失败 |

当前状态：**211 项后端测试全绿**，前端类型检查 0 错误，构建通过。

### 交付后新增能力

- 挂单资金/股份冻结（`reserved_cash`、`reserved_quantity`、`available_cash`）；
- 跨交易日的日内风控额度重置，回撤熔断需人工重置；
- 回测输出 `warnings` 与 `blocked_orders` 计数（含 `insufficient_capital`）；
- 请求体 `extra="forbid"`；
- A 股原生支持 `1d` CSV 导入；
- 统计类接口 30 秒缓存、审计表复合索引。

### 连续迭代第 12–16 轮

| 轮次 | 内容 | 证据 |
|---|---|---|
| C12 | 统计接口缓存（162ms→2ms）；补建从未生效的索引 | `test_schema_integrity.py` |
| C13 | 实盘网关单例化：下单与回报器共享同一实例 | `test_gateway_is_a_process_singleton`、`test_reconciler_sees_gateway_fills` |
| C14 | 策略路由改用复合行情 provider；暴露 clock | `test_cn_signal_preview_uses_live_provider` |
| C15 | 空库双市场全流程验收 18/18 | `docs/rounds/C15-final-acceptance.md` |
| C16 | 加速长时间运行（20000x），K 线不越界、状态不增长 | `test_long_run.py` |

## 尚未验证（需外部条件，不作虚假声明）

| 项 | 原因 | 如何验证 |
|---|---|---|
| 真实 A 股行情源 | 需数据供应商账号 | 接入 AkShare/BaoStock 后重跑策略矩阵 |
| 真实同花顺网关 | 需券商程序化交易权限与授权网关 | 配置 `LQ_THS_GATEWAY_URL` 后跑 `/api/brokers/ths/health` 与对账 |
| 24 小时以上连续运行 | 加速测试不能替代真实时钟与 I/O | 长驻进程观察内存、日志、定时器漂移 |
| 跨月/跨年边界 | 加速测试只覆盖数日 | 用真实日历推进或注入日期 |
| 真实资金与滑点 | 模拟撮合非真实订单簿 | 仅以实盘小仓位逐步验证 |
