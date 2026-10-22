# 实时行情（加密 + A 股）

## 三个数据模式

| 市场 | 模式 | 配置 | 是否需要网络 |
|---|---|---|---|
| 加密 | 合成（默认） | `LQ_CRYPTO_DATA_SOURCE=synthetic` | 否 |
| 加密 | 实时 | `LQ_CRYPTO_DATA_SOURCE=binance` | 是 |
| A 股 | 合成（默认） | `LQ_CN_DATA_SOURCE=synthetic` | 否 |
| A 股 | 实时 | `LQ_CN_DATA_SOURCE=tencent` | 是 |

两个市场独立配置：可以只把 A 股切到真实，加密继续跑合成。

真实模式的**共同规则**：合成引擎不再运行。若两个写入方同时生产同一批 K 线，
价格水平会在两个体系间来回跳（同代码差 20%+），涨幅与涨跌停随之失真。
因此真实模式下合成引擎只保留"登记标的目录"的职责。

## 加密（Binance 公共行情）

只读接口，不需要 API Key，也不接触账户：

- `https://data-api.binance.vision/api/v3/ticker/24hr`
- `https://data-api.binance.vision/api/v3/klines`

`api.binance.com` 在部分网络不可达，而专用行情域名可达，因此基址可配置
（`LQ_CRYPTO_DATA_BASE_URL`）。真实下单仍走 `modules/brokers/binance.py` 的
签名交易接口，与此无关。

```env
LQ_CRYPTO_DATA_SOURCE=binance
LQ_CRYPTO_QUOTE_POLL_SECONDS=5
```

```bash
curl http://127.0.0.1:8000/api/market/data/source | jq
curl http://127.0.0.1:8000/api/market/data/quotes | jq
curl -X POST http://127.0.0.1:8000/api/market/data/poll | jq
curl -X POST http://127.0.0.1:8000/api/market/data/sync | jq   # 强制重导历史
```

## A 股（腾讯公开行情）

```env
LQ_CN_DATA_SOURCE=tencent
LQ_CN_SYNC_ON_START=true
LQ_CN_QUOTE_POLL_SECONDS=15
```

```bash
curl http://127.0.0.1:8000/api/cn/data/source | jq
curl http://127.0.0.1:8000/api/cn/data/quotes | jq
curl -X POST http://127.0.0.1:8000/api/cn/data/poll | jq
curl -X POST http://127.0.0.1:8000/api/cn/data/sync | jq
```

## 轮询节奏与新鲜度

| 场景 | 间隔 |
|---|---|
| 加密（7×24） | `LQ_CRYPTO_QUOTE_POLL_SECONDS` |
| A 股盘中 | `LQ_CN_QUOTE_POLL_SECONDS` |
| A 股休市 | `max(60s, 4 × 间隔)` |
| 上游连续失败 | 指数退避：加密上限 60s，A 股上限 300s，恢复后立即回到正常节奏 |

失败时**不用完整堆栈刷屏**：只记一行原因，并写入 `last_error`，通过
`/data/source` 与页面错误条展示。

报价超过阈值即判为不新鲜：

- 加密：`max(15s, 5 × 间隔)`
- A 股盘中：`max(30s, 4 × 间隔)`；休市：`8 × 间隔`

不新鲜的后果是**明确的**：快照回落到数据库最后一根 bar，接口返回
`fresh=false`，前端显示「滞留 / 报价滞留」。把死价格当实时价展示，比显示空缺更危险。

## 15 分钟线怎么来的

上游都不提供"每根 15m 的完整 OHLCV"，两个市场的重建方式不同：

### 加密

轮询 ticker 得到价格路径，累积当前桶的 high/low/close。**成交量不来自轮询**：
24h ticker 的 volume 是滚动成交额，差分后会把一整天的量算进一根 15 分钟线。
每根桶结束时额外请求一次 klines（`limit=3`）取该根的权威 OHLCV，用它覆盖累积值。
拿不到时保留累积版本（量为 0）而不是丢弃整根——宁可量缺失，不可 K 线缺失。

### A 股

报价本身带**当日** O/H/L/现价与**累计**成交量，这就是当日日线。因此：

- 日线直接由报价写入（`_sync_today_daily`），只在盘中写；
- 15m bar 由连续采样重建，成交量取累计量差分；
- 日线成交量**赋值**为交易所累计量，绝不由 15m 求和——进程中途启动时，
  求和会丢掉开盘以来的所有成交，重启还会丢两次。

## 已知限制

1. **是轮询，不是推送**：延迟上限是一个轮询间隔。两个上游都没有 WebSocket。
2. **15m 线是重建的**：反映被采样的价格路径，真实极值可能被漏掉。加密收盘时有
   klines 校正，A 股没有分时接口，无法校正。
3. **A 股盘中启动时首根 15m 的 open** 取首个观测价，而非 9:30 开盘价。
4. **不含集合竞价与逐笔**。
5. **单进程状态**：多实例部署各自轮询；数据库靠唯一索引去重不会重复写入，
   但健康状态不共享。
6. **上游均非官方 SLA 接口**，可能变更或限流。腾讯实现带 3 次指数退避重试（4xx 不重试）；
   Binance 对 418/451 直接失败不重试。
7. **A 股日线在收盘后不再更新**：收盘后报价是最后成交价，日线以收盘前最后一次盘中
   写入为准；跨境/除权调整次日由报价的昨收刷新。

## 数据源健康告警

实时链路最大的静默风险不是报错，而是**不报错**：数据源死掉后所有消费方都回落到
最后一根已存 K 线，页面继续显示价格，看起来一切正常。

两个报价引擎各自带健康守卫：

| 条件 | 行为 |
|---|---|
| 连续失败 ≥ 3 次 | 抛 `data_feed` 严重告警 |
| A 股盘中报价超过 2× 新鲜度阈值未更新 | 同上 |
| 仍处于故障 | 不再新增告警（状态翻转才报一次） |
| 恢复 | 自动确认告警 |

告警文案会写清「交易仍在用最后一根已存 K 线估值」，并保留上游错误原文。
从未成功轮询过时显示「启动后从未成功获取报价」。

平台级告警没有 `account_id`，`/api/alerts`、`/api/alerts/summary`、
`/api/alerts/categories` 都会把空 `account_id` 的行并入任何账户的视图——
否则最严重的告警会被账户过滤器完全隐藏。
