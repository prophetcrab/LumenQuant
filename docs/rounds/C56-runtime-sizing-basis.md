# 迭代记录 C56：运行时的仓位基准用错了钱

## 动机

项目最核心的正确性主张是"回测与实盘行为一致"。C27 用共享止损模块统一了
两者，C36/C37 统一了研究链路。这一轮检查**仓位规模**这一层。

## 缺陷：按账面余额计算仓位，忽略了冻结资金

回测里：

```python
budget = cash * cfg.position_pct      # cash 是引擎里可自由支配的现金
```

运行时里：

```python
cash_budget = account.cash_balance * row.position_pct
```

`cash_balance` 是**账面余额**，不扣除挂单冻结。当账户里有未成交的限价买单时，
`available_cash < cash_balance`，两者不同。

### 实测

100k 账户，先挂一笔约 60k 名义的限价买单（限价设得很低，不会成交）：

```
cash=100000.00 available=69974.41
```

然后用一个 `position_pct=0.5` 的实例开仓：

```
entry notional=50010.67   (50% of cash=50000.00, 50% of available=34987.20)
```

**实例花了 5 万，但当时只有 7 万可用**——它把已经被冻结的 3 万也算进了自己的预算。
若那笔挂单随后成交，账户会后继无力，甚至触发执行期的资金不足拒绝。

### 修法

```python
available = self.trading.available_cash(row.account_id)
cash_budget = max(0.0, available * row.position_pct)
```

修后：

```
entry notional=34994.68 vs 50% of available=34987.20   PASS
```

差额 7 元来自手续费与手数取整。

## 为什么这是一个"一致性"问题而不只是"资金"问题

回测没有挂单冻结这个概念——它每根 K 线至多持有一个仓位，资金是纯现金。
运行时多了"挂单"这个状态，于是同一份 `position_pct` 在两种环境下
代表的资金规模不同。

这不是简单的 bug，而是两个执行模型的差异。选择"按可用现金"而不是"按账面余额"：

- 回测的 `cash` 就是可用现金，对齐它才是真正的一致
- 按账面余额会让自动化策略抢走手动挂单的资金，这是用户没有预期的行为

## 验证

| 检查项 | 结果 |
|---|---|
| 有冻结资金时仓位按可用现金计算 | ✅ 新增测试 |
| 开仓后账面余额与可用现金仍自洽 | ✅ |
| 无冻结资金时行为不变（全量回归） | ✅ 373 passed |

## 顺带确认：平仓路径不需要改

运行时的卖出走 `trading.close_position()`，它内部：

```python
quantity = position.available_quantity if market == "cn_stock" else position.quantity
if quantity <= 0:
    raise ConflictError("无可卖数量（T+1：当日买入次日可用）")
```

T+1 与可用数量检查都在交易服务里，运行时不需要重复实现。
这也符合模块边界：执行规则只在 trading 模块。

## 设计取舍

- **为什么不用 `account.cash_balance - reserved_cash` 自己算**：
  `trading.available_cash()` 已经有这个逻辑，且未来的冻结来源（例如
  保证金、手续费预扣）只会加在那一处。重复实现会分叉。
- **为什么 `max(0.0, ...)`**：可用现金理论上不会为负（冻结不超过账面），
  但如果出现负值，`position_pct × 负数` 会产生负的预算，
  后续 `quantity <= 0` 的判断会给出误导性的错误信息。
- **为什么不把 `max_order_notional` 也按可用现金缩放**：它是硬上限，
  语义是"单笔最多花这么多"，与账户余额无关。

## 追加：A 股卖出资金也做 T+1

上面最后提到的差异直接做掉了。原来 A 股卖出后，款项立刻可以再买入：

```
sell: 201
after sell: cash=199989.48 available=199989.48   ← 完全相同
re-buy same session: 201                          ← 实际券商不会放行
```

真实的 A 股是资金 T+1 到账（与股份 T+1 对称）。实现：

- 新表 `cash_holds`：`(account_id, amount, release_day, ref)`
- 卖出成交时按净额（扣除佣金与印花税）写一条 hold，`release_day` = 下一个交易日
- `available_cash = book_cash - reserved_by_orders - held_proceeds`
- `settle_t1()` 在释放股份可用量的同时删除该账户的全部 hold

修后：

```
after sell: cash=199989.48 available=199002.99 (diff=986.49)   ← 净额被锁定
after settle: available == cash                                   ← 结算后释放
```

### 顺带发现：组合页自己算了一套可用现金

`PortfolioService.overview()` 原本：

```python
available_cash = round_money(cash - reserved)
```

它没有调用 `TradingService.available_cash()`，所以加了 hold 之后两边会打架。
改为委托：

```python
available_cash = self.trading.available_cash(account_id)
```

这正是"允许重复代码但要知道代价"的一个实例：两处实现看起来一样，
加了第三个扣减项之后就分叉了。可用现金只有一个定义，放在交易服务里。

### 为什么 `settle_t1` 释放全部 hold 而不是按日期过滤

这个方法**就是**结算事件本身——"新的交易日开始了"。按 `release_day <= today`
过滤会在结算早于日历翻页时留下卡住的 hold（测试里就是这样）。
释放全部持有是语义上正确的：开盘结算意味着昨天卖出的钱今天可用了。

## 后续

- 回测引擎目前仍没有"卖出资金 T+1 到账"的概念。A 股回测里同一天卖出后
  立刻买入是允许的，而实盘会被 hold 挡住。这是**已知的研究与实盘差异**，
  需要在 `BacktestEngine` 里引入资金可用时间。列入下一轮。
