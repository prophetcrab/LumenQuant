# 迭代记录 C100：实盘账本的七个静默错误（成交了，但账本不知道）

## 排查方式

C99 让加密实盘能跑通"下单 → 成交 → 入账"之后，我请了一个独立审查专门找
**"券商做了、本地记错或没记"** 的情形。这类缺陷的共同点是：不报错、不违反
任何断言、页面显示正常，但账本与真实账户永久偏离。

找到 7 个，全部可复现。

## 缺陷一（P0）：交易所已成交，本地订单行被整条回滚

`_place_live_order` 的顺序是：调用网关 → 建订单行 → 记账 → 提交。

但记账会抛异常：

```python
# _book_fill 买入分支
if total_cost > available + 1e-9:
    raise ConflictError("insufficient cash at execution time")
```

而风控校验用的是**市价**，交易所实际成交价可能更高（滑点）。于是：

```
风控通过（按市价算成本 100）
→ 交易所成交（实际 100.7）
→ _book_fill 判定资金不足，抛异常
→ get_session 回滚
→ 订单行连同 broker_order_id 一起消失
```

交易所那边钱花了、币到手了，本地连这笔单的存在都没有记录，对账器再也
不会查它。用户看到 409「执行时资金不足」，以为没下成。

### 修复

网关确认接单后**先提交订单行**，再记账；记账失败只降级为"待对账"：

```python
self.session.add(order)
self.session.flush()
record_audit(...)
self.session.commit()          # ← 先落盘，broker_order_id 不可丢

try:
    self._book_fill(...)
    self.session.commit()
except Exception as exc:
    self.session.rollback()
    order = self.session.get(Order, order.id)
    order.status = "open"
    order.reject_reason = f"本地记账失败，等待对账回写：{exc}"
    self.session.commit()
    self.alerts.raise_alert(category="exit_failed", level="critical", ...)
```

对账器按券商回报回写，不依赖刚刚失败的那个本地资金校验——这条路能把账
补回来。

## 缺陷二（P0）：`exchangeInfo` 的 symbol 参数发进了请求体

非签名请求的构造是：

```python
url = f"{self.base_url}{path}"
body = params or None
response = client.request(method, url, headers=headers, data=body)
```

httpx 把 GET 的 `data` 放进**请求体**。实测：

```
URL : https://api.binance.test/api/v3/exchangeInfo     ← 没有 ?symbol=
BODY: b'symbol=BTCUSDT'
```

交易所忽略 GET body，于是返回**全部交易对**；`_symbol_rules` 取
`data["symbols"][:1]`，大概率不是被查询的标的。后果：

- 用错交易对的 `stepSize` → 数量不是真实步长的倍数 → 交易所 `-1111` 拒单
  （C99 想修的问题原样存在）；
- 若首条步长更粗，会把数量砍掉上百倍（BTCUSDT 被按 0.001 而不是 0.00001）；
- 首次查询还下载整份 exchangeInfo（MB 级）。

### 修复

参数改走 query string，并且**按 symbol 精确匹配**而不是信任返回顺序：

```python
response = self._client().request(method, url, headers=headers, params=params or None)
...
entries = [item for item in data.get("symbols", [])
           if str(item.get("symbol", "")).upper() == key]
```

## 缺陷三：`avg_price <= 0` 时订单被终结，已成交部分永久丢失

```python
price = result.avg_price or 0.0
if price <= 0:
    return False               # 没记账
...
if result.status == "cancelled":
    order.status = "cancelled"  # 但状态照样终结
```

券商对撤单常只回累计数量、不回价格。于是那笔真实成交既没入账，订单也离开
了轮询集合（`OPEN_STATUSES`），再也不会被查到——而预冻结的资金随状态终结
被释放，本地现金比实际多出这一笔。

同理 `status=filled` 但 `executedQty=0` 会被直接置为 `filled`，零成交却
"已完成"。

### 修复

有未入账 delta 但没有价格时，**保持订单 open 并记录原因**，等下一次轮询：

```python
if price <= 0:
    order.reject_reason = f"券商回报成交量 {cumulative:g} 但未提供成交价，等待再次轮询"
    return False
```

并且 tick 在写终态前检查是否还有未入账成交量：

```python
if result.filled_quantity > order.filled_quantity + 1e-9:
    continue          # 还有没记的成交，不许终结
```

另外 `status=filled` 但累计成交量不足委托量时也只记原因不终结——否则预留
释放、尾量蒸发。

## 缺陷四：对账路径漏了 A 股 T+1 的资金冻结

`_book_fill`（即时回报）在 A 股卖出后写 `CashHold`，`_apply`（对账回写）
只调 `post_cash`，没有 hold。

而真实同花顺网关对市价单几乎总是回 `accepted`，所以**所有实盘 A 股卖出都
走 `_apply`**。结果是卖出所得当天就能用于买入，`settle_t1` /
`release_held_cash` 永远看不到这笔钱。

### 修复

`_apply` 的卖出分支补上与 `_book_fill` 一致的 `CashHold`。

## 缺陷五：部分成交用累计均价记增量，系统性错价

`BrokerOrderResult.avg_price` 是**累计**均价，而 `_apply` 把
`delta × avg_price` 当作这一笔的金额：

```
第一次: 0.5 成交, 报价 50   → 均价 100 → 记 0.5 @ 100
第二次: 1.0 成交, 报价 110  → 均价 110 → 记 0.5 @ 110
本地总支出 105，实际 110
```

差的 5 永久留在账本里，持仓成本、Fill 价格、手续费基数全部偏低。

### 修复

`BrokerOrderResult` 增加 `quote_quantity`（累计成交额），对账器做差分得到
真实边际价：

```
incremental = quote_quantity - booked_quantity × booked_avg
marginal    = incremental / (cumulative - booked)
```

同时把订单的 `avg_fill_price` 保持为**累计均价**——下一次差分正是拿它做基数，
写入边际价会让下一轮算出错误的增量。

## 缺陷六：通道消失后订单被静默跳过

```python
if channel_client is None:
    continue
```

运营者在重启时把 `LQ_THS_GATEWAY_URL` / `LQ_THS_MOCK_ENABLED` 关掉，或某个
部署只配了一个市场：属于该通道的在途单每 tick 被跳过，不计入 `polled`、
不告警，`reserved_cash` 永久占用。

### 修复

连续 3 次遇到无通道的订单后发一条 critical 告警，明确说明"成交不会再回写、
预留资金不会释放"。孤儿计数有独立的 key，不会被常规通道计数重置掉。

## 缺陷七：`/binance/reconcile` 把现金当资产，`all_match` 永假

```python
local_assets.setdefault(account.base_currency.upper(), 0.0)
```

把 `USDT` 以"本地 0"塞进资产集合，而交易所侧是全部现金余额。必然出现
`USDT: local 0 / exchange 100000` → `match=False` → `all_match` 永假。
资金完全对齐的正常账户也会一直报不一致。

### 修复

计价币从资产比较中剔除，只走下面的现金行：

```python
if currency:
    local_assets.pop(currency, None)
    remote_assets = {k: v for k, v in remote_assets.items() if k != currency}
```

顺带把 `broker.balances()` 的重复调用合并成一次。

## 另外三处

**`align-cash` 会把现金清零。** `balances()` 只返回非零资产，
`balances.get(currency, 0.0)` 无法区分"没有该资产"和"余额为 0"。用户把 USDT
全换成 BTC 后点确认 → 本地现金被写成 0，而平台并没有那笔 BTC 持仓。
改为：交易所未报告该计价币时**拒绝执行**。

**`align-cash` 缺确认头。** `/trading/orders` 要求 `X-Live-Confirm: true`
（自定义头会触发 CORS 预检），而这个接口只要 query 参数。默认
`auth_enabled=false` 时它是一个浏览器可直接提交的简单表单请求，一次就能改写
资金基准。已要求同一个头，并校验账户绑定的券商是 binance。

**对账器被绑在 `runtime_enabled` 上。** 它原本在 `runtime_enabled` 的
`if` 块里创建。纯手动实盘模式（`LQ_RUNTIME_ENABLED=false`，正是
`live_auto_trading_enabled` 想支持的场景）或没有行情引擎的部署下根本不建
对账器——实盘订单永不推进、资金永久冻结。对账是资金安全职责，不该依赖是否
跑策略，已移到独立条件。

**顺带**：`build_ths_gateway` 在 `main.py` 里没传 `price_feed`，导致 mock
网关的市价单永远停在 `accepted`——演练路径本身就是"僵尸单"的稳定复现。
已补上（用 `live_candle` 的收盘价）。THS 客户端也改成连接复用，与 Binance
对称。

## 验证

### 新增 12 项

`tests/unit/test_live_order_integrity.py`（8 项）：

| 用例 | 断言 |
|---|---|
| GET 参数在 query | URL 含 `symbol=BTCUSDT`，body 为空 |
| 按 symbol 匹配过滤器 | 服务端返回全量时仍取到 BTCUSDT 的步长 |
| 无 order_id 拒绝 | 订单记 `rejected`，不产生持仓 |
| 本地撮合不碰实盘单 | 丢掉 broker_id 的实盘行仍不被本地成交 |
| 无价成交不终结 | 保持 `open` + 原因；补价后正常入账并终结 |
| 部分成交按边际价 | 现金减少 110（不是 105），Fill 价格 100 / 120 |
| A 股卖出写 CashHold | 对账路径产生 11000 的冻结 |
| 孤儿订单告警一次 | 4 次 tick → 1 条"通道已不可用" |

`tests/integration/test_reconciler_wiring.py`（4 项）：

| 用例 | 断言 |
|---|---|
| runtime 关闭仍对账 | `reconcile_engine` 存在，`runtime_engine` 为 None |
| 实盘关闭不建对账器 | `reconcile_engine` 为 None |
| 状态暴露轮询 | lifespan 内 `reconciler_running=true` |
| mock 网关有报价源 | `_price_feed` 能给出正价格 |

### 还原验证（9 项全部生效）

| 还原的改动 | 测试 |
|---|---|
| query 改回 body | 1 failed |
| 按 symbol 匹配改回取首条 | 1 failed |
| 接受空 order_id | 1 failed |
| 本地撮合不检查 source | 1 failed |
| 去掉"还有未入账则不终结" | 1 failed |
| 用累计均价记增量 | 1 failed |
| 去掉 A 股 CashHold | 1 failed |
| 对账器重新绑回 runtime | 1 failed |
| mock 网关不传 price_feed | 1 failed |

### 回归

- 后端 **575 passed**，改动文件 ruff 干净
- 前端 69 passed，`tsc` 干净，构建通过

## 共同的形状

C95–C98 修的是"失败路径做错事"和"空转"。这一轮是第三种：

| 缺陷 | 券商侧 | 本地侧 | 谁能发现 |
|---|---|---|---|
| 订单行回滚 | 已成交 | 无记录 | 只有对账（如果还对得上） |
| 参数进 body | 拒单 | 记了个假数量 | 只有"下单总是失败" |
| 无价成交 | 已成交 | 未入账 | 不会 |
| 缺 CashHold | 已交收 | 当天可再买 | 不会，直到次日对账 |
| 累计均价 | 收 110 | 记 105 | 不会，差额永久留存 |
| 通道消失 | 订单还挂着 | 静默跳过 | 不会 |
| 现金当资产 | 正常 | 永报不一致 | 会，但会当噪音忽略 |

共同点是**"本地账本与真实账户偏离，且没有任何断言失败"**。这一轮新加的
`_increment_price` / `CashHold` / 未入账检查，都是把"券商视角的事实"作为
唯一真值来源，而不是用本地推断补全。

## 已知限制（更新）

1. **A 股最低佣金按每次增量收取**：同一笔分 3 次回报会收 3 次 5 元最低佣金。
   需要按订单累计名义额计算累计费用再取增量，本轮未做。
2. **没有订单超时**：券商回 `accepted` 后永不回报时，订单和预留资金会一直
   挂着。健康告警只覆盖轮询异常，不覆盖"查询成功但永远 open"。
3. **`/trading/match`、`/trading/settle`、`DELETE /trading/orders/{id}` 无
   额外确认**，与 `/trading/orders` 的保护层级不一致。
4. 其余同 C99：仅现货、成交价用累计口径（现已按增量差分，但券商不给
   `quote_quantity` 时仍退回累计均价）、不自动同步交易所手工操作。

## 缺陷九（补记）：撤单竞态——撤单不会撤销已经发生的成交

`cancel_order` 的顺序是：撤销网关委托 → 本地置 `cancelled` → 提交。

券商可以在这两次轮询之间（最长一个对账间隔，默认 3 秒）成交一部分。撤单成功
只意味着"剩余部分不会再成交"，不意味着"什么都没成交过"。而本地直接置
终态，订单离开轮询集合，那部分成交永远不会被补记：

```
限价买 1 BTC @ 95
→ 券商成交 0.5（PARTIALLY_FILLED），对账器还没来得及轮询
→ 用户撤单，券商撤销剩余 0.5
→ 本地: status=cancelled, filled_quantity=0, 无持仓
→ 实际: 账户里有 0.5 BTC，现金已经花掉
```

这是默认配置下的常见路径，不是边缘情况。

### 修复

撤单成功后**强制读取一次券商终态**，用与对账器完全相同的记账逻辑补记
（新增 `brokers/booking.py`，两条路径共用，避免"哪条先跑导致数字不同"）：

```python
if not self._settle_before_closing(order, gateway):
    self.session.commit()
    return self._order_view(order)   # 保持 open，交给对账器
order.status = "cancelled"
```

`_settle_before_closing` 返回 False 的两种情况都必须让订单**保持挂起**：

- **读不到终态**（网关超时）：`reject_reason` 写明"撤单已提交，但无法读取
  券商终态"，并发 warning 告警。订单仍在 `OPEN_STATUSES`，对账器继续跟踪，
  迟到的成交仍然会被补记。呈现为"已撤销"是错的——用户会以为没成交过。
- **券商报了成交量但没有价格**：保持挂起并发 critical。这与对账路径的第 3 条
  是同一个判断。

补记成功时写一条 `order.filled_during_cancel` 审计，说明这笔成交是在撤单时
才发现的，而不是下单时。

## 缺陷十（补记）：A 股 T+1 解锁只在策略运行时存在时才发生

T+1 解锁原本写在 `TradingEngine._session_duties` 里，而 `TradingEngine` 只在
`LQ_RUNTIME_ENABLED=true` 时构造。受支持的"仅手动实盘"模式
（`LQ_RUNTIME_ENABLED=false` + `LQ_LIVE_TRADING_ENABLED=true`）下：

- 对账器已经在跑（C100 已修），成交能回写；
- 但**没有任何东西会把 `available_quantity` 从 0 提到 `quantity`**；
- 于是今天买的 A 股永远不可卖 —— `close_position` 和卖单都会被 T+1 规则
  永久拒绝。

`LiveReconciler.unlock_t1` 当时除测试外没有任何调用点。

### 修复

把解锁逻辑抽到 `brokers/settlement.py`，两个后台循环共用：

```python
def release_t1(session, *, now_ms=None) -> tuple[int, int]:
    ...   # 只处理 cn_stock 账户；返回 (解锁持仓数, 释放冻结数)
```

对账器在每次 tick 开头执行一次，按交易日去重：

```python
key = settlement.session_key(now)
if self._settled_session == key:
    return
positions, holds = settlement.release_t1(session, now_ms=now)
settlement.refresh_prev_close(session, cn_engine)
self._settled_session = key
```

`TradingEngine._session_duties` 改为调用同一函数，两处不再有分叉的实现。

几个刻意的选择：

- **只处理 A 股账户**。加密即时交收，无条件解锁会把"账本 bug"伪装成
  "T+1 冻结"，让一个真实的 `quantity/available` 不一致永远看不见。
- **冻结资金按 `release_day <= today` 释放**，而不是全清。盘中调用不会提前
  放行券商还没交收的款项。
- **没有模拟引擎时回落到墙钟**。真实数据部署没有 `CnMarketEngine`，早返回
  会让 T+1 永不发生（这是 C78 修过的一个缺陷，不能在这条新路径上重新引入）。

### 验证

`tests/unit/test_cancel_and_settlement.py`（9 项）：

| 用例 | 断言 |
|---|---|
| 撤单补记已成交 | 现金 -38、持仓 0.4、1 笔 Fill、状态 `cancelled` |
| 无成交撤单 | 资金与持仓不变 |
| 不重复记账 | 对账器已记过后撤单，现金与 Fill 数量不变 |
| 终态读不到 | 订单保持 `open` + 原因；恢复后对账器能补记 |
| 有量无价 | 订单保持 `open`，未入账，原因含"未提供成交价" |
| runtime 关闭仍解锁 T+1 | 对账器 tick 后 `available = quantity` |
| 按 release_day 释放 | 到期冻结清除，未来冻结保留 |
| 不碰加密持仓 | crypto 的 `available` 保持 0 |
| 无引擎用墙钟 | 断言读到的 now 接近真实时间；休市不解锁 |

还原验证（5 项全部生效）：跳过终态查询、终态读不到也终结、对账器不做 T+1、
忽略 `release_day`、无差别解锁所有账户——各自对应测试变红。

### 回归

- 后端 **575 passed**（新增 9 项）
