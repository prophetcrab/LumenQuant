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

## 自动运行
- `GET /api/runtime/instances?account_id=`
- `POST /api/runtime/instances` — `{account_id, strategy_key, symbol, timeframe, params, position_pct, max_order_notional, start}`
- `PATCH /api/runtime/instances/{id}` — `{params?, position_pct?, max_order_notional?}`
- `POST /api/runtime/instances/{id}/toggle` — `{running}`
- `DELETE /api/runtime/instances/{id}`（需先停止）
- `GET /api/runtime/instances/{id}/logs?limit=`
- `POST /api/runtime/tick` — 立即评估全部运行中实例

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

## A股
- `GET /api/cn/rules` — 手数、板块涨跌停、费率、T+1、时段
- `GET /api/cn/session` — 当前是否开市、下一开盘时间
- `GET /api/cn/calendar?days=` — 交易日历
- `GET /api/cn/symbols` — A 股标的含现价/涨跌停
- `POST /api/cn/symbols/{code}/suspend?suspended=` — 模拟停牌
- `POST /api/cn/settle`、`POST /api/trading/settle` — 释放 T+1 冻结

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
