# 迭代记录 C82：挂单可以永久阻止止损平仓

## 动机

C81 修完建仓预算后，检查平仓路径 `close_position`——止损与移动止损都走它。

## 缺陷：平仓数量被重复扣除预留

```python
quantity = (position.available_quantity if market == "cn_stock" else position.quantity)
```

`available_quantity`（在 `positions()` 视图中）已经减去了挂单冻结：

```python
sellable = max(0.0, position.available_quantity - self.reserved_quantity(...))
```

而 `place_order` 内部又算一次：

```python
position_commit = round_qty(
    (position.available_quantity if market == "cn_stock" else position.quantity)
    - self.reserved_quantity(account_id, symbol)
)
```

于是同一笔预留被扣两次。实测（持仓 1.0，挂着 0.4 卖单）：

```
before: quantity 1.0, available 0.6
close:  409 insufficient position: hold 0.6, sell 1.0
```

**平仓请求发的是 1.0，而可用量已经被扣成 0.6。**

## 为什么这很严重

`protect_positions()` 的止损、移动止损、止盈全部调用 `close_position`。
一封普通的未成交限价卖单，就能让**所有保护性离场永远失败**：

- 日志里只会反复出现一条"insufficient position"；
- 仓位继续暴露在市场里；
- 用户以为挂单只是"等个好价"，实际是把止损彻底关掉了。

## 修复：平仓前先撤掉同标的的挂单

平仓的语义是"这个仓位不再存在"，那么为它工作的挂单也没有存在意义：

```python
for order in self.open_orders(account_id=account_id):
    if order.symbol.upper() == code and order.side == "sell":
        self.cancel_order(order.id, account_id=account_id)
```

然后按**总持仓**下单，让 `place_order` 只扣一次预留。

选择"撤单"而不是"允许超额卖出"的理由：后者会让账本出现负持仓或
卖出不存在股份的风险，而撤单是用户能理解的确定性动作，且撤销本身可审计。

## 验证

### 单元

```
持仓 1.0，挂 0.4 卖单：
  before: available 0.6
  close:  200 filled=1.0
  after:  持仓清空，挂单状态 cancelled，无残留
```

还原撤单逻辑后：`ConflictError: insufficient position: hold 0.6, sell 1.0`。

### T+1 仍然有效

撤单修复不能顺带放宽 A 股的当日买入限制。集成测试构造一个
`quantity=100, available_quantity=0` 的持仓，平仓仍被拒绝（"T+1"/"可卖"）。

## 回归

后端 **486 passed**（新增 2 项）。

## 已知限制

1. **撤单失败则平仓失败**：实盘网关拒绝撤单时，`cancel_order` 抛
   `ConflictError` 并保留本地 open 状态（既有行为），平仓随之中止。这是
   正确的保守选择——不能在券商侧还挂着卖单时再发一笔。
2. **只撤卖单**：同一标的的买入挂单不影响平仓，保持不动。
3. **不区分来源**：手动挂的卖单也会被撤；平仓是"清空该标的"的强语义，
   这是有意为之。
