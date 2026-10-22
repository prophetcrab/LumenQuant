# API 摘要

Base URL：`http://127.0.0.1:8000/api`　完整交互式文档：`/docs`

错误统一为 `{"error": {"code": "...", "message": "..."}}`，常见状态码：
`400/409` 业务冲突、`403` 被禁用、`404` 不存在、`422` 参数非法、`429` 限流、`502` 上游故障。

## 系统
- `GET /health`（无需认证）
- `GET /api/meta` — 已启用模块与能力
- `GET /api/system/metrics` — 请求量、错误率、最慢路由、缓存命中、引擎状态
- `GET /api/system/audit?action=&limit=`
- `GET /api/system/audit/summary`

## 账户
- `GET /api/accounts?include_archived=`
- `POST /api/accounts` — `{name, kind: paper|live, initial_balance, base_currency, leverage}`
- `GET /api/accounts/{id}`
- `PATCH /api/accounts/{id}` — `{name?, status?, leverage?}`
- `POST /api/accounts/{id}/deposit`、`POST .../withdraw` — `{amount, note?}`
- `GET /api/accounts/{id}/ledger?limit=`

## 行情
- `GET /api/market/symbols`、`POST /api/market/symbols`、`PATCH /api/market/symbols/{code}`
- `GET /api/market/candles?symbol=&timeframe=15m|30m|1h|4h|1d&limit=`
- `GET /api/market/indicators?symbol=&timeframe=&limit=&specs=sma:20,macd:12:26:9,`
- `GET /api/market/snapshot?symbol=`、`GET /api/market/snapshots`
- `GET /api/market/clock`

## 交易
- `POST /api/trading/orders` — `{account_id, symbol, side, order_type, quantity, limit_price?, client_order_id?}`
  - 实盘账户额外需要请求头 `X-Live-Confirm: true`
- `GET /api/trading/orders?account_id=&status=&limit=`
- `DELETE /api/trading/orders/{id}?account_id=`
- `GET /api/trading/positions?account_id=`
- `GET /api/trading/fills?account_id=&limit=`
- `POST /api/trading/positions/{symbol}/close` — `{account_id}`
- `POST /api/trading/match` — 手动触发一次撮合

## 策略
- `GET /api/strategies` — 目录（含 `params_schema` 与默认值）
- `GET /api/strategies/{key}`
- `POST /api/strategies/{key}/signals` — `{symbol, timeframe, limit, params}`

## 回测
- `POST /api/backtests` — `{strategy, symbol, timeframe, bars, params, config}`
- `GET /api/backtests?limit=`、`GET /api/backtests/{id}`、`DELETE /api/backtests/{id}`
- `POST /api/backtests/optimise` — 网格搜索 + 样本外检验
- `POST /api/backtests/walk-forward` — 多窗口滚动验证
- `POST /api/backtests/multi` — `{strategy, symbols[], timeframe, bars, params, config, weights?, rebalance_bars?}`
  同一策略跑一篮子标的：资金按权重拆分，各 sleeve 独立回测后合并成一条曲线；
  要求标的同市场、同周期。`rebalance_bars>0` 时每 N 根恢复目标权重
  （假设份额可分割，不计调仓费用）。运行结果自动保存，
  同时为每个标的写一条普通回测（`origin="multi"`）供组合分析使用。
- `GET /api/backtests/multi?limit=`、`GET /api/backtests/multi/{id}`、`DELETE /api/backtests/multi/{id}`
  — 多标的回测的历史、完整结果与删除。
- `POST /api/backtests/portfolio` — `{run_ids[], weights?, initial_capital?}`
  合并已保存回测：对齐权益曲线、计算合并指标、成分贡献、相关性矩阵与分散效果。
  要求周期与市场一致，重叠区间至少 50 根。

## 自动运行
- `GET /api/runtime/instances?account_id=`
- `POST /api/runtime/instances` — `{account_id, strategy_key, symbol, timeframe, params, position_pct, max_order_notional, start}`
- `PATCH /api/runtime/instances/{id}` — `{params?, position_pct?, max_order_notional?, stop_loss_pct?, take_profit_pct?, trailing_stop_pct?}`（策略/标的/周期不可改）
- `POST /api/runtime/instances/{id}/toggle` — `{running}`
- `DELETE /api/runtime/instances/{id}`（需先停止）
- `GET /api/runtime/instances/{id}/logs?limit=`
- `POST /api/runtime/tick` — 立即评估全部运行中实例（先保护性离场，再评估信号）

## 组合
- `GET /api/portfolio/overview?account_id=&record=`
- `GET /api/portfolio/history?account_id=&limit=`
- `GET /api/portfolio/analytics?account_id=&limit=`

## 风控
- `GET /api/risk/limits`
- `GET /api/risk/state?account_id=`
- `POST /api/risk/observe?account_id=&equity=`
- `POST /api/risk/reset?account_id=`

## 实盘通道
- `GET /api/brokers/status`
- `GET /api/brokers/binance/verify`

## 数据
- `GET /api/data/coverage`
- `GET /api/data/quality?symbol=&timeframe=`
- `GET /api/data/export?symbol=&timeframe=&limit=` → `text/csv`
- `POST /api/data/import`（multipart：`symbol`, `timeframe`, `replace`, `file`）
- `DELETE /api/data/series?symbol=&timeframe=` — 删除某标的某周期的全部 K 线，返回实际条数

## A股
- `GET /api/cn/rules` — 手数、板块涨跌停、费率、T+1、时段
- `GET /api/cn/session` — 当前是否开市、下一开盘时间
- `GET /api/cn/calendar?days=` — 交易日历，含 `coverage`（该年节假日表是否已加载）
- `GET /api/cn/symbols` — A 股标的含现价/涨跌停
- `POST /api/cn/symbols/{code}/suspend?suspended=` — 模拟停牌
- `POST /api/cn/settle`、`POST /api/trading/settle` — 释放 T+1 冻结

## A股行情数据
- `GET /api/cn/data/source` — 数据源健康：模式（synthetic / on-demand / realtime-poll）、轮询间隔、延迟
- `GET /api/cn/data/quotes` — 全部标的的实时报价，含 `age_ms` 与 `fresh`
- `POST /api/cn/data/poll` — 立即拉取一次报价，不等待下一个轮询周期
- `POST /api/cn/data/sync?codes=` — 同步历史（会替换该标的原有 K 线）
- `GET /api/cn/data/staleness` — 每个标的日线的滞后期

## 加密行情数据
- `GET /api/market/data/source` — 数据源健康（synthetic / realtime-poll）
- `GET /api/market/data/quotes` — 全部标的的实时报价，含 `age_ms` 与 `fresh`
- `POST /api/market/data/poll` — 立即拉取一次
- `POST /api/market/data/sync` — 强制重导 Binance 历史（替换原有序列）

## 实盘通道
- `GET /api/brokers/status` — 各通道（binance / ths）就绪状态
- `GET /api/brokers/ths/health` — 同花顺网关健康与余额
- `GET /api/brokers/ths/positions` — 网关侧持仓
- `GET /api/brokers/ths/reconcile?account_id=` — 本地与网关持仓对账

> 实盘下单：`POST /api/trading/orders` 且必须携带 `X-Live-Confirm: true`。

## AI 助手
- `GET /api/assistant/status`
- `GET /api/assistant/tools`
- `POST /api/assistant/chat` — `{message, account_id?}`
- `POST /api/assistant/tools/{name}` — `{tool, arguments}`

可用工具：`list_accounts`、`account_overview`、`risk_overview`、`list_positions`、
`recent_orders`、`market_snapshot`、`list_strategies`、`strategy_signals`、
`run_backtest`、`list_runtime_instances`。全部只读。

## 告警
- `GET /api/alerts?account_id=&unacknowledged_only=&level=&category=&limit=` — 告警列表
- `GET /api/alerts/categories?account_id=` — 各类型的数量（含已确认）
- `POST /api/alerts/prune?keep_days=&account_id=` — 删除超期的已确认告警
  （未确认的永不按时间删除）
- `GET /api/alerts/summary?account_id=` — 未确认数量（按级别）
- `POST /api/alerts/{id}/ack`、`POST /api/alerts/ack-all?account_id=` — 确认

熔断触发、保护性离场（含失败）、自动下单被拒与网关故障会自动产生告警；
同一条未确认告警在 5 分钟内不会重复。已确认告警默认保留 30 天
（`LQ_ALERT_RETENTION_DAYS`），由调度器每小时修剪。
