# 迭代记录 C98：自动交易调度循环的四个问题（一个丢信号、三个空转）

## 排查范围

沿着调度循环本身看 `StrategyRuntimeEngine.tick` 与 `RuntimeService`：

| 环节 | 每 tick 执行一次 |
|---|---|
| 保护性离场扫描 | `protect_positions` |
| 逐实例信号评估 | `evaluate_instance` |
| 每小时维护 | 日志裁剪、告警清理、孤儿 sleeve、快照降采样 |

C95–C97 修的都是**失败路径**（重复下单、漏告警）。这一轮看的是**正常路径**：
循环本身在"什么都没发生"时做了什么，以及它失败时会不会悄悄吞掉信号。

找到四处，一处是真缺陷，三处是空转。

## 缺陷一：策略抛异常后，那根 bar 被永久消费

`evaluate_instance` 原来的顺序是**先推进游标，再评估**：

```python
if last.time <= row.last_bar_time:
    return "no_new_bar"
row.last_bar_time = last.time      # ← 先记下"这根 bar 处理过了"
state = strategy.prepare(candles, params)
signal = strategy.evaluate(state, len(candles) - 1)
```

而调度器捕获异常后是**提交**的：

```python
except Exception as exc:
    row.last_error = str(exc)[:400]
    session.commit()               # ← 把刚推进的游标一起落盘
```

所以策略里任何一次崩溃（指标计算失误、`prepare` 里索引越界、依赖的行情字段
缺失）都会让**那根 bar 永远不再被评估**。下一个 tick 读到
`last.time <= row.last_bar_time` 直接返回 `no_new_bar`，`last_error` 留在行上，
界面上只看到一条错误文本，仓位没变。

这正是 C97 描述的形状：系统在"看起来正常"的状态下漏掉了一根 bar。

### 修复

把游标推进挪到"这根 bar 已有确定结论"之后：

```python
def evaluate_instance(self, row):
    ...
    state = strategy.prepare(candles, params)
    signal = strategy.evaluate(state, len(candles) - 1)

    if not signal.is_actionable:
        row.last_bar_time = last.time
        self.session.commit()
        return "hold"

    try:
        ...  # 下单
    except AppError as exc:
        ...  # 被拒也是这根 bar 的结论
    row.last_bar_time = last.time
    self.session.commit()
    return "executed"
```

三点取舍：

- **被拒（`AppError`）仍然推进游标**。风控拒绝、涨跌停、可用量不足都是这根 bar
  的确定结论；不推进就会每 6 秒重试同一笔必然失败的单。
- **崩溃（非 `AppError`）不推进**，下一个 tick 重试。策略评估是历史的纯函数，
  重跑安全。
- **重跑不会重复下单**：开仓方向有 C96 的在途买单检查，离场方向有 C95 的
  `close_position_ex` 复用。这两条保护让"至少一次投递"变得安全。

`_place_entry` 的日志也改为接收显式 `bar_time`，不再读 `row.last_bar_time`——
否则游标还没推进时，日志里会记上一根 bar 的时间。

## 空转二：每个 tick 都重读并重聚合 600 根 K 线

调度器每 6 秒跑一次，但 15m 的 bar 每 15 分钟才收一根。绝大多数 tick 的结论
都是 `no_new_bar`，而拿到这个结论要先把 600 根目标周期的 bar 读出来（1h 需要
从 2404 根 15m 聚合），再整段反序列化。

加了时钟闸门：先问"有没有可能已经有更新的 bar 收盘了"。

```python
if row.last_bar_time > 0 and not self._bar_may_have_closed(row):
    return "no_new_bar"
```

`MarketService` 新增两个方法：

```python
def now_ms(self, symbol) -> int: ...           # 这根 bar 的收盘判定用哪个时钟
def next_close_after(self, symbol, timeframe, open_time) -> int:
    """比 open_time 更新的 bar 最早何时可能完整。"""
```

`next_close_after` 刻意返回**下界**而非精确时刻：

- 加密：`open_time + 2 × step`（bucket 连续）；
- A 股日内：`following + step`，若下一个 bar 起点落在午休/收盘后则先跳到下一
  个时段开盘；
- A 股日线：下一个交易日的 15:00（不是午夜）。

下界的意义是**只会多查、不会漏查**：真实收盘因为休市只会更晚。任何时钟读不
出来、或标的/周期无法解析的情况一律返回"可能已收盘"，退回原路径。

游标反而超前于当前时钟时（合成历史跑在 60 倍速时钟上留下的"未来 bar"）也
直接放行——否则会等一根已经发生过的 bar 永远等不到。

### 实测

11,520 根种子数据的生产规模，单实例一次 quiet tick：

| | 耗时 |
|---|---|
| 有闸门 | 0.18 ms |
| 无闸门 | 13.67 ms |

**77 倍**。20 个实例、6 秒一轮时，从 273 ms 降到 3.6 ms。SQL 层面从 5 条降到
2 条。

## 空转三：止损扫描没有单实例隔离

`protect_positions` 整个循环包在一个 `try` 之外——`_position_row` 或
`evaluate_tick` 里任何未预料的异常（损坏的行、被移出目录的标的）都会让
**后面的实例全部得不到止损检查**，而且这一轮什么都不会记。

信号循环早就有隔离（`for row in running: try ... except`），止损没有。

### 修复

抽出 `_protect_one`，主循环逐个隔离：

```python
for row in self.running_instances():
    try:
        outcome = self._protect_one(row, prices)
    except Exception as exc:
        outcomes[row.id] = "protect_failed"
        try:
            row.last_error = f"止损检查失败：{exc}"[:400]
            self._log(row.id, "error", "protect_failed", ...)
            self.alerts.raise_alert(category="protect_failed", level="critical", ...)
        except Exception:
            logger.exception("could not record a failed stop check")
        continue
```

按 C97 的判据：一条没人看的日志不够，仓位正在裸奔，必须发 critical 告警。
记录失败的那几行本身也包在 `try` 里——否则一个已经失效的 session 会把
"记录失败"变成"扫描崩溃"。

**为什么不用 savepoint**：第一版给每个实例套了 `session.begin_nested()`，想让
失败实例的写入回滚、其它实例的保留。实测第二个实例立刻炸：

```
InvalidRequestError: Can't operate on closed transaction inside context manager
```

因为 `TradingService.place_order` 和 `AlertService.raise_alert` 内部各自
`commit()`，一次成功调用就把嵌套事务结束了，后续写入落在已关闭的事务上。
这个代码库里"服务层自行提交"是既定约定，savepoint 与之不兼容。去掉后，
失败实例之前那些实例已经写入的峰值/执行计数仍然保留（同一事务），这正是
想要的效果。

顺带按标的一次缓存报价。多个实例配置同一标的时，过去每实例各查一次行情，
现在整轮只查一次；失败也缓存，让每个实例仍各自报出自己的裸奔告警。

## 空转四：每次开仓、每条 info 日志的冗余查询

三处小账：

1. `_place_entry` 里有一行 `account = self.accounts.get(...)`，结果从未使用——
   每次开仓白做一次账户查询加视图构建。
2. `_position_quantity` 调 `trading.positions()`（为每笔持仓构建包含两次额外查询
   的视图）只为读一个数量；改成直接查 `Position` 行。
3. `_log` 不论级别都先去重查询一次。而只有 warn/error 会去重——info（信号、
   成交）是不同事件，本来就不该被吞。把查询挪进 `if level in {"warn","error"}`。

另外 `tick()` 一次读运行实例，同时传给止损扫描和信号循环。过去两处各查一次，
而且两次之间集合变更时两个阶段会看到不同结果。

## 附带修复：无 K 线时快照返回 500

`_crypto_snapshot` 的兜底分支引用了作用域里不存在的变量：

```python
raise NotFoundError(f"no candles for {symbol}")   # symbol 未定义，应为 row.code
```

新登记、还没导入历史的标的请求 `/api/market/snapshot` 会抛
`NameError` → 500，而不是 UI 期待的"暂无数据" 404。已改成 `row.code` 并补测试。

## 验证

### 新增 10 项测试

`tests/unit/test_runtime_hardening.py`（6 项）：

| 用例 | 断言 |
|---|---|
| 崩溃不消费 bar | 模拟调度器"捕获并 commit"后游标仍为 0；恢复正常后同一根 bar 能正常成交 |
| quiet tick 跳过读取 | 5 次评估 `candles` 读取计数不变；时钟越过下一收盘后 +1 |
| 时钟失效退回读取 | `next_close_after` 抛异常时仍读取并正确得出 `no_new_bar` |
| 止损单实例隔离 | 第一个实例 `_position_row` 抛错 → `protect_failed`；第二个仍 `stop_loss`；失败有日志 |
| 失败不回滚他人进度 | 后一个实例抛错时，前一个实例本轮记录的峰值仍在 |
| 同标的报价只查一次 | 两个实例一轮扫描 `last_price` 只被调用 1 次 |

`tests/unit/test_closed_only_bars.py`（2 项）：`next_close_after` 的加密与 A 股
（日内跨午休、日线次一交易日 15:00）分支。

`tests/integration/test_risk_api.py`（1 项）：停止实例但仍有持仓时，风控总览给出
警告。

`tests/integration/test_market_api.py`（1 项）：无 K 线的标的返回 404 而非 500。

### 还原验证

逐项把修复改回去，确认对应测试变红：

| 还原的改动 | 结果 |
|---|---|
| 游标改回"评估前推进" | 1 failed |
| 删除时钟闸门 | 1 failed |
| 止损异常隔离改回不隔离 | 1 failed |
| 删除报价缓存 | 1 failed |
| `row.code` 改回 `symbol` | 1 failed（`NameError`） |

### 回归

- 后端 **540 passed**，ruff 干净。

## 这一轮的共性

C95–C97 修的是**失败路径上做错事**。这一轮是**失败路径之外**的两件事：

| 类别 | 表现 | 用户能否察觉 |
|---|---|---|
| 异常吞掉信号 | 一根 bar 静默丢失 | 不能，只有 `last_error` 一行 |
| 空转 | 每 6 秒白算一遍 | 能，但只会表现为"实例多了就卡" |
| 隔离缺失 | 一个坏实例让全员失去止损 | 不能，直到价格真的穿过止损 |
| 变量名写错 | 500 而非 404 | 能，但会被当成"行情坏了" |

`outcomes` 里的 `protect_failed` 和 `price_unavailable`、`exit_failed`、`stop_loss`
现在是同一套语义：**每个实例每轮的结果都能被机器和人都读懂**，而不是
"成功的才写、失败的留白"。

## 已知限制

1. **时钟闸门依赖 provider 提供 `now_ms`/`next_close_after`**。任何拿不到的
   实现（测试替身、旧部署）一律退回完整读取，行为与改动前一致——这是刻意的，
   不做兼容的"默认假设"。
2. **A 股日线的下界是"下一交易日 15:00"**。若某天收盘后又被交易所回填（盘后
   数据修正），要到下一交易日才会被重新评估；这对策略信号是可接受的，对要求
   即时回填的展示层不适用（展示层不走 `closed_only`）。
3. **崩溃重试没有次数上限**。一个每次都崩的策略会每 tick 重试并反复写
   `last_error`（warn/error 有 5 分钟去重，不会刷屏，但也不会自动停用实例）。
   是否要在连续 N 次失败后自动 stop，需要产品决定。
4. **止损的报价缓存粒度是"一轮扫描"**。不同账户持有同一标的时共用一次报价；
   若两次读取之间价格变动，同一轮内多个实例看到的是同一个价格。这在一步之内
   是想要的（保证一致性），但意味着同一轮里不会出现"一个触发、一个没触发"
   的撕裂。
