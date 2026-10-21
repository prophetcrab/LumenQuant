# 迭代记录 C43：账户归档后实例仍在交易

## 动机

账户归档是一个"停止使用"的声明。归档后：
下单被拒（409）、入金被拒（409）、历史仍可查——这些都正确。
但还有一个漏洞没被测过：**归档时正在运行的自动实例。**

## 缺陷

在一个有持仓、有运行中实例的账户上执行归档：

```
buy: 201
instance: 201
archive: 200 archived
positions still readable: 200
orders still readable: 200
overview still readable: 200
ledger: 200
can still trade: 409          ← 正确
can deposit: 409              ← 正确
tick after archive: 200 executed   ← 错误
instance running flag: True        ← 错误
```

归档之后，下一次调度 tick 返回 `executed`——**实例照常交易**。

### 根因

`set_running` 里检查了账户状态：

```python
if running:
    account = self.accounts.require(row.account_id)
    if account.status != "active":
        raise ConflictError("account is archived")
```

但这个检查只在**启动实例时**发生。归档账户时没有任何代码去看该账户上的实例，
调度器也从不复查账户状态。所以：

1. 实例先启动（账户 active）
2. 账户后归档
3. 实例继续运行，直到有人手动停止

这个顺序在现实中很常见：先发现"这个账户不对劲"，然后去归档，
而不是先记得去停实例再归档。

## 修法（两层）

### 1. 归档即停止实例

`AccountService.update()` 在状态变为 `archived` 时，把该账户所有运行中的实例
置为 `running = False`，并把数量写进审计：

```python
"stopped_instances": len(stopped)
```

这样归档的语义完整："这个账户停止一切交易活动"。

### 2. 执行时兜底

`evaluate_instance()` 在每次评估前复查账户状态：

```python
account = self.accounts.require(row.account_id)
if account.status != "active":
    self._log(row.id, "warn", "account_archived", "账户已归档，跳过执行", ...)
    return "account_archived"
```

这层不是多余的：数据库里可能已经存在"归档前创建、且本次修复之前没有停止"的实例，
也可能有人直接改库把 `running` 改回 true。执行时的检查是最后的防线。

修完后：

```
archive: 200
instance running after archive: False
tick: {}
logs: [('info', 'created')]
```

## 设计取舍

- **为什么归档要停止实例，而不是只在执行时拒绝**：只拒绝执行的话，
  实例会一直"运行中"但永远不做事，日志里每轮刷一条跳过记录，
  用户看到的状态是假的。显式停止让界面与事实一致。
- **为什么两层都要**：业务层的动作（归档 → 停实例）保证正常路径正确；
  执行层的检查保证异常路径（历史数据、直接改库）也不会造成意外交易。
  这和 C39 的实盘开关是同一个思路。
- **为什么归档不强制平仓**：平仓是有成本的交易行为，应该在用户明确要求时发生。
  归档只是"不再有新动作"，已有持仓保持原样，用户可以后在另一个账户处理。

## 验证

| 检查项 | 结果 |
|---|---|
| 归档后实例 `running` 变为 false | ✅ 新增测试 |
| 归档后 tick 无待评估实例 | ✅ 新增测试 |
| 归档后持仓/订单/组合/流水仍可读 | ✅ 新增测试 |
| 归档后下单与入金被拒 | ✅ 新增测试 |
| 强制把 running 改回 true 仍不交易（执行兜底） | ✅ 新增测试 |
| 审计记录停止的实例数 | ✅ |
| 后端全量回归 | ✅ 358 passed（新增 4 项） |

## 后续

- 归档账户的持仓仍在组合页计入总权益。如果用户归档是为了"隔离"，
  可能需要一个"归档账户不计入汇总"的选项。
- 恢复归档（`archived` → `active`）不会自动重启实例，这是刻意的：
  恢复账户不等于恢复自动化。
