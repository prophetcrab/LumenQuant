# 迭代记录 C81：自动运行的满仓下单必然失败

## 动机

C80 修完手动下单的三处限额缺陷后，检查自动运行的建仓路径
`RuntimeService._place_entry`——它独立计算数量，不走手动下单的校验。

## 缺陷：按 budget/price 计算数量，不给手续费留额度

```python
budget = min(available * row.position_pct, row.max_order_notional)
raw = budget / price
quantity = math.floor(raw / lot) * lot
```

`place_order` 收的是 `notional + fee`：

```python
cost = notional + notional * intent.fee_bps / 10_000.0
if cost > ctx.cash:
    raise ConflictError("insufficient cash")
```

两者不一致。实测（可用 10,000、费率 10bp）：

```
price: 70254.19  qty: 0.1423  notional: 9997.17
full-size buy: 409 insufficient cash: need 10007.17, available 10000.0
```

**任何 `position_pct=1.0` 的实例都会稳定地失败**，而配置成满仓恰恰是最常见的
用法之一。错误信息说"资金不足"，但账户里明明有钱——差额正是那笔手续费。

### 修复

给 `TradingService` 增加一个公开的预算接口，由它统一算"含费后还买得起多少"：

```python
def max_affordable_quantity(account_id, symbol, price, budget, *, lot_size=None):
    rate = self.fee_bps / 10_000.0
    raw = budget / (price * (1.0 + rate)) if rate > 0 else budget / price
    quantity = round_qty(math.floor(raw / lot) * lot)
    # 用真实费用函数确认，A 股佣金有 5 元下限，解析解可能偏大
    while quantity > 0:
        notional = quantity * price
        if notional + self._fee(market, "buy", notional) <= budget:
            return quantity
        quantity = round_qty(quantity - lot)
    return 0.0
```

三个要点：

- **floor 而不是 round**：向上取整会超预算；而且 `round_qty` 只是裁掉多余小数位，
  并不对齐 lot step，两者不能混用（第一版写成 `round_qty(raw / lot) * lot`，
  结果 50 元预算配 100 元一手时返回了 0.5 手）；
- **用真实费用函数复核**：A 股佣金最低 5 元，解析解在小额单上会偏大；
- **lot 由调用方传入**：调用方已经解析过 symbol，避免与价格端口对 lot 的理解不一致。

`_place_entry` 改为调用它，并把硬编码的最小金额 `10` 换成
`self.trading.policy.limits.min_order_notional`——部署提高该阈值时不该被旧常量卡住。

## 验证

### 单元

| 用例 | 断言 |
|---|---|
| 满预算含费 | `notional + fee <= budget`，订单能成交，现金不为负；零费率时能买更多 |
| 限额边界（既有） | 单笔上限低于一手时仍报"超过单笔上限"而非"资金不足" |

还原为 `budget / price` 后：

```
assert (10000.0 + 10.0) <= (10000.0 + 1e-09)   ← 失败
```

### 真实 A 股数据

```
price: 1312.5  budget: 300,000
affordable qty: 200 股
notional 262,500.00 + fee 68.25 = 262,568.25 <= 300,000: True
order: filled 200 @ 1312.5
cash left: 37,431.75
```

注意 200 股而非 228 股：一手 100 股、单价 1,312.5，三手要 393,750 超过预算，
所以按整手向下取整到 200 股——这是正确行为。

## 回归

后端 **484 passed**（新增 1 项）。

## 已知限制

1. **不含滑点**：预算只覆盖手续费，实际成交价可能更高；`slippage_bps` 由撮合层
   计入成交价，若滑点方向不利，极满仓的单子仍可能资金不足（此时会正常报错，
   不会静默超额）。
2. **仅买入方向**：卖出不需要预算，`max_affordable_quantity` 只服务开仓。
3. **runtime 测试用零费率**：`tests/unit/test_runtime.py` 的 fixture 是 `fee_bps=0`，
   所以该缺陷只能由 `test_trading.py` 的含费用例覆盖；runtime 侧靠复用同一函数间接保证。
