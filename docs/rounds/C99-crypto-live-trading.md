# 迭代记录 C99：加密实盘交易链路（原先只有一个市场能真正成交）

## 缺陷

平台宣称支持两个市场的实盘交易，但只有 A 股的链路是完整的。加密实盘存在
三个断点，任何一个都足以让"实盘"变成"只提交不成交"。

### 一、加密订单永远没人对账

`LiveReconciler` 只接同花顺网关：

```python
reconcile_engine = LiveReconciler(db, settings, gateway=cn_gateway)
```

它的 `start()` 在网关为 `None` 时直接返回，`tick()` 也只按
`live:` 前缀筛选订单。而 Binance 对限价单的回应几乎总是 `NEW`——订单
挂在交易所，成交要等后续查询。

实测（限价买 1 BTC @ 95，交易所随后成交）：

```
提交后:            status=open  broker_order_id=B1  reserved cash=95.0
连续 reconcile:    {'polled': 0, 'filled': 0, 'rejected': 0}   ← 一次都没查
最终:              status=open  filled=0.0
```

后果是链式的：

| 环节 | 状态 |
|---|---|
| 本地订单 | 永远 `open` |
| 预留现金 | 永远冻结（`reserved_cash` 按未完成买单计算） |
| 持仓 | 永不出现 |
| 可用资金 | 单调减少，直到手动撤单 |

用户看到的是"下单成功"，实际仓位在交易所，本地账本是空的。

### 二、`order_status` 缺 symbol，Binance 无法查询

Binance 的 `/api/v3/order` 强制要求 `symbol` 参数。原端口契约是

```python
def order_status(self, broker_order_id: str) -> BrokerOrderResult: ...
```

加密适配器根本没有实现这个方法，就算接上了也查不到——本地订单行里有
symbol，但契约不允许把它传下去。

### 三、数量没对齐交易所步长

每个 Binance 交易对有 `LOT_SIZE.stepSize`。不在步长整数倍上的数量会被
直接拒绝（错误码 `-1111`）。而 `_place_live_order` 把调用方给的数量原样
写入本地订单行，于是：

- 交易所拒单，本地记录了一个交易所从未接受的数量；
- 自动运行的仓位计算（`max_affordable_quantity`）用的是自建的 `lot_size`，
  与交易所真实的 stepSize 未必一致；
- 用户看到 `open` 的订单，实际上从未存在。

## 修复

### 对账器支持双通道

`LiveReconciler` 现在同时持有 A 股网关和 Binance 适配器，按**账户市场**
分派（不能按 source 判断——两个通道都写 `live:` 前缀）：

```python
def _channel_for(self, session, order):
    market = AccountService(session).require(order.account_id).market
    if market == "cn_stock":
        return "ths", self.gateway
    return "binance", self.broker
```

`start()` 的条件从"网关存在"改为"任一通道存在"：

```python
if self.gateway is None and self.broker is None:
    return
```

记账逻辑本来就是市场无关的，现在按市场选择费率：A 股走 `CnFeeSchedule`，
加密走 `fee_bps`。

### 端口契约补上 symbol

```python
def order_status(self, broker_order_id: str, *, symbol: str = "") -> BrokerOrderResult: ...
```

Binance 实现里 symbol 缺失时抛 `ConflictError` 而不是猜——静默去掉参数
只会换来一次必然失败的请求。A 股网关忽略这个参数（按 order_id 查询）。

### 数量在写入本地订单前对齐

```python
normalise = getattr(gateway, "normalize_quantity", None)
if normalise is not None:
    quantity = round_qty(normalise(symbol, quantity))
```

`BinanceBroker.normalize_quantity` 拉一次 `exchangeInfo` 并缓存 stepSize，
按**向下取整**对齐。取整方向不能反——向上会让订单金额超过预算，而向下
最多少买一点。汇率元数据拉取失败时退回原值，由交易所最终裁决，
不让一次元数据超时阻断实盘下单。

下单报文里数量也按步长自身的小数位渲染（`stepSize=0.00001` 渲染 5 位，
不是固定 8 位），避免 `0.12345000` 这类被某些实现当作精度超限的写法。

### 顺带修的三处

1. **每次请求新建 `httpx.Client`**：对账器每 3 秒轮询一次，每次都是一次
   TLS 握手。改为进程内单例，`close()` 在引擎停止时调用（`stop()` 里）。
2. **对账失败的告警**：原来只有 `logger.warning`。交易所断线时所有实盘订单
   停在 `open`、现金持续冻结，而页面上什么都不显示。现在连续 3 次失败后
   发一条 `critical`，之后每 20 次提醒一次。
3. **`unlock_t1` 会解锁加密持仓**：它无条件把所有
   `quantity > available_quantity` 的持仓对齐，而加密市场根本没有 T+1。
   改为只处理 A 股账户——越界解锁等于用账本 bug 掩盖另一个账本 bug。

## 实盘可用性的最后一环：本地资金与交易所余额对齐

本地账本按建账户时的 `initial_balance` 记账，而真实可交易资金在交易所。
文档示例里 A 股实盘用 `initial_balance=1`（"初始资金只是本地记账起点"），
这个约定在 A 股下成立——因为 A 股实盘的下单校验以网关为准；但在加密侧，
`_place_live_order` 之前的资金校验读的是本地账本：

```
下单被拒: insufficient cash: need 444.77, available 1.0
```

即便对账器已经能推进订单，也无法下一笔单。

新增 `AccountService.align_to_broker_cash()` 与
`POST /brokers/binance/align-cash`：

```
GET/POST ?account_id=..&confirm=false   → 预览：local / broker / difference
POST     ?account_id=..&confirm=true    → 应用
```

两点设计：

- **默认只预览**。对齐余额是一次入金/出金事件，绝不能因为打开了某个页面
  就悄悄发生。
- **写成账本条目**（`deposit` / `withdraw`，附原因），不是直接改字段。
  差异必须以"资金变动"的形式留在流水里，否则对账历史会被抹平。

## 验证

### 后端新增 14 项

`tests/unit/test_crypto_live.py`（9 项）：

| 用例 | 断言 |
|---|---|
| 挂着 → 成交 | 从 `open` 到 `filled`，持仓 1.0、可卖 1.0、预留现金归零 |
| 重复轮询幂等 | 第二遍 `filled=0`，持仓不变 |
| symbol 传下去 | 对账器调用 `order_status` 时带上 `BTCUSDT` |
| 部分成交只记增量 | 0.5 → 2.0 分两次入账，状态 `partially_filled` → `filled` |
| 拒单终态 | `rejected` + `reject_reason` |
| 连续失败一条告警 | 5 次 tick，`gateway_failure` 告警 1~2 条，标题含"成交回报中断" |
| 数量向下取整 | `0.123456789 → 0.12345`，报文渲染 5 位 |
| 缺 symbol 直接报错 | 抛 `AppError`，不发请求 |
| 客户端复用 | 三次调用同一个 `httpx.Client`，`close()` 后置空 |

`tests/integration/test_crypto_live_api.py`（5 项）：走完整 HTTP 链路

| 用例 | 断言 |
|---|---|
| 通道状态 | 两个市场分别报告 `ready`，且有 `reconciler_running` |
| 对齐预览不改账 | `applied=false`，现金仍是 1.0 |
| 对齐后才能下单 | 先 409 `insufficient cash`，对齐后 201，报文数量 `0.25` |
| 全链路成交 | `open` → `partially_filled` → `filled`，2 笔 Fill，无预留现金 |
| 未对齐账户对账 | `cash_match=false`，明确给出两边数值 |

### 还原验证

| 还原的改动 | 结果 |
|---|---|
| 对账器去掉加密分支 | 1 failed |
| 不传 symbol | 1 failed |
| 去掉数量对齐 | 1 failed |

### 前端

`RiskPanel` 里的"实盘通道"卡片读的是旧的 `BrokerStatus`
（`broker` / `ready` / `credentials_configured`），而后端 `/brokers/status`
返回的是 `channels` 结构——**这张卡片一直显示"未就绪"**，即使通道正常。
同时删掉了与 `BrokerStatus` 重复的 `BrokerChannels` 类型（同一个端点两套结构）。

新卡片按市场列出两个通道，并把"成交回报轮询"单独显示：密钥配好了不等于
成交会回写，这是本轮修的核心问题，必须能一眼看到。

新增 `RiskPanel.test.tsx`（3 项）：通道列出、轮询运行中、轮询停止时的提示。

### 回归

- 后端 **566 passed**，改动文件 ruff 干净
- 前端 **69 passed**，`tsc` 零错误，生产构建通过

## 加密实盘开通清单

```env
LQ_LIVE_TRADING_ENABLED=true
LQ_LIVE_AUTO_TRADING_ENABLED=true        # 仅当允许策略自动下真实单
LQ_BINANCE_API_KEY=...
LQ_BINANCE_API_SECRET=...
LQ_CRYPTO_DATA_SOURCE=binance            # 用真实行情驱动信号
LQ_LIVE_RECONCILE_SECONDS=3
```

1. `GET /api/brokers/status`：`channels.binance.ready` 为 true 且
   `reconciler_running` 为 true。
2. `GET /api/brokers/binance/verify`：确认 `can_trade`。
3. `POST /api/brokers/binance/align-cash?account_id=..&confirm=true`：
   本地账本对齐交易所余额。
4. 手动单需 `X-Live-Confirm: true`；自动运行需上述第四个开关。
5. 定期 `GET /api/brokers/binance/reconcile` 与
   `GET /api/brokers/ths/reconcile` 对账。

## 已知限制

1. **只支持现货**。期货/杠杆账户的 `balances` 语义不同，需要单独的适配器；
   当前实现按现货 REST v3 设计。
2. **成交价取自交易所累计均价**。分批成交时每笔增量成交的单价都用累计均价
   近似，多笔价差较大时单笔成本会有偏差（费用也按该均价计算）。
   要求逐笔精确需改用 `/api/v3/myTrades`，本轮未做。
3. **不自动同步交易所侧的手工操作**。用户在币安 App 里手动买卖，本地账本
   不知道；`/binance/reconcile` 能发现差异，但不会自动调整。这是刻意的——
   自动"修复"会把用户的手工操作抹成平台操作。
4. **`align-cash` 只对齐计价币现金，不对齐币币持仓**。持仓重新对齐需要
   同时处理成本基准，属于独立的一次设计。
5. **对账告警只有恢复不清除**：通道恢复后不会自动确认那条
   `gateway_failure`，需要人工点确认。中断期间"哪些订单可能漏了成交"
   需要人工判断，自动清除会掩盖这一点。
