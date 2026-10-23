# 迭代记录 C95：自动交易实例的两处稳定性缺陷

## 排查范围

沿着"所有交易方向"梳理 `RuntimeService` 的路径：

| 方向 | 入口 |
|---|---|
| 开仓 | `evaluate_instance` → `_place_entry` |
| 加仓 | 同一标的已持仓时记录 `position_already_open` |
| 策略平仓 | `evaluate_instance` → `close_position` |
| 止损/止盈/移动止损 | `protect_positions` → `close_position` |
| 重启 | `set_running` |

发现两处会直接损害交易稳定性的缺陷。

## 缺陷一：实盘离场单每 2 秒被撤销重发

`protect_positions` 每个调度 tick 都会检查。实盘网关是**异步成交**的：下单后
返回 `accepted/open`，真正成交要等交易所。

用"接受但暂不成交"的网关复现（4 个 tick）：

```
tick 1: placed=1 cancels=0
tick 2: placed=2 cancels=1
tick 3: placed=3 cancels=2
tick 4: placed=4 cancels=3
```

**8 秒内下了 4 单、撤了 3 单，没有一单活到成交。**

根因是 C82 的修复：`close_position` 在提交前会撤销该标的的所有挂卖单，
本意是清掉陈旧限价单。但它把**自己上一轮提交的离场单**也撤了，然后重新提交。
持仓一直存在 → 每轮都判定"要离场" → 无限循环。

对止损来说这是致命的：仓位永远平不掉，而日志显示"止损已触发"。

### 修复

给自动离场的委托打标记（`exit:<instance_id>`），`close_position` 遇到在途的
离场单时直接复用它而不是撤销重发：

```python
inflight = [o for o in self.open_orders(...)
            if o.symbol == code and o.side == "sell" and _is_exit_source(o.source)]
if inflight:
    return self._order_view(sorted(inflight, key=lambda o: o.created_at)[0])
```

`_is_exit_source` 要剥掉 `live:` 前缀再判断——实盘订单行存的是
`live:exit:xxx`，第一次实现直接 `startswith("exit:")` 匹配失败，修复无效。
这个细节是被测试打回来的。

### 副带问题：在途单被当成新执行

修复前每轮还会 `executions += 1` 并抛一条 critical 告警。一个慢成交的止损会
在 10 秒内产生 5 条"止损触发"告警，把真正的事件埋掉。

新增 `close_position_ex()` 返回 `(order, reused)`，让调用方能区分"刚提交"与
"在等待"：

```
tick 1: stop_loss           ← 提交，executions=1，告警 1 条
tick 2-5: stop_loss_pending ← 只记一条 info 日志
```

## 缺陷二：重启后沿用陈旧的移动止损峰值

`set_running(False)` 只是把 `running` 置 false，`peak_price` 保留原值。

复现：持仓期间峰值记到 200，然后停用实例；价格跌到 120 后重新启用——

```
restart outcome: {'...': 'trailing_stop'}    ← 立即平仓
```

**重启那一刻就把一个健康仓位清掉了。** 因为移动止损按"从峰值回撤 40%"计算，
而 200 这个峰值是停用前的，当前价格从未接近过它。

### 修复

启动时把 `peak_price` 归零，保护从重启价格重新建立：

```python
if running:
    row.peak_price = 0.0
```

验证：重启后不再误触发；随后真实的 -10.8% 回撤仍能正常触发。

## 验证

### 单元（4 项，`tests/unit/test_pending_exit.py`）

| 用例 | 断言 |
|---|---|
| 在途离场不重发 | 5 个 tick 只下 1 单、0 撤单，后续均为 `_pending` |
| 在途离场不重复告警 | 5 个 tick 只产生 1 条告警 |
| 标记能穿透 `live:` 前缀 | 订单 source 以 `live:exit:` 开头 |
| 重启不沿用陈旧峰值 | 重启后 peak=0、首个 tick 不触发；真实回撤仍触发 |

两项修复都做了还原验证（分别 1 项测试失败）。

### 真实数据

腾讯实时行情，模拟 A 股账户，持仓 600519.SH @ 1335.00，止损 -3%，价格打到 -5%：

```
tick 1: {'...': 'stop_loss'}
tick 2: {}
tick 3: {}
executions=1  positions=[('600519.SH', 0.0)]  alerts=1
```

一次执行、一条告警、仓位清零，后续 tick 无重复动作。

## 回归

- 后端 **527 passed**（新增 4 项）
- 前端 66 passed，`tsc` 干净，构建通过

## 整理

排查过程中有两份测试草稿写进了 `test_stops.py`，与新建的
`test_pending_exit.py` 重复。删除前者中的重复段落后全量转绿。

## 已知限制

1. **在途单没有超时**：若网关永远不回报成交状态，离场单会一直挂着，实例
   持续记 `_pending`。需要人工介入或依赖网关侧撤单，本轮未加自动超时重发。
2. **只对卖出方向生效**：买入是单次事件，没有重复触发的问题。
3. **`peak_price` 归零意味着重启后重新累积**：对于刚建仓就重启的场景，
   移动止损的参照点会推迟，这是"宁可少触发也不误触发"的取舍。
