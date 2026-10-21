# 迭代记录 C34：压力测试修复与长时间运行

## 动机

交付文档里列着三个未完成项：前端自动化测试、24h+ 连续运行、压力测试复验。
这一轮先做后两个——它们共用同一套脚本，而且这些脚本本身就坏了。

## 压测脚本的四个缺陷

跑 `system_load.py` 直接崩了。逐个查下去，发现脚本从写好之后就没真正跑通过：

### 1. `/api/trading/positions` 缺少 `account_id`

```python
params={"account_id": None}   # httpx 直接丢弃 None，参数根本发不出去
```

四分之一的"读请求"实际是 422，被统计成错误率。

### 2. 中间件限流把压测当成攻击

默认 `LQ_RATE_LIMIT_PER_MINUTE=600`。压测 500 个请求在几十秒内打完，
**全部 429**，测出来的延迟其实是限流器的响应速度。

### 3. 风控拒绝被算成系统故障

120 单里 109 单是 `order rate limit reached`，报告成 91% 错误率。
再跑一次，现金不足和超卖又被算成错误。这两类都是风控和校验**正常工作**。

### 4. `strategy_load.py` 无法从仓库外运行

缺 `sys.path` 引导，`python tests/stress/strategy_load.py` 报 `No module named 'app'`。
它的 `top` 字段输出的是收益最高的三次回测——三次都是同一个策略同一标的，
对吞吐压测毫无信息量。

### 修法

结果分成四类，只有最后一类算故障：

| 类别 | 含义 |
|---|---|
| `ok` | 2xx |
| `throttled` | 429 或 409 rate limit，风控在工作 |
| `rejected` | 现金不足/超卖/涨跌停/手数不符，校验在工作 |
| `errors` | 其余，需要排查 |

`strategy_load.py` 的 `top` 改成 `slowest`（含耗时），并补上路径引导。
新增 `tests/stress/README.md` 说明每个脚本的前置条件和参考值。

## 引擎长时间运行的缺陷：日志无限增长

`soak_cn_engine.py` 跑 90 个模拟交易日没有问题（8 个标的一共 10104 根 15 分钟线，
0 根非交易日、0 重复、0 畸形）。但检查日志表时发现：

**`RuntimeLog` 没有任何保留策略，而且 `insufficient_data` 每次 tick 都写一条。**

调度器 6 秒一轮。一个"数据不足"的实例一天写约 14400 行，
一个挂了的价格源、一个低于一手成本的名义上限，都是长期存在的状态——
它们会一直写下去，直到数据库变得不可用。

修法两层：

1. **写入去重**：相同的 `(event, message)` 在 5 分钟内只记一次。
   只对 `warn`/`error` 生效——`started`/`updated` 是用户操作，连续两次也是真实意图，
   不能吞掉。
2. **读取裁剪**：每个实例最多保留 2000 行（API 最多返回 200 行，超出纯属磁盘占用），
   调度器每小时修剪一次。

顺带修了一个时区问题：SQLite 返回的 `created_at` 是 naive datetime，
直接和 `utcnow()` 相减会抛 `TypeError`。

## 时间压缩方案

真跑 24 小时没有意义（也无法在迭代中完成）。改为**推进模拟时钟**：

- A 股引擎的时钟只在开市时段前进，收盘后跳到下一个交易日 09:30
- 直接修改 `clock._cursor`，跳过真实等待
- 90 个交易日（含跨越 2027 年元旦）约 5 秒完成

关键细节：跳到下一个时段必须走 `next_open_ms`。落在 11:30 整点时不在上午盘区间内，
用"下一个交易日"的朴素跳法会**跳过整个下午盘**——K 线少一半，而指标看起来全绿。
（这个 bug 在写脚本时真实出现过，靠打印每步时间才发现。）

## 实测结果

### 并发压测（200 单 / 12 并发 + 400 读）

```
place_orders: requests=200 ok=200 throttled=0 rejected=0 errors=0
              p50=40.5ms p95=697ms max=2771ms
read_queries: requests=400 ok=400 throttled=0 rejected=0 errors=0
              p50=61.6ms p95=107ms max=259ms
equity_reconciles: True (delta -0.0)
ledger_reconciles: True
fills_cover_orders: True (368 fills / 368 filled orders)
```

### A 股并发（80 单 / 8 并发）

```
errors=0 p50=140.9ms p95=217.2ms max=241.6ms
resting_orders=80  t1_invariant_holds=True
```

### 连续运行（90 个交易日，跨越 2027 元旦）

```
final_day=2027-01-19  symbols=8
intraday_bars=10104   daily_bars=632
bars_on_non_trading_days=0   malformed_daily_bars=0
duplicate_daily_bars=0
missing_prev_close=[]  missing_limit_band=[]
ticks=1008  elapsed=4.49s  ticks_per_s=224.3
```

### 策略吞吐（9 策略 × 2 标的 × 5 轮，2000 根）

```
runs=90 p50=10.1ms p95=77.7ms max=88.3ms total=2.75s
slowest: bollinger_reversion (ETHUSDT) 88.3ms
```

## 验证

| 检查项 | 结果 |
|---|---|
| 重复 warn 只写一条 | ✅ 新增测试 |
| 超过去重窗口后重新记录 | ✅ 新增测试 |
| `started`/`updated` 不去重 | ✅ 新增测试 |
| 修剪保留最新 N 行 | ✅ 新增测试 |
| 90 日 soak 全部不变量 | ✅ |
| 并发下单账目一致（权益、账本、成交） | ✅ |
| A 股 T+1 并发不变量 | ✅ |
| 后端全量回归 | ✅ 335 passed（新增 4 项） |

## 已知限制

- SQLite 单写者：并发写 p95 由排队决定。压低它需要换库或串行化写入，
  调 PRAGMA 无效。
- soak 覆盖引擎与数据不变量，不覆盖真实进程的 24 小时内存/句柄增长。
  要验证那部分需要常驻部署后采多数天 `/api/system/metrics`。
- 压测未覆盖真实同花顺网关（受券商程序化交易权限限制）。
