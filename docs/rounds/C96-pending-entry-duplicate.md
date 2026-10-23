# 迭代记录 C96：在途买单被下一根 bar 重复提交

## 缺陷

C95 修好了离场方向。同一轮检查开仓方向时发现了对称的问题。

实盘买单提交后保持 `open` 直到交易所成交，在此之前**持仓仍然是 0**。
而 `evaluate_instance` 的开仓条件是"没有持仓"：

```python
if signal.action == "buy" and position <= 0:
    self._place_entry(row, last.close)
```

`last_bar_time` 只保证**同一根 bar** 不重复评估。一旦进入下一根 bar，
`position` 仍是 0（上一单还没成交），于是又下一单。

复现（每根 bar 都产生买入信号）：

```
bar1: executed  placed=1
bar2: executed  placed=2      ← 500 股 与 250 股同时在途
bar3: executed  placed=3
bar4: executed  placed=4
```

**一个配置为 50% 仓位的实例，在 4 根 bar 内累积了 4 笔买单。**
如果这些单陆续成交，实际敞口是设计值的四倍——而且第二、三、四笔的
数量因为可用资金已被预留而递减，最终仓位变成一个无法解释的混合体。

### 修复

开仓前先看在途买单：

```python
pending = self._pending_entries(row)
if pending:
    self._log(row.id, "info", "entry_pending",
              f"买入委托在途，等待成交（{pending[0].id[:8]}）", ...)
else:
    self._place_entry(row, last.close)
```

`_pending_entries` 按 `source LIKE 'strategy:<instance>'` 过滤，因此**只认自己
提交的单**：同一账户里另一实例或手工挂的买单不会阻塞本实例。

验证：4 根 bar 只下 1 单，订单表只有一条 `open` 的买单。

## 三个方向的对称性

把这两轮放在一起看，缺陷形状是同一个：**异步成交让"状态"滞后于"意图"**。

| 方向 | 判断依据 | 滞后导致 |
|---|---|---|
| 开仓 | `position <= 0` | 在途买单不被看见 → 每根 bar 重复下单 |
| 平仓/止损 | `position.quantity > 0` | 在途卖单不被看见 → 每个 tick 撤销重发 |
| 重启 | `peak_price` 持久化 | 陈旧峰值跨越停机 → 立即误触发 |

三处的修法也一致：**把"在途"显式建模**，而不是靠持仓状态推断。

## 验证

| 用例 | 断言 |
|---|---|
| 在途买单不重复 | 4 根 bar 只 1 单，订单表仅 1 条 open 买单 |
| 成交后不重开 | 模拟成交并建仓后，再来一根 bar 仍不追加 |

还原开仓守卫后第一个测试失败（`assert 4 == 1`）。

## 回归

- 后端 **529 passed**（新增 2 项）

## 已知限制

1. **在途单无超时**：网关长期不回报时，实例会一直记 `entry_pending`。
   与 C95 的离场路径一致，需要人工介入或依赖网关侧撤单。
2. **部分成交按"仍有在途"处理**：`partially_filled` 也算未完成，因此不会
   补单。若策略本意是"补足到目标仓位"，当前实现会偏保守。
3. **不跨实例协调**：两个实例配置同一标的时，各自独立判断，靠
   `position_already_open` 提示，不做合并。
