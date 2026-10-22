# 迭代记录 C74：双市场实时行情接入

## 动机

到 C73 为止，两个市场的行情都是本地合成数据。A 股链路是"点一次同步 +
之后价格冻结"，`cn_quote_poll_seconds` 这个配置项从引入起就没有任何代码
读取过；加密则完全没有真实数据通道。`docs/delivery/cn-data-source.md` 的
「已知限制」里自己写着：

> **无实时推送**：本地是"同步 + 轮询"，不是 WebSocket 推送。

但实际连轮询也没有。用户看到的"实时价"是最近一次同步的收盘价。

本轮把两个市场都接上线上实时数据：A 股走腾讯公开行情，加密走 Binance
公共行情，均为免 Key 的只读接口。

## 设计取舍

### 为什么不继续用合成引擎

真实数据模式原本是 `seed_market=true` 时的 `CnMarketEngine` 继续 tick，
然后同步时用真实数据整体覆盖。这有两个问题：

1. **两个写入方**：合成引擎每 5 秒写一根 15m bar，价格水平与真实行情
   完全不同（同代码可能差 20%+）。同步前用户看到的是合成价，同步后跳到
   真实价，涨跌停区间也跟着失真——C32 修过一次同类问题。
2. **时钟不一致**：合成引擎的 `CnSessionClock` 是 60x 加速的，T+1 结算
   和交易时段判断都会跟着它走。

所以本轮在真实数据模式下**不再启动合成引擎**，它的职责缩减为登记标的
目录（`ensure_symbols`）。价格序列的唯一写入方变成 `CnQuoteEngine`。

### 为什么轮询而不是 WebSocket

腾讯的公开行情接口没有推送通道。引入 WebSocket 需要自己搭中继服务，
超出"低耦合、可离线"的项目约束。15 秒轮询对 A 股（最小周期 15 分钟）
已经足够，且实现、测试、运维成本都低一个数量级。

### 本轮新增

| 文件 | 作用 |
|---|---|
| `modules/cn/quotes.py` | A 股实时报价引擎：批量拉取、累积 15m bar、日线直写、健康状态 |
| `modules/cn/sources/tencent.py` | 新增 `quotes()`，一次 HTTP 请求取多只 |
| `modules/cn/data_router.py` | `/cn/data/quotes`、`/cn/data/poll`；`/source` 报告轮询健康 |
| `modules/market/crypto_quotes.py` | 加密实时引擎：行情导入、ticker 轮询、klines 校正 |
| `modules/market/sources/binance_public.py` | Binance 公共 ticker / kline 客户端 |
| `modules/market/data_router.py` | `/market/data/source|quotes|poll|sync` |
| `modules/market/service.py` | 两个市场的快照与 `last_price` 都优先用交易所报价；半根 bar 合并 |
| `frontend/.../CnMarketPage.tsx` | 实时价、行情延迟列、立即刷新按钮、休市降频提示 |
| `frontend/.../MarketsPage.tsx` | 数据源标识、报价滞留标记 |
| `frontend/.../Layout.tsx` | 页脚按真实数据源显示「实时」而非固定「模拟时钟」 |

## 关键实现细节

### 批量报价

`qt.gtimg.cn/q=sh600519,sz000001,...` 一次可返回多只，返回体是按 `;` 分隔的
多行 `v_<ticker>="..."`。引擎按 60 只一组切分：单组失败只丢这一组，
其余组的报价仍然生效；全部失败才抛 `SourceUnavailable`。

逐只请求在 8 个标的时是 8 倍延迟（实测 8 只批量约 300–400ms），
在更大自选股列表上还会触发限流。Binance 同理用 `symbols=[...]` 批量。

### A 股 15m bar 的重建

行情接口只给**当日** OHLC 和**累计**成交量，没有 15m 分时。引擎的做法是：

- 按报价时间戳对齐到 15m 桶；
- 同桶内累加 high/low，收盘价取最后一次报价；
- 成交量取累计量的**差分**（本桶增量），而不是累计值；
- 跨桶时把上一桶写入数据库。

没有上一根 bar 时（进程在桶中途启动），开仓价取**首个观测价**而不是当日开盘价——
否则会凭空造出一根从 09:30 到现在的大阴/大阳线。

### A 股日线直接由报价写入

日线不由 15m 求和，而由报价写入（`_sync_today_daily`）：

- 报价自带当日 O/H/L/现价与**累计**成交量，这本身就是日线；
- 15m 求和会丢掉进程启动前的全部成交，重启后还会丢两次；
- 只在盘中写入——收盘后重写 high/low 可能收窄交易所已经公布的区间。

### 加密的成交量必须来自 klines

24h ticker 的 `volume` 是**滚动成交额**，差分后会把一整天的量算进一根 15 分钟线。
因此轮询只负责价格路径，成交量留空；每根桶结束时额外请求 `klines(limit=3)`，
用交易所的权威 OHLCV 覆盖累积值。拿不到时保留累积版本（量为 0）而非丢弃整根。

### 半根 bar 的合并

Binance klines 包含**当前未收盘**的那根，所以导入后数据库里会有一条
"部分 bar"，其时间戳正是实时桶的时间。直接替换会丢掉真实的 open/high/low/量，
直接跳过又会让最后一根冻结 15 分钟。因此合并：时间取已有值，open 保留，
high/low 取并集，close 取实时价，量相加。

### 新鲜度

| 市场 | 阈值 |
|---|---|
| 加密 | `max(15s, 5 × 间隔)` |
| A 股盘中 | `max(30s, 4 × 间隔)` |
| A 股休市 | `8 × 间隔` |

超时的后果是明确的：`live_quote()` 返回 `None`，快照回落到数据库最后一根 bar，
接口返回 `fresh=false`，前端显示「滞留 / 报价滞留」。把过期价格当成实时价
展示，比显示一个明显的空缺更危险。

### 轮询节奏与失败退避

| 场景 | 间隔 |
|---|---|
| 加密（7×24） | `crypto_quote_poll_seconds`（默认 5s） |
| A 股盘中 | `cn_quote_poll_seconds`（默认 15s） |
| A 股休市 | `max(60s, 4 × 间隔)` |
| 连续失败 | 指数退避：加密上限 60s，A 股上限 300s；成功后立即复位 |

一个整夜无人值守的实例不应该每 15 秒打一次上游接口，也不应该在 DNS 故障时
每 5 秒打一次并把日志刷满堆栈。失败只记一行原因，写入 `last_error` 供接口与
页面展示。

### 启动期不再重复导入

加密历史上限 1000 根 15m + 1000 根日线/标的。每次启动都全量重导既慢又会与
轮询抢带宽（实测启动瞬间大量 DNS 失败）。现在先检查本地最新 15m 时间戳，
30 分钟内最新就跳过；`/market/data/sync` 传 `force=True` 强制重导。

## 顺带修掉的两个缺陷

这两个都是真实运行才暴露的，单元测试没有覆盖到相应的执行路径。

### 1. `create_app()` 在建表前查表

真实数据 + `seed_market=false` 的部署（本轮新增的路径）会在
`create_app()` 里调用 `ensure_symbols()`，而建表在 lifespan 里。全新数据库
启动直接崩：

```
sqlite3.OperationalError: no such table: market_symbols
```

改为把标的登记延迟到 lifespan 里 `db.create_schema()` 之后执行。

### 2. 函数内 import 遮蔽模块级名字

`StrategyRuntimeEngine.tick()` 在方法中段写了：

```python
from app.modules.alerts.service import AlertService
```

Python 的规则是：函数体内任意位置的赋值/import 都会让这个名字在整个函数内
成为局部变量。于是方法**开头**的 `TradingService(..., alerts=AlertService(session))`
抛 `UnboundLocalError`，整个 tick 失败。

更糟的是调度器 `except Exception: logger.exception(...)` 会吞掉它，进程继续跑、
接口全部正常，只是**没有任何策略被评估过**。同样的问题在
`market/service.py` 的 `MarketService` 上也存在（C70 引入）。

修复后新增 `tests/unit/test_import_shadowing.py`：用 AST 扫描全仓，禁止
函数内 import 与模块级名字重名。这条规则用覆盖率抓不到——缺陷在未执行的分支里。

## 验证

### 单元（19 个）

`tests/unit/test_quote_engine.py`（A 股，确定性时钟 + 假数据源）：

| 用例 | 断言 |
|---|---|
| 首次轮询 | 建出 10:00 的 bar，open 取首观测价 |
| 同桶累积 | high/low 单调、成交量取差分 |
| 跨桶 | 上一根落库，下一根 open 接上一根 close |
| 日线直写 | 日线 O/H/L/C/量与报价的累计量一致 |
| 日线不累加 | 三次轮询后量等于最后一次累计量，不是求和 |
| 休市 | 无 live bar，但报价仍在 |
| 过期 | 90s 后 `live_quote` 为 None，`fresh=false` |
| 昨收变更 | 只有变化才写库 |
| 涨停锁 | 触及 +10% 标记为 locked，回落解除 |
| 批量解析 | 一次请求解析多只，GBK 正确 |
| 缺符号 | 上游少返回一只不报错 |

`tests/unit/test_crypto_realtime.py`（加密）：

| 用例 | 断言 |
|---|---|
| K 线列序 | high/low 在 close 之前，未按 close-first 解析 |
| 批量 ticker | 一次 `symbols=[...]` 请求 |
| 4xx 不重试 | 451 只请求一次 |
| 累积 | 价格路径累积，成交量不臆造 |
| 收盘 | 上一桶落库 |
| **权威量校正** | 收盘时用 klines 的真实 OHLCV 覆盖累积值 |
| 过期 | 10 分钟后报价判为不新鲜 |
| 快照 | 用 24h ticker 的窗口计算涨跌与极值 |

`tests/unit/test_import_shadowing.py`：AST 扫描全仓，禁止函数内 import 与模块名重名。

### 集成（5 个）

`tests/integration/test_cn_realtime.py`：真实数据模式下合成引擎被替换；
`/cn/data/quotes` 报告新鲜度；快照价格/涨跌/涨跌停取自报价；
昨收变更后涨跌停区间跟着变；合成模式下 `/quotes` 返回 502 并提示如何开启。

### 真实网络

全新数据库 + 腾讯 + Binance 真实接口：

```
crypto: mode=realtime-poll 6 symbols 5s latency 702ms
cn:     mode=realtime-poll 8 quotes 15s latency 405ms session_open=false
CN 日线量 33514 == 报价累计量 33514
BTC 15m 最后一根: open 84338.74 high 84344.66 low 84300.0 close 84328.93 vol 16.08
```

启动 45 秒内 `poll failed` 与 `Traceback` 均为 0 次（退避 + 跳过已最新历史）。

A 股实时成交验证：模拟账户以市价买入 600519.SH 100 股，成交价 1309.69 与
同刻报价完全一致，成交额 130,969 + 费用 34.05，`available_quantity=0`（T+1 冻结生效）。

### 浏览器验收（2560×1440）

- 行情页：副标题显示「Binance 实时」，24h 统计与侧栏报价一致
- A 股页：数据源卡片显示「腾讯实时轮询 · 8 只 · 15s 轮询 · 418ms」，行情延迟列 `<1s`
- 页脚：由固定的「模拟时钟」改为「实时 · 2026-10-21 休市」
- 「立即刷新报价」按钮：toast 返回「已刷新 8 个标的（373ms）」

### 回归

- 后端 **442 passed**（新增 25 个）
- 前端 66 passed，`tsc --noEmit` 干净，`vite build` 通过

## 已知限制

1. **仍是轮询，不是推送**：延迟上限是一个轮询间隔。两个上游都没有推送通道。
2. **A 股 15m bar 是重建的**：腾讯没有分时接口，高/低点只反映被采样的价格路径，
   真实极值可能被漏掉；加密可以在收盘时用 klines 校正，A 股不能。
3. **当日开盘价不参与 A 股 bar 重建**：进程在盘中启动时，首根 bar 的 open 是首观测价。
4. **A 股休市期间不累积**：收盘后的报价不再写入 bar；日线以收盘前最后一次盘中写入为准。
5. **跨日价差**：A 股日线在盘前不做预测性写入，开盘首个报价到达后才建立当日 bar。
6. **单进程内存状态**：多实例部署时各自轮询、各自持有 live bar；数据库层
   靠唯一索引去重，不会重复写入，但健康状态不共享。
7. **加密成交量口径**：轮询期间累积的 bar 量为 0，只有收盘校正后才写入真实值；
   若当时网络失败，该根会以量 0 保存而不是被丢弃。
