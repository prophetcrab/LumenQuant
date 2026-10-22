# 迭代记录 C80：持仓限额在实时行情下有两处失效

## 动机

C74 修过"取不到价被当成价格 0 会误触发熔断"。沿同一条资金安全路径继续查，
`max_position_notional`（单标的持仓上限）在真实行情下有两处独立缺陷。

## 缺陷一：敞口按成本价计算

```python
projected = ctx.position_qty * ctx.position_avg_price + notional
```

持仓限额管的是**敞口**，而敞口是"现在值多少"，不是"当初花了多少"。
成本 100、现价 200 的持仓承担着两倍于账面成本的风险，但限额只看到成本。

后果：价格上涨后加仓，实际敞口可能翻倍而限额检查认为还很宽松。

### 修复

`RiskContext` 新增 `position_market_price`，由 `TradingService` 传入实时价：

```python
mark = ctx.position_market_price or ctx.position_avg_price
projected = held * mark + notional
```

取不到价时回落到成本（`0.0` 表示未知），与 C74 的估值回落保持一致。

## 缺陷二：A 股当日买入完全不计入敞口

这个更严重，是真实数据端到端验证时才暴露的：

```
限额 150,000
第一笔买入 100 股（约 131,000）：201 filled          ✓ 应放行
第二笔买入 100 股（合计约 262,000）：201 filled      ✗ 应拦截
```

根因在 `position_commit`：

```python
position_commit = round_qty(
    (position.available_quantity if market == "cn_stock" else position.quantity)
    - self.reserved_quantity(account_id, symbol)
)
```

A 股当日买入的 `available_quantity` 是 **0**（T+1 冻结），于是
`ctx.position_qty = 0`，`projected` 只剩新单本身，限额形同不存在。
**每个 A 股账户当日的第二次买入都不受持仓上限约束。**

### 为什么不能直接改成 quantity

卖出校验用的是同一个 `ctx.position_qty`：

```python
if intent.quantity > ctx.position_qty + 1e-9:
    raise ConflictError("insufficient position")
```

如果把它换成总持仓，A 股 T+1 就被绕过了——当日买入立刻可卖。

"能卖多少"和"持有多少"是两个不同的问题，必须分开：

| 字段 | 含义 | 用途 |
|---|---|---|
| `position_qty` | 当前可卖（T+1 + 挂单冻结后） | 卖出校验 |
| `exposure_qty` | 全部持有 | 敞口限额 |

`exposure_qty` 缺省为 0 时回落到 `position_qty`，保持既有调用方行为。

## 验证

### 单元

| 用例 | 断言 |
|---|---|
| 成本 vs 市价 | 同一持仓在现价翻倍后，加仓被限额拦截；成本估值则放行 |
| A 股当日买入 | `position_qty=0` 但 `exposure_qty=100` 时，加仓被拦截；同日卖出仍被 T+1 拒绝 |

两个测试都做了还原验证：

```
还原 mark 逻辑      → DID NOT RAISE ConflictError
还原 exposure_qty   → DID NOT RAISE ConflictError
```

### 真实数据端到端

限额 150,000，账户 600,000，标的 600519.SH（现价约 1,310）：

```
first buy (100 sh):  201 filled
second buy (100 sh): 409 projected position value 261,862.0 exceeds the notional limit 150,000.0
same-day sell:       409 可用数量不足：可卖 0 股（T+1 当日买入次日可用）
final position:      quantity 100, available 0, market_value 130,931
```

两个约束同时成立：限额拦住加仓，T+1 拦住当日卖出。

## 回归

后端 **482 passed**（新增 2 项）。

## 已知限制

1. **只约束单标的**：没有组合层面的总敞口上限（跨标的求和）。
2. **市价缺失时回落到成本**：与熔断的估值策略一致，但那意味着报价中断期间
   限额会偏松；数据源健康告警（C74）会同时提示。
3. **限额是静态配置**：不随账户权益变化按比例调整。
