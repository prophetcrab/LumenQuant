# 迭代记录 C55：实盘网关故障时的行为

## 动机

实盘网关是系统里唯一的外部依赖。前面几十轮都假设它可用——
`MockThsGateway` 永远不会失败。真实网关会超时、会拒绝、会返回无法解析的内容。

这一轮把网关打断，看系统怎么表现。

## 缺陷一：下单时网关抛异常 → 500，且订单不留痕

把 `gateway.place_order` 换成直接抛异常：

```
POST /api/trading/orders -> 未处理异常 -> HTTP 500
orders recorded: 0
```

两个问题：

1. **返回 500**：用户看到的是"服务器错误"，而不是"网关不可用"
2. **没有任何记录**：整个下单尝试在系统里不存在。用户无法知道
   "我到底有没有发出这笔单"，事后也无法审计

第二点更严重。实盘的语义下，"可能已发出"和"确定没发出"是完全不同的状态，
必须留痕。

### 修法

捕获异常，先落一条 `rejected` 订单再抛业务错误：

```python
except Exception as exc:
    self._record_gateway_failure(...)   # 落库 + 审计，状态 rejected
    raise ConflictError(f"下单网关调用失败，订单未确认提交：{exc}") from exc
```

`reject_reason` 写的是 `gateway error before ack: ...`——明确表达
"ack 之前就失败了"，不假装知道券商侧发生了什么。

修后：

```
order: 409 下单网关调用失败，订单未确认提交：gateway unreachable
orders recorded: [('rejected', 'live:manual', 'gateway error before ack: gateway unreachable')]
```

## 缺陷二：撤单失败被当成成功（潜在）

原实现：

```python
gateway.cancel_order(order.symbol, order.broker_order_id)   # 可能抛异常
order.status = "cancelled"
```

异常会冒泡，所以状态不会被改成 cancelled——**这一点原本是对的**。
但它同样会返回 500，把"券商可能还持有这张单"这个关键信息埋在一个服务器错误里。

修法：包装成 409 并说明风险：

> 撤单请求失败，委托可能仍在券商侧有效：cancel rejected

### 尝试过但回退的改动

第一版我改成了 `order.status = "cancel_requested"`，本意是"等券商确认"。
但检查后发现：

- `OPEN_STATUSES = ("new", "open", "partially_filled")` 不包含它
- `LiveReconciler` 不认识这个状态，不会推进它

结果会是订单变成"孤儿"——既不在待处理列表里，也不会被任何后台任务推进。
这是一个比原问题更糟的状态。回退，保持 `cancelled` 只在实际撤单成功时写。

## 设计取舍

- **为什么不重试**：下单不是幂等操作。网关超时后重试可能造成重复下单，
  这比失败更危险。撤单同理。重试应该由重连器（reconciler）在拿到
  明确的券商状态之后决定。
- **为什么记录成 `rejected` 而不是 `failed`**：状态机里已有 `rejected`，
  前端与统计都认识它。新增状态会让所有消费方都要改。
- **为什么 `reject_reason` 用英文前缀**：`gateway error before ack` 是一个
  机器可识别的标记，方便日志检索；具体异常文本在后面，给人看。
- **为什么不在这一层做熔断**：连续失败应由运维观测（系统页的错误率）发现。
  在交易服务里做熔断会把"网关暂时慢"升级成"禁止交易"，风险更大。

## 验证

| 检查项 | 结果 |
|---|---|
| 网关抛异常 → 409 而非 500 | ✅ 新增测试 |
| 失败订单落库为 rejected 且带原因 | ✅ 新增测试 |
| 失败下单不扣现金、不建持仓 | ✅ 新增测试 |
| 撤单失败 → 409 且订单状态不变 | ✅ 新增测试 |
| 撤单失败文案说明"可能仍有效" | ✅ |
| Reconciler 单笔轮询失败后继续遍历 | ✅ 新增测试 |
| 后端全量回归 | ✅ 372 passed（新增 4 项） |

## 后续

- 网关连续失败目前只会体现在系统页的错误率与审计日志里。
  可以给风险中心加一条"网关健康"指标（最近 N 次调用成功率）。
## 补充：对账器已经是对的

上一轮文档留下一条："对账器在网关不可用时的行为尚未验证。" 本轮一并确认。

`LiveReconciler.tick()` 对每个订单的轮询都包在 try 里：

```python
try:
    result = gateway.order_status(order.broker_order_id)
except Exception as exc:
    logger.warning("status poll failed for %s: %s", order.broker_order_id, exc)
    continue
```

实测三个订单、中间一个抛异常：

```
WARNING status poll failed for b1: gateway hiccup
polls: ['b0', 'b1', 'b2']   # 三个都轮询了
```

行为正确：单笔失败记 warning，其余继续。这是"一个失败不该让整批停下"的
正确实现，无需改动。已固化为测试 `test_reconciler_keeps_polling_after_a_status_failure`。

## 后续（更新）

- 网关连续失败目前只会体现在系统页的错误率与审计日志里。
  可以给风险中心加一条"网关健康"指标（最近 N 次调用成功率）。
