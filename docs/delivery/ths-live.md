# 同花顺接入与实盘操作

## 先说清楚边界

本平台**不驱动同花顺桌面客户端**，也不做窗口句柄注入、模拟键鼠或内存读取。
那类做法违反同花顺用户协议，稳定性与合规性都无法保证。

平台接入的是**已授权的下单网关**：一个提供 HTTP JSON 接口的本地或券商侧服务，
由你在券商开通程序化交易权限后部署或获取。平台只负责：

1. 按 A 股规则校验委托（手数、涨跌停、T+1、时段）；
2. 把委托提交给网关；
3. 轮询网关状态，把成交回写到本地持仓与账本；
4. 与网关对账，暴露差异。

## 网关接口契约

平台按以下契约调用网关（`Authorization: Bearer <token>`）：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/health` | `{"ok": true}` |
| GET | `/api/account` | `{"cash": 100000, "positions":[{"symbol","quantity","available_quantity","avg_price"}]}` |
| POST | `/api/orders` | 请求 `{symbol, side, order_type, quantity, limit_price?, client_order_id?}`，返回 `{order_id, status}` |
| GET | `/api/orders/{id}` | `{order_id, status, filled_quantity, avg_price, reject_reason?}` |
| DELETE | `/api/orders/{id}` | 撤单 |
| GET | `/api/orders?status=open` | 未完成委托 |
| GET | `/api/trades?since=<ms>` | 增量成交回报 |

`status` 取值：`pending / accepted / partial / filled / cancelled / rejected`，
平台映射为自己的 `new / open / partially_filled / filled / cancelled / rejected`。

如果你的网关字段不同，改 `backend/app/modules/brokers/ths.py` 里的
`ThsHttpGateway` 即可，其余代码不用动。

## 配置

```env
# 开启实盘总开关
LQ_LIVE_TRADING_ENABLED=true

# 指向已授权的同花顺下单网关
LQ_THS_GATEWAY_URL=http://127.0.0.1:9001
LQ_THS_GATEWAY_TOKEN=<网关访问令牌>

# 成交回报轮询间隔（秒）
LQ_LIVE_RECONCILE_SECONDS=3

# A股费率（按你的券商实际费率改）
LQ_CN_COMMISSION_BPS=2.5
LQ_CN_MIN_COMMISSION=5
LQ_CN_STAMP_DUTY_BPS=5
LQ_CN_TRANSFER_FEE_BPS=0.1
```

启用演练网关（不需要真实券商，用于验证整条链路）：

```env
LQ_LIVE_TRADING_ENABLED=true
LQ_THS_MOCK_ENABLED=true
```

## 操作流程

### 1. 确认通道就绪

```bash
curl http://127.0.0.1:8000/api/brokers/status | jq
curl http://127.0.0.1:8000/api/brokers/ths/health | jq
```

`status.channels.ths.ready` 为 `true` 才能下单。

### 2. 创建实盘账户

```bash
curl -X POST http://127.0.0.1:8000/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"name":"A股实盘","kind":"live","market":"cn_stock","initial_balance":1}'
```

初始资金只是本地记账起点，实际余额以网关为准。

### 3. 下单（必须带确认头）

```bash
curl -X POST http://127.0.0.1:8000/api/trading/orders \
  -H "Content-Type: application/json" \
  -H "X-Live-Confirm: true" \
  -d '{"account_id":"<id>","symbol":"600519.SH","side":"buy","order_type":"limit","quantity":100,"limit_price":1680.00}'
```

返回中 `broker_order_id` 是网关单号，`status` 通常为 `open`（网关已受理）。

### 4. 成交回报

后台 `LiveReconciler` 每 3 秒轮询未完成委托：

- 只把**增量成交**写入本地（幂等键：`order_id + 累计成交量`）；
- 按真实价格与 A 股费率计算费用；
- 买入后 `available_quantity` 保持 0（T+1），次日开盘结算解锁；
- 拒单写入 `reject_reason`。

前端「交易 → A股」页面可看到委托状态与网关单号。

### 5. 对账

```bash
curl "http://127.0.0.1:8000/api/brokers/ths/reconcile?account_id=<id>" | jq
```

逐标的比较本地持仓与网关持仓，`all_match` 为 `false` 时需要人工核对。
建议每日收盘后对一次。

## 三重安全条件

实盘下单必须同时满足：

1. `LQ_LIVE_TRADING_ENABLED=true`（服务端总开关）；
2. 网关已配置且 `ready`；
3. 请求头 `X-Live-Confirm: true`（防误触）。

任一不满足即 403/409，且原因写在错误消息里。

## 常见问题

**Q: 下单返回 409「A股非交易时段」**
实盘默认强制真实交易时段。若在非交易时段调试，仅在演练环境设
`LQ_CN_LIVE_SESSION_GATE=false`。

**Q: 委托一直停在 open**
网关未回报成交。检查 `GET /api/orders/{id}` 是否返回 `filled`；
若网关侧已成交而本地未更新，看后端日志中 `live reconcile` 相关报错。

**Q: 本地持仓与券商不一致**
先拉对账接口定位差异标的；不要手工改数据库，用撤单/补单让状态回归，
差异原因通常是手工在券商客户端下过单。

**Q: 我的券商没有程序化接口**
那只能使用模拟账户。平台不会通过桌面自动化绕过这一限制。
