# 迭代记录 C97：价格中断时裸奔的仓位没有告警

## 缺陷

`protect_positions` 在取不到价格时的处理是：

```python
except Exception as exc:
    row.last_error = f"无法获取 {row.symbol} 价格：{exc}"[:400]
    self._log(row.id, "error", "price_unavailable", row.last_error, ...)
    outcomes[row.id] = "price_unavailable"
    continue
```

只有 `last_error` 和一条日志。**没有告警。**

对比同函数里的另一条失败路径——`close_position` 抛异常时有
`level="critical"` 的 `exit_failed` 告警。两者的后果完全相同：仓位在没有
止损保护的情况下继续持有。

### 实测

价格源抛异常后连续 3 个 tick：

```
feed down 1: {'...': 'price_unavailable'}
feed down 2: {'...': 'price_unavailable'}
feed down 3: {'...': 'price_unavailable'}
last_error: 无法获取 BTCUSDT 价格：no price
alerts: []            ← 一条都没有
```

运行日志里确实写了 3 条 error，但没人盯着日志看。告警中心、侧栏角标、
风险页全都显示正常。

## 修复

补上与 `exit_failed` 同级的告警：

```python
self.alerts.raise_alert(
    category="price_unavailable",
    level="critical",
    account_id=row.account_id,
    title=f"{row.symbol} 无法获取价格，止损失效",
    message=f"{row.strategy_key} · {row.symbol}：{exc}。"
            "持仓在价格恢复前没有任何止损保护。",
    ref_type="strategy_instance",
    ref_id=row.id,
)
```

告警服务自带的 5 分钟去重让持续中断只留一条记录——不需要在这里另做节流。

## 验证

| 断言 | 结果 |
|---|---|
| 每次 `price_unavailable` 都记入 outcome | ✓ |
| 3 个 tick 只产生 1 条告警 | ✓ |
| 级别为 critical、标题含「止损失效」 | ✓ |

还原告警代码后该测试失败。

## 回归

- 后端 **530 passed**（新增 1 项）

## 这一轮的三处修复，共性是什么

C95–C97 修的三个缺陷都在**自动交易的失败路径**上：

| 缺陷 | 表现 | 用户能否察觉 |
|---|---|---|
| 离场单重复提交 | 止损永远平不掉 | 不能，日志显示"已触发" |
| 在途买单重复提交 | 仓位翻倍 | 不能，每笔都"成功" |
| 价格中断无告警 | 仓位裸奔 | 不能，页面全部正常 |

它们都不产生报错、不违反任何断言、测试全部通过。共同点是**系统在"看起来
正常"的状态下做了错误的事**，而唯一的信号埋在没人看的地方。

这也是本轮把 `outcomes` 从"只有成功"扩展成一串显式状态
（`stop_loss` / `stop_loss_pending` / `price_unavailable` / `exit_failed`）
的原因：让每个 tick 的结果都能被机器和人都读懂。

## 已知限制

1. **恢复后不会自动确认告警**：价格恢复时不会 acknowledge 那条
   `price_unavailable`，需要人工点确认。数据源健康告警（C74）有自动恢复，
   这里没有——因为"价格恢复"不等于"这段时间没有错过止损"。
2. **不记录中断时长**：告警只有原因，没有"从几点开始"。要判断错过多少行情
   仍需翻运行日志。
