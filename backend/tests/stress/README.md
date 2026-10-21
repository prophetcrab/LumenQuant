# 压力测试脚本

四个脚本回答不同问题。前三个面向运行中的服务，第四个面向引擎本身。

| 脚本 | 需要服务 | 问题 |
|---|---|---|
| `system_load.py` | ✅ | 并发下单 + 混合查询下的延迟与账目一致性 |
| `cn_load.py` | ✅ | A 股挂单、T+1 冻结、涨停约束在并发下是否守住 |
| `load_api.py` | ✅ | 纯读接口的吞吐 |
| `strategy_load.py` | ❌（进程内） | 9 个策略 × 2 标的 × N 轮的回测耗时分布 |
| `soak_cn_engine.py` | ❌（进程内） | 跨月/跨年连续运行后的数据不变量 |
| `verify_calendar.py` | ❌（需外网） | 手工维护的节假日表是否与真实交易所数据一致 |
| `real_data_e2e.py` | ❌（需外网） | 真实数据下从同步到归档的 10 项端到端验收 |

## 运行 system_load / cn_load / load_api

中间件限流（`LQ_RATE_LIMIT_PER_MINUTE`，默认 600/分钟/IP）和单账户下单限流
（`LQ_RISK_MAX_ORDERS_PER_MINUTE`，默认 120/分钟）都会拦住压测。压测时把两者放大：

```bash
LQ_RATE_LIMIT_PER_MINUTE=100000 LQ_RISK_MAX_ORDERS_PER_MINUTE=100000 \
  uvicorn app.main:app --host 127.0.0.1 --port 8000
```

```bash
python tests/stress/system_load.py --account <id> --orders 200 --concurrency 12 --queries 400
python tests/stress/cn_load.py --orders 80 --concurrency 8
python tests/stress/load_api.py --base http://127.0.0.1:8000 --requests 500 --concurrency 25
```

`system_load.py` 把结果分成四类，只有 `errors` 代表系统故障：

- `ok` — 2xx
- `throttled` — 命中限流（429 或 409 rate limit），说明风控在工作
- `rejected` — 业务规则拒绝（现金不足、超卖、涨跌停、手数不对），说明校验在工作
- `errors` — 其余，需要排查

`cn_load.py` 额外断言 `t1_invariant_holds`：并发卖出后，`available_quantity`
不得为负，也不得超过 `quantity`。

## 运行 soak / strategy_load

这两个是进程内的，直接跑，不需要先起服务：

```bash
python tests/stress/soak_cn_engine.py --days 90
python tests/stress/strategy_load.py --bars 2000 --repeat 5
```

`soak_cn_engine.py` 压缩时间：直接推进模拟交易时钟，90 个交易日约 5 秒跑完，
然后检查不变量（非交易日无 K 线、无重复日线、OHLC 自洽、涨跌停字段齐全）。
退出码非 0 表示有不变量被破坏。

`verify_calendar.py` 拉取真实日线，逐日比对 `calendar.py` 的判定，
双向报告「日历说开市但没有 K 线」和「日历说休市但有 K 线」。
节假日表是人工维护的，每年 12 月更新后应当跑一次：

```bash
python tests/stress/verify_calendar.py --bars 400
```

退出码非 0 表示日历与真实数据不符，需要更新 `_HOLIDAYS` 或用
`LQ_CN_EXTRA_HOLIDAYS` 补充。

`real_data_e2e.py` 在临时库上跑完整链路，覆盖：真实同步、日线质量、
建账户、回测、真实价下单与 T+1 拦截、自动实例执行、风控保护可见、
权益恒等式、助手市场隔离、归档停止实例。退出码非 0 表示某一步失败。

```bash
python tests/stress/real_data_e2e.py --code 600036.SH --daily 300
```

## 已知的性能边界

SQLite 是单写者：并发写请求会排队，p95 由等待写锁决定而不是由业务逻辑决定。
`system_load.py` 的观测参考值（本机、200 单 / 12 并发）：

- 下单：p50 ≈ 40 ms，p95 ≈ 700 ms（少数请求排到 2 s 以上）
- 读接口：p50 ≈ 60 ms，p95 ≈ 110 ms

要压低写延迟，换 PostgreSQL 或把下单串行化到一个写队列，而不是调 SQLite 参数。
