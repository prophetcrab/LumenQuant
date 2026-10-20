# 交付：架构与模块说明

## 技术选型

| 层 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.13 | 量化生态成熟，向量/统计库丰富 |
| Web | FastAPI + Starlette | 自带 OpenAPI、依赖注入和中间件链 |
| 持久化 | SQLite（WAL）→ 可切 PostgreSQL | 零运维启动；SQLAlchemy 保持迁移可能 |
| 前端 | React 19 + TypeScript + Vite | 类型安全、热更新快、构建产物小 |
| 图表 | ECharts（按需注册） | K 线/面积/环图齐全，tree-shaking 后 654KB |
| 状态 | TanStack Query | 轮询、缓存、失效语义清晰 |
| 样式 | 原生 CSS 变量 + 语义类 | 无运行时依赖，2K 下可控 |

## 模块边界

```
app/
├─ core/            共享内核：配置、DB、缓存、事件、错误、审计、安全、观测
├─ api/             组合根：依赖、路由聚合
└─ modules/
   ├─ accounts/     账户与资金账本
   ├─ market/       标的、K线、合成行情、指标、聚合
   ├─ trading/      订单、撮合、持仓、成交、实盘路由
   ├─ risk/         下单前策略 + 账户级熔断
   ├─ strategies/   策略插件契约、注册表、9 个内置策略
   ├─ backtest/     事件驱动回测与绩效
   ├─ portfolio/    权益快照、组合视图、分析、归因
   ├─ runtime/      策略实例、自动运行、日志
   ├─ assistant/    只读工具集 + 本地/LLM 双模式
   ├─ brokers/      交易所适配器（Binance）
   ├─ data/         CSV 导入导出与数据质量
   └─ system/       指标与审计查询
```

### 耦合规则
1. 模块不得导入其他模块的 ORM 模型（`trading` 用 `ports.py` 的 Protocol）。
2. 跨模块写操作只能通过对方 Service 的事务内方法（如 `accounts.post_cash`）。
3. 领域事件用于解耦副作用（`OrderFilled`）。
4. 组合根（`main.create_app` / 各 `router.build_service`）负责装配。

### 允许的重复
为保持边界清晰，`trading/router.py`、`runtime/router.py`、`assistant/router.py` 各自构造
`AccountService`/`MarketService`/`TradingService`。这份装配代码是刻意重复的：
抽成共享工厂会把三个模块重新耦合成一个“上帝对象”。

## 数据流

**手动交易**
```
POST /api/trading/orders
  → 风控熔断检查 → 限额/手数/资金/持仓校验
  → 模拟: 撮合 → 写 order/fill/position/ledger（单事务）
  → 实盘: BrokerPort → 用真实成交回写本地
  → 发布 OrderFilled
```

**自动交易**
```
StrategyRuntimeEngine (每 6s)
  → 取运行中实例 → 读最近 600 根「已收盘」K 线
  → prepare/evaluate → 信号
  → buy: 按仓位比例与名义上限 → lot 量化 → 市价单
  → sell: 平掉该标的持仓
  → 写 RuntimeLog + 游标 last_bar_time（同 K 线幂等）
```

**组合**
```
PortfolioEngine (每 15s)
  → 逐活跃账户记录权益快照（含净投入）
  → 喂给熔断器（日损失/回撤）
  → 前端轮询 /portfolio/overview、/history、/analytics
```

## 关键不变量
| 不变量 | 保障方式 | 验证 |
|---|---|---|
| 现金 = 账本求和 | 现金从不单独存储 | `test_end_to_end`、压测对账 |
| 权益 = 现金 + 持仓市值 | 单一计算函数 | e2e 断言、压测断言 |
| 成交必有订单 | 同一事务写入 | 压测 `fills_cover_orders` |
| 同一 K 线只执行一次 | `last_bar_time` 游标 | `test_entry_executes_once_per_bar` |
| 入金不算收益 | 净投入 + 时间加权 | `test_deposit_is_not_counted_as_return` |
| 回测无未来函数 | 收盘信号、次根开盘成交 | `test_signals_execute_on_next_bar_open` |

## 扩展点

| 想加什么 | 改哪里 | 不改什么 |
|---|---|---|
| 新策略 | `strategies/library/*.py` + `@register` | 回测、运行时、前端 |
| 新交易所 | 实现 `BrokerPort` + `build_broker` | 交易与风控 |
| 新行情源 | 替换 `MarketEngine` 的写入路径 | 读取服务、策略、回测 |
| 新指标 | `market/indicators.py` + `compute()` | 策略只需调用 |
| 新 AI 工具 | `assistant/tools.py::_register` | 对话服务、前端 |
| 换数据库 | `LQ_DATABASE_URL` | 所有模块代码 |

## 已知限制
1. 单机单进程；限流与指标为进程内状态。
2. 只支持多头现货（无做空、无杠杆保证金模型）。
3. 实盘走 Binance 现货 REST，未实现 WebSocket 用户数据流。
4. 盈亏归因基于当前持仓，已完全平仓的标的不会出现在归因表。
5. 轻量迁移只支持加列，不做破坏性变更。
