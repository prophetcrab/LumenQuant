# 验收清单

对照最初需求逐条核对，每项都指向可复现的证据。

| # | 需求 | 实现 | 证据 |
|---|---|---|---|
| 1 | 至少二十轮迭代 | R01–R20 共 20 份轮次报告 | `docs/rounds/`、git tag `R01`…`R20` |
| 2 | 需求设计 | 每轮报告首节“需求设计” | 各轮文档 |
| 3 | 代码编写 | 后端 15,148 行 / 前端 9,261 行（不含测试） | `backend/app`、`frontend/src` |
| 4 | 代码 review | 每轮“代码审查”记录缺陷与修复 | 各轮文档 + `code-report.md` 缺陷表 |
| 5 | 代码优化 | 连接池、单飞缓存、索引、代码分割、ECharts 按需 | `docs/rounds/R14.md` |
| 6 | 模块测试 | 237 个单元用例 | `pytest tests/unit` |
| 7 | 系统测试 | 172 个集成 + 3 个端到端 | `pytest tests/integration tests/system` |
| 8 | 压力测试 | 5 个脚本（HTTP 压测、策略吞吐、跨年 soak、日历校验） | `backend/tests/stress/`、`C34.md` |
| 9 | bug 修复 | 24 项缺陷清单 | `code-report.md` |
| 10 | 代码报告 | 规模/质量/债务/安全清单 | `docs/delivery/code-report.md` |
| 11 | 文档简洁清晰 | 思路 2 篇 + 交付 9 篇 + 20 轮记录 + 46 份迭代记录 | `docs/` |
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
| 28 | AI 助手 | 10 个只读工具 + 本地/LLM 双模式 + 市场感知 | `test_assistant_api.py`、`AssistantPage` |
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
..\.venv\Scripts\python.exe -m pytest -q        # 247 passed
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

### 真实行情接入（C17–C18）

| 项 | 状态 | 证据 |
|---|---|---|
| 可插拔数据源（synthetic/tencent） | ✅ | `app/modules/cn/sources/` |
| 实时报价、日线、15 分钟线 | ✅ | 腾讯三接口，实测 8 标的 |
| 启动时同步（失败不阻塞） | ✅ | `_startup_sync` |
| 数据滞后期可查 | ✅ | `/api/cn/data/staleness`，实测 stale_days=0 |
| 同步即替换（避免口径混合） | ✅ | `test_sync_replaces_synthetic_history` |
| 退避重试（4xx 不重试） | ✅ | 3 项重试用例 |
| 复权口径统一为不复权 | ✅ | `test_daily_endpoint_uses_unadjusted_series` |
| 报价与日线同尺度 | ✅ | `test_quote_and_daily_share_the_same_price_scale` |

### 双市场实时行情（C74）

| 项 | 状态 | 证据 |
|---|---|---|
| 批量报价（一次请求取多只） | ✅ | `TencentSource.quotes()` / Binance `symbols=[...]`；实测 8 只 300–400ms |
| 盘中 15s / 休市降频轮询 | ✅ | `CnQuoteEngine._next_delay`，`/api/cn/data/source` 可见 |
| 失败指数退避 | ✅ | 加密上限 60s、A 股上限 300s；启动 45s 内 0 次失败日志 |
| 实时价优先于重建 bar | ✅ | `test_snapshot_prefers_the_exchange_quote` |
| A 股日线直接由报价写入（含累计量） | ✅ | `test_daily_bar_is_written_straight_from_the_quote` |
| 加密收盘用 klines 校正真实成交量 | ✅ | `test_closed_bar_uses_the_authoritative_kline_volume` |
| 未收盘 bar 与导入 bar 合并而非替换 | ✅ | `_merge_partial_bar`，图表最后一根跟随实时价 |
| 过期报价判为不新鲜而非照常展示 | ✅ | `test_stale_quote_is_reported_instead_of_served_as_live` |
| 昨收变更后涨跌停跟着变 | ✅ | `test_quote_poll_updates_prev_close_for_the_next_limit_band` |
| 真实模式下不再运行合成引擎 | ✅ | `test_real_data_mode_replaces_the_synthetic_engine` |
| 合成模式下接口明确报错 | ✅ | `test_poll_and_quotes_fail_clearly_in_synthetic_mode` |
| 函数内 import 禁止遮蔽模块名 | ✅ | `tests/unit/test_import_shadowing.py` |
| 回测/信号排除未收盘 bar | ✅ | `tests/unit/test_closed_only_bars.py`，同一回测两次结果一致 |
| 收盘集合竞价写入日线 | ✅ | `test_closing_auction_price_replaces_the_last_in_session_write` |
| 盘前不造幽灵日线 | ✅ | `test_pre_open_quote_does_not_create_a_phantom_daily_bar` |
| 行情中断自动告警、只报一次、恢复自动确认 | ✅ | `test_feed_outage_raises_one_alert_and_recovery_clears_it` |
| 平台级告警对每个账户可见 | ✅ | `test_platform_wide_alert_is_visible_to_every_account` |
| 权益快照两级降采样 | ✅ | `tests/unit/test_snapshot_pruning.py`；实测 21938 → 124 行 |
| 组合分析检测异常跳变并给出稳健相关性 | ✅ | 真实数据：1 根 +73% bar 使相关性 0.006 → 0.230，剔除后复原 0.032 |
| 按调整后权重重跑篮子 | ✅ | `test_apply_weights_reruns_the_basket_with_the_new_mix`；浏览器实测 60/30/10 → 收益 57.91% vs 等权 32.13% |
| 告警时间范围与跨账户合计 | ✅ | `test_alerts_can_be_filtered_by_time_window`、`test_summary_can_total_across_all_accounts`；浏览器实测切换账户与时间窗 |
| A 股 bar 的收盘判定用 A 股时钟 | ✅ | `test_a_share_bar_is_judged_by_the_a_share_clock`；还原修复后失败 |
| 轮询中断后补历史并修正量 0 的坏行 | ✅ | `test_outage_gap_is_backfilled_instead_of_persisting_a_fake_bar`；实测 7 根全部带真实量 |
| 全新部署自动导入 A 股历史 | ✅ | 实测 8968 根 / 8 标的，回测 422 → 201 |
| 节假日覆盖范围外不误报数据缺口 | ✅ | `test_daily_gaps_are_not_reported_outside_the_calendar_years`；真实 2023 数据 4 → 0 |
| 助手工具数值自带市场与币种 | ✅ | `test_snapshot_tool_labels_market_and_currency` 等 4 项；A 股标「较昨收」、加密标「近 24 小时」 |
| LLM 模式按账户市场补齐标的 | ✅ | `test_llm_tool_calls_get_the_account_market_symbol`；还原注入逻辑后失败 |
| 持仓限额按市价计算敞口 | ✅ | `test_position_limit_uses_market_value_not_cost`；还原后 DID NOT RAISE |
| A 股当日买入计入敞口但不可卖 | ✅ | `test_same_day_a_share_position_counts_toward_the_notional_limit`；实测第二笔 409、当日卖出 409 |
| 限价单按最坏成交价计算名义金额 | ✅ | `test_limit_order_notional_uses_the_worse_of_limit_and_market`；实测 0.1 BTC 市价/限价均 409 |
| 满仓下单为手续费预留额度 | ✅ | `test_full_budget_sizing_reserves_the_fee`；实测 A 股 30 万预算买入 200 股含费后仍有余额 |
| 平仓不受未成交挂单阻塞 | ✅ | `test_close_position_cancels_resting_sells_first`；实测持仓 1.0+挂 0.4 时平仓 200 filled=1.0 |
| A 股下单界面明确说明禁用原因 | ✅ | 2K 浏览器实测：可卖 0 时提示 T+1，资金标注「可立即用于买入」 |
| 集合竞价时段不生成伪日线 | ✅ | `test_opening_auction_quote_does_not_create_a_daily_bar`；收盘 15:00 竞价仍被接受 |
| 无 K 线时仍给出涨跌停 | ✅ | `test_cn_snapshot_keeps_limit_band_without_any_candles`、`test_gem_board_uses_a_twenty_percent_band` |
| 停牌状态随行情下发 | ✅ | `test_snapshot_reports_the_suspended_flag`；停牌时仍返回最后成交价与涨跌停 |
| 跨日后 `prev_close` 滚动更新 | ✅ | `test_prev_close_rolls_to_the_previous_trading_day_close`；还原后失败 |
| 15m 采样值被交易所权威值覆盖 | ✅ | `test_authoritative_bar_replaces_a_sampled_one_with_nonzero_volume`；端点故障时不降级 |
| 未来时间戳不阻塞真实历史导入 | ✅ | `test_future_dated_bars_are_not_treated_as_fresh`；重启后 6 标的各导入 2000 根 |
| 全市场 A 股选股 | ✅ | 实测 5,573 只全市场、9.5s；6 个预设、12 项单元 + 8 项集成测试 |
| 选股结果可直接入池 | ✅ | `POST /api/screener/add-symbol`，板块从代码推断 |
| 实盘对账包含资金 | ✅ | `test_reconcile_reports_a_cash_difference`；本地 50 万 vs 网关 100 万被判为差异 |
| 选股标的加入即可回测 | ✅ | `test_add_symbol_imports_history_so_it_can_be_backtested`；实测 800 日线、回测 201 |
| 测试不受 .env 真实数据配置影响 | ✅ | .env 配 tencent+binance 时全量 496 项仍在 2.5 分钟内通过 |

**真实网络实测（全新数据库，双市场）**：`crypto: 6 symbols / 5s / 702ms`、
`cn: 8 quotes / 15s / 405ms`；CN 日线量 33514 与报价累计量完全一致；
BTC 15m 最后一根带真实成交量 16.08；A 股模拟市价单成交价 1309.69 与同刻报价一致，
T+1 冻结 `available_quantity=0`；`/api/market/clock` 的 `cn` 段报告 `simulated: false`。

**真实数据下的策略表现（平安银行 120 日线，50 万资金）**：
收益区间 -9.44% ~ +11.15%，有正有负；对比修复前的虚假 +70% 区间，
说明数据口径已正确。

**新增交付文档**：`docs/delivery/cn-data-source.md`（模式、配置、接口、
数据语义、已知限制）。

### 真实数据持续迭代（C19–C23）

| 轮次 | 内容 | 证据 |
|---|---|---|
| C19 | 真实行情下的交易闭环（T+1、非对称费用、权益守恒） | 实测记录 |
| C20 | 真实数据暴露 ema_trend 胜率 4%，加牛熊过滤后 -28%→-6.8% | `test_ema_trend_regime_filter_avoids_bear_market_entries` |
| C21 | 过滤非交易日 K 线、滞后期非负 | `test_sync_drops_non_trading_day_bars` |
| C22 | 量化后仅对 sma_cross/donchian 提供可选过滤；暖机不足提示原因 | `test_regime_filter_defaults_differ_by_strategy` |
| C23 | 布尔参数前端渲染修复（原被 `Number()` 抹平） | `test_boolean_strategy_param_is_accepted` |

**真实数据策略矩阵（800 根日线）**：招商银行（上涨市）基准 +28.2%，
ema_trend +20.2%；茅台（下跌市）基准 -6.3%，多数趋势策略亏损、
均值回归类为正。结论与市场结构一致。

### 持续迭代阶段（C24–C47）

C 轮以"实际使用系统"为主，每轮都从真实操作中找缺陷。完整清单见 `docs/rounds/`。

| 主题 | 轮次 | 关键交付 |
|---|---|---|
| 研究工具链 | C24–C26 | 参数优化（IS/OOS）、滚动验证、基准对比（alpha/beta/IR） |
| 止损一致性 | C27 | `risk/stops.py` 共享逻辑，回测与实盘语义统一 |
| 风险中心 | C28 | `/api/risk/overview` 聚合接口 + 独立页面 |
| 参数交接 | C29–C30 | 策略库一键带去回测 / 建成自动实例；实例可编辑 |
| 组合分析 | C31 | 跨回测合并、相关性矩阵、分散收益 |
| 真实数据验收 | C32 | 纯真实数据部署修复；助手接入风控 |
| 跨日边界 | C33 | T+1 释放修复；节假日表覆盖范围显式化 |
| 压测与 soak | C34 | 5 个压测脚本；运行日志去重与上限 |
| 前端测试 | C35 | Vitest + Testing Library，54 项 |
| 研究链路 | C36 | 滚动验证丢窗口、结构性告警被覆盖、优化器漏报衰减 |
| 信号一致性 | C37 | 预览套用持仓状态，与回测成交语义对齐 |
| 数据质量 | C38 | 缺口按日历计算；用真实数据反向校验发现 2026 春节少休一天 |
| 实盘安全 | C39–C40 | 自动运行独立开关 + 每轮复查；前端实盘开户入口 |
| 助手健壮性 | C41, C46 | 无账户时给指引而非 500；按账户市场选默认标的 |
| 币种一致性 | C42 | 图表 tooltip 跟随市场；后端返回 currency |
| 归档语义 | C43–C44 | 归档即停止实例；确认弹窗列出后果 |
| 数据管理 | C45 | 按标的/周期删除 K 线 |
| 多账户 | C48 | 切换器显示市场；按币种分组合计（不做汇率换算） |
| 全链路冒烟 | C49 | 13 页加载检查；修正净投入币种与成交量标签 |
| 市场隔离 | C50–C51 | 交易页/回测页标的与周期按市场过滤 |
| 真实数据验收脚本 | C52 | `tests/stress/real_data_e2e.py`，10 项断言 |
| 缓存一致性 | C53 | 导入/删除/同步统一失效 K 线、快照与质量缓存 |
| 周期与调度健壮性 | C54 | A 股无 4h；坏实例不再拖垮整轮调度 |
| 网关故障 | C55 | 下单/撤单失败落库为 rejected 并返回 409；对账器继续遍历 |
| 资金语义一致性 | C56–C57 | 仓位按可用现金；A 股卖出资金 T+1 到账（实盘与回测同步实现） |
| 多标的回测 | C61–C63 | 一篮子标的按权重拆分、等权基准、定期再平衡、结果持久化 |
| 告警中心 | C64–C68 | 熔断/离场/下单失败自动告警，去重、确认、保留策略、类别过滤、侧栏角标 |
| 篮子接入组合分析 | C69–C70 | 多标的回测的 sleeve 落成普通回测，可一键导入相关性分析；删除时级联清理 |
| 异常跳变检测 | C72–C73 | A 股在途资金未计入权益（-95%/+1785% 单根跳变）修复；引擎与多标的合并曲线均自动告警 |
| 实时行情 | C74 | 双市场接入真实数据（腾讯 + Binance）；批量轮询、退避、新闻度、15m 重建、成交量口径、半根 bar 合并 |
| 组合分析稳健性 | C75 | 相关性矩阵检测异常跳变，同时给出剔除后的稳健矩阵与扭曲程度 |
| 权重落地 | C76 | 组合分析可按调整后的权重重跑篮子，生成新记录并保留原篮子 |
| 告警可观测性 | C77 | 时间范围筛选、跨账户合计与明细；侧栏角标改为全局口径 |
| 时间基准与缺口 | C78 | A 股 bar 改用 A 股时钟判定收盘；加密轮询中断后从 klines 补历史并修正坏行 |
| 助手数值语义 | C79 | 行情/账户工具返回市场与币种，A 股涨跌改标「较昨收」，本地文案按币种分组 |
| 持仓限额 | C80 | 敞口改按市价；A 股当日买入计入敞口；限价单按最坏成交价计算名义金额 |
| 建仓预算 | C81 | 自动运行按含费金额计算数量，满仓配置不再必然落空 |
| 平仓可用量 | C82 | 平仓前撤销同标的卖单，修复挂单永久阻塞止损的问题 |
| 界面文案 | C83 | 可用资金不再标注 T+1；卖出/买入被禁用时说明具体原因 |
| 竞价时段防护 | C84 | 显式时间守卫替代对 volume>0 的间接依赖（加固，非缺陷修复） |
| 涨跌停可见性 | C85 | 无行情时按昨收算涨跌停，不再整行为 0；板块限幅按板块取 |
| 停牌可见性 | C86 | Snapshot 增加 suspended 字段，助手与两个行情接口同步下发 |
| 涨跌停基准 | C87 | 交易日切换时把 `prev_close` 更新为上一交易日收盘（此前从不更新） |
| 15m 精度 | C88 | 权威 K 线无条件覆盖采样值；端点故障时不把精确行降级 |
| 数据源切换 | C89 | 修复未来时间戳阻塞历史导入；测试强制离线，不受 .env 影响 |
| A股选股 | C90 | 新浪全市场快照、并发拉取、6 个预设、估值/成交/板块筛选、一键入池 |
| 实盘对账 | C91 | 资金纳入对账；网关未报告余额时报「未获取」而非判为一致 |
| 选股闭环 | C92 | add-symbol 默认同步历史，区分成功/失败/合成模式三种结果 |

**当前规模**：后端 519 项测试、前端 66 项测试，全部通过。

**验证方式**：除自动化测试外，每轮都在真实浏览器中操作 2K 界面确认，
并在真实 A 股行情（腾讯数据源）上跑通同步 → 回测 → 优化 → 滚动验证 → 组合分析。
