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

## 复现命令

```powershell
cd D:\LumenQuant\backend
..\.venv\Scripts\python.exe -m pytest -q        # 119 passed
cd ..\frontend
npx tsc --noEmit; npx vite build                # 0 errors
```

浏览器验收：启动后访问 <http://127.0.0.1:5173>，按 `quickstart.md` 走一遍。
