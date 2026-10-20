# A股交易模式

## 支持的规则

| 规则 | 实现 | 位置 |
|---|---|---|
| 交易时段 | 09:30–11:30 / 13:00–15:00，节假日闭市 | `cn/calendar.py` |
| T+1 | 当日买入 `available_quantity=0`，每个交易日开盘自动解锁 | `trading/service.py`、`trading/engine.py` |
| 涨跌停 | 主板 ±10%、创业板/科创板 ±20%、北交所 ±30%、ST ±5% | `cn/rules.py` |
| 涨停/跌停锁死 | 涨停不可买、跌停不可卖（含挂单撮合跳过） | `trading/service.py` |
| 手数 | 买入 100 股整数倍；零股仅可一次性全部卖出 | `cn/rules.py` |
| 费用 | 佣金（万 2.5，最低 5 元）+ 卖出印花税 0.05% + 过户费 0.001% | `cn/fees.py` |
| 停牌 | 停牌标的拒单，撮合跳过 | `market/models.py`、`trading/service.py` |
| 价格笼子 | 限价单必须落在当日涨跌停区间内 | `cn/rules.py` |
| 界面 | 集合竞价、盘后固定价格等未实现（见"限制"） | — |

## 数据

系统启动时自动播种 8 个 A 股标的（沪/深，覆盖主板、创业板、科创板），
每个标的 600 个交易日的**日线**与**15 分钟线**，K 线只落在真实交易时段内。

- 日线是原生序列（不是 15 分钟聚合），回测 `1d` 直接使用；
- 其他周期（`30m/1h/4h`）在读取时聚合；
- 昨收、涨跌停、板块随标的存储，每日开盘前刷新。

```bash
# 查看规则
curl http://127.0.0.1:8000/api/cn/rules | jq
# 查看交易日历
curl http://127.0.0.1:8000/api/cn/calendar?days=10 | jq
# 查看行情与会话状态
curl http://127.0.0.1:8000/api/cn/symbols | jq
curl http://127.0.0.1:8000/api/cn/session | jq
```

## 账户

创建账户时指定 `market: "cn_stock"`：

```bash
curl -X POST http://127.0.0.1:8000/api/accounts \
  -H "Content-Type: application/json" \
  -d '{"name":"A股模拟","kind":"paper","market":"cn_stock","initial_balance":300000}'
```

- 基础货币自动为 `CNY`，杠杆固定 1 倍；
- 下单标的必须与账户市场一致，否则 422；
- 加密账户与 A 股账户可并存，前端交易页按账户市场自动切换面板。

## 回测

A 股规则由**标的**决定，客户端无法通过 config 关闭：

```bash
curl -X POST http://127.0.0.1:8000/api/backtests -H "Content-Type: application/json" -d '{
  "strategy":"sma_cross","symbol":"600519.SH","timeframe":"1d","bars":300,
  "params":{"fast":5,"slow":20}
}'
```

即使请求里传 `{"t_plus_one": false, "lot_size": 1}`，服务端也会强制覆盖为
A 股规则并在结果 `config` 中回显，避免"回测跑赢实盘"的口径偏差。

回测新增输出：
- `metrics.periods_per_year = 242`（A 股年化基准，加密为 365）；
- `metrics.blocked_orders = {limit_up, limit_down, t_plus_one}` —— 有多少次想成交但被规则拦下。

## 自动交易

策略实例支持 A 股标的，评估仍只使用**已收盘** K 线；下单走同一套 A 股校验，
因此自动运行不会绕过手数、涨跌停或 T+1。

## 交易时段门控

| 场景 | 默认行为 |
|---|---|
| 模拟账户 | 不强制时段（便于演示与测试，`LQ_CN_SESSION_ENFORCED=true` 可强制） |
| 实盘账户 | 强制真实北京时间交易时段（`LQ_CN_LIVE_SESSION_GATE=false` 仅用于演练） |

## 限制（未实现，需明确知晓）

1. **集合竞价**：09:15–09:25 与 14:57–15:00 的撮合规则未建模。
2. **融资融券、做空**：仅支持现金买入多头。
3. **新股/科创板盘后定价、北交所连续竞价细节**：未实现。
4. **除权除息**：日线不做复权处理，长期回测需导入前复权数据。
5. **逐笔/Level-2 行情**：使用合成 15 分钟线，非真实 tick。
6. **T+1 结算时点**：以"下一个交易日开盘"为解锁时点，未模拟券商夜间清算作业。

这几点都会影响回测与真实交易的偏差，实盘前应先用券商数据校准。
