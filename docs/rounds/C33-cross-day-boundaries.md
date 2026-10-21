# 迭代记录 C33：跨日边界——T+1 释放与日历覆盖

## 动机

文档里记着一条未完成项："跨月/跨年边界验证"。A 股系统的时间边界比加密复杂得多：
T+1 依赖于"日"的切换，交易日历依赖于节假日，熔断依赖于市场自己的日界。
这一轮逐个查过去，抓到两个真实缺陷。

## 缺陷一：真实数据部署下，T+1 永远不会释放

`TradingEngine._session_duties()` 负责每天开盘时把 `available_quantity` 拉到 `quantity`。
它的开头是：

```python
cn_engine = getattr(self.market_engine, "cn", None)
clock = getattr(cn_engine, "clock", None)
if clock is None:
    return          # ← 这里
```

`clock` 来自模拟 A 股引擎。而 C32 刚支持了**纯真实数据部署**
（`seed_market=False` + `cn_data_source=tencent`），这种模式下 `cn_market_engine` 不存在，
`clock` 是 `None`，于是整个结算职责直接返回。

后果：今天买入的股票，`available_quantity` 永远是 0，**第二天也卖不掉**。
而没有任何其他地方会清这个字段——它是一个单向的、不可恢复的死锁。
用户只会看到"可卖 0 股（T+1）"，而且等多久都不会变。

**修法**：时钟缺失时回落到系统时间，结算职责照常执行。

```python
now = clock.now_ms() if clock is not None else int(time.time() * 1000)
```

### 顺带修正作用域

原来的筛选条件是"所有 quantity > available_quantity 的持仓"，没有限定市场。
加密持仓本应始终 `available == quantity`；如果出现不一致，那是一个真实的缺陷，
在 A 股结算里顺手抹平它只会掩盖问题。现在只处理 A 股账户的持仓，
`settle_t1()` 也做了同样的限定。

## 缺陷二：2027 年起，元旦被当成交易日

`calendar.py` 里是硬编码的 `_HOLIDAYS` 集合，覆盖 2024–2026。
`is_trading_day()` 的实现是：

```python
return day.weekday() < 5 and day.isoformat() not in _HOLIDAYS
```

2027 年的日期不在集合里，于是一个普通的周五 `2027-01-01` 被判定为**交易日**。
模拟时钟会在元旦开出一个不存在的交易日，数据同步会把假期当成缺口，
交易日历接口会给出错误的"下一交易日"。

**修法分两层**：

1. **固定日期的法定假日按 `(月, 日)` 匹配**，任何年份都生效：
   元旦 1/1、劳动节 5/1、国庆 10/1–10/3。
2. **农历假日不做猜测**（春节、清明、端午、中秋的日期无法推导，猜错比不猜更糟），
   改为显式报告覆盖范围：

```python
calendar_coverage(date(2027, 1, 1))
# {"year": 2027, "covered": False, "covered_years": [2024, 2025, 2026],
#  "warning": "2027 年的节假日表尚未加载……"}
```

`GET /api/cn/calendar` 返回 `coverage` 与 `upcoming_coverage`，
A 股页面在未覆盖时把这条告警显示在交易日卡片里——缺数据要看得见，
而不是让系统安静地跑在错误的日历上。

## 验证

| 检查项 | 结果 |
|---|---|
| 真实数据模式（无 CN 引擎）下 `_session_duties` 释放 T+1 | ✅ 新增集成测试 |
| 加密持仓不被结算逻辑改动 | ✅ 新增集成测试 |
| 2027-01-01 / 05-01 / 10-01~03 判定为休市 | ✅ |
| 2027 普通工作日仍可交易 | ✅ |
| 2026-12-31 的下一交易日为 2027-01-04（跳过元旦与周末） | ✅ |
| 覆盖范围内 `coverage.covered = true` 且无告警 | ✅ |
| 覆盖范围外返回明确告警文案 | ✅ |
| A 股页面仅在未覆盖时显示告警 | ✅ 浏览器确认 |
| 前端类型检查 / 构建 | ✅ |
| 后端全量回归 | ✅ 331 passed（新增 8 项） |

## 已知限制

- 2027 年及以后的农历假日仍需在每年 12 月更新 `calendar.py`，
  或用 `LQ_CN_EXTRA_HOLIDAYS` 一次性注入。接口已经把这件事变成可见的运维项。
- T+1 结算依赖交易进程持续运行；若进程跨日夜停摆，重启后在开盘时会补做结算。
