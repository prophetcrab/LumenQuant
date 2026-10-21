# 迭代记录 C39：实盘自动运行的安全缺口

## 动机

实盘相关代码是风险最高的部分，值得单独复核一次。复核目标很具体：
**是否所有真实下单路径都被"显式开启"约束住。**

## 缺陷一：自动运行完全绕过实盘确认

原有的实盘保护是三层：

| 层 | 位置 | 作用 |
|---|---|---|
| `LQ_LIVE_TRADING_ENABLED` | 配置 / 创建实盘账户 | 服务端总开关 |
| 网关 `ready` | 下单前 | 通道必须可用 |
| `X-Live-Confirm: true` | **HTTP 请求头** | 防误触 |

问题在第三层：它是**HTTP 中间件层面的检查**，而自动运行的路径是

```
调度器 → RuntimeService.evaluate_instance → trading.place_order(...)
```

完全不经过 HTTP。于是：

```
实盘账户 + 一个 start=true 的实例  →  真实订单发出，没有任何确认
```

实测确认：

```
manual order without header: 403 live order requires header X-Live-Confirm: true
instance create: 201
  tick: executed
orders on live account: [('600036.SH', 'buy', 'open', 'live:strategy:387da015')]
```

同一秒钟内：手动下单被拒绝，自动下单成功。用户以为"实盘需要确认头"就是安全的，
实际上只要建一个实例就能绕开它。

### 修法

引入独立的开关 `LQ_LIVE_AUTO_TRADING_ENABLED`，默认 `false`：

```python
if account.kind == "live" and not self.live_auto_trading_enabled:
    raise ForbiddenError(
        "实盘账户的自动运行需要 LQ_LIVE_AUTO_TRADING_ENABLED=true；"
        "仅开启 LQ_LIVE_TRADING_ENABLED 只允许手动下单"
    )
```

为什么是**独立开关**而不是复用现有总开关：这两件事的风险性质不同。
手动下单时有人在看屏幕；自动运行是无人值守的。
"我允许自己在盯盘时下实盘单"不等于"我允许程序半夜自己下单"。

## 缺陷二：关闭开关后，已存在的实例仍在下单

第一版修复只检查了创建时。验证第二个场景：

```
1. live_auto_trading_enabled=True → 创建实例成功
2. 重启，开关改为 False
3. 调用 tick × 3
```

结果：

```
orders before/after ticks with switch off: 0 1
sources: ['live:strategy:b520db03']
```

**开关关掉了，订单照样发出。** 运维人员关掉开关、重启服务，
以为机器人停了，实际它还在交易。这个场景比第一个更危险——因为它发生在
"我已经意识到风险并采取了措施"之后。

### 修法

在 `evaluate_instance` 入口每次都检查：

```python
balance = self._live_guard(row)
if balance is not None:
    return balance
```

修复后同样的场景：

```
tick: live_auto_disabled
tick: live_auto_disabled
 orders: []
events: [('warn', 'live_auto_disabled'), ('info', 'created')]
```

日志只记一次（复用 C34 的去重窗口），不会每 6 秒刷屏。

## 刻意保留的例外：保护性离场不受该开关限制

`protect_positions()` **不**检查这个开关。原因：

关闭自动开仓的目的是"不要再自己建新仓"。如果已有实盘持仓，
禁止止损平仓会让它**失去保护**——这比允许平仓危险得多。
在 -10% 的位置因为一个开关而拒绝止损，是安全设计走向了反面。

代码注释写明了这一点，避免未来有人"顺手统一"：

```python
# Deliberately NOT gated on live_auto_trading_enabled: turning off new
# automated entries must not also disable protective exits.
```

## 验证

| 检查项 | 结果 |
|---|---|
| 手动实盘下单仍需 `X-Live-Confirm` | ✅ 测试 |
| 未开自动开关时无法创建实盘实例 | ✅ 403 + 明确文案 |
| 开启后可以创建 | ✅ 201 |
| **关闭开关后已存在实例停止下单** | ✅ 3 次 tick 全为 `live_auto_disabled`，0 订单 |
| 告警只记一次 | ✅ |
| 模拟账户不受该开关影响 | ✅ |
| 后端全量回归 | ✅ 351 passed（新增 5 项） |

## 安全边界（更新后）

真实下单需要同时满足：

**手动**：`LQ_LIVE_TRADING_ENABLED` + 网关 ready + `X-Live-Confirm: true`
**自动**：以上 + `LQ_LIVE_AUTO_TRADING_ENABLED`（每次调度复查）

密钥仍然只从环境读取，不写入数据库（未改动）。

## 后续

- 运行时的日志里目前用 `live_auto_disabled` 表示被拦截，
  但没有区分"从未开启"和"曾经开启后被关闭"。可以加一个计数。
- 实盘实例的创建建议要求二次确认（前端弹窗 + 输入实例名），
  这属于 UI 层防误触，与后端开关互补。
