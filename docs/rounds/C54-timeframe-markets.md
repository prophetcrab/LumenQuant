# 迭代记录 C54：A 股不该出现 4h，以及一个会拖垮调度的问题

## 动机

C53 修完缓存后继续看真实数据的可用性。检查"每个市场支持哪些周期"这件事时，
发现前后端各有一套时间周期定义，而它们不一致。

## 缺陷一：A 股不支持 4h，但界面上有

后端明确：

```python
CN_TIMEFRAMES = ("15m", "30m", "1h", "1d")
```

前端四个页面各自写死：

```ts
const TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"];   // ×4 份
```

A 股账户在自动运行页选 4h 建实例 → 后端返回 201（当时没校验）→
实例永远读不到 K 线。回测页选 4h → 422。

为什么 A 股没有 4h 是合理的：A 股每天只有 4 小时交易（9:30–11:30 + 13:00–15:00），
"4 小时 K 线"实际上等于"整日一根"，与 1d 重复且跨越午休，没有独立意义。

## 缺陷二：一个坏实例让整个 tick 返回 422

修第一步时发现更严重的问题。创建一个 4h 实例后调用 `/api/runtime/tick`：

```
POST /api/runtime/tick -> 422
```

原因是 `evaluate_instance` 里 `market.candles(...)` 抛 `ValidationError`，
这个异常在 tick 循环里**没有捕获**，直接冒泡成 HTTP 错误。

后果：**该账户上所有其他正常实例也一并停止调度**，而且每 6 秒的自动调度
同样会失败。一个配置错误能瘫痪整个账户的自动化。

对比后台调度器 `StrategyRuntimeEngine.tick()`——它早就做了逐实例隔离：

```python
for row in service.running_instances():
    try:
        results[row.id] = service.evaluate_instance(row)
    except Exception as exc:
        row.last_error = str(exc)[:400]
        results[row.id] = "error"
```

手动端点漏了这段。

## 修法

### 1. 共享周期表

新增 `shared/timeframes.ts`：

```ts
export const CRYPTO_TIMEFRAMES = ["15m", "30m", "1h", "4h", "1d"] as const;
export const CN_TIMEFRAMES = ["15m", "30m", "1h", "1d"] as const;
export function timeframesFor(market): readonly Timeframe[];
```

| 页面 | 周期跟随 |
|---|---|
| 自动运行 | 账户市场 |
| 策略库 | 账户市场 |
| 回测 | **所选标的**的市场 |

回测跟标的不跟账户：研究可以跨市场。选 600036.SH 时是 4 个周期，
切到 BTCUSDT 后变成 5 个——浏览器实测确认。

### 2. 创建时按市场校验

```python
allowed = self.market.supported_timeframes(symbol_view.market)
if timeframe not in allowed:
    raise ValidationError(f"{symbol_view.market} 支持 {', '.join(allowed)}，不支持 {timeframe}")
```

放在"账户与标的市场一致"检查之后——先确认市场，再确认周期。

### 3. 手动 tick 逐实例隔离

与后台调度器对齐：单个实例出错记 `last_error` 并返回 `"error"`，
其余实例继续评估。

## 修的过程中踩到的坑

给回测页加"默认标的跟随账户市场"时，第一版写成：

```ts
useEffect(() => {
  if (inMarket 包含 symbol) return;
  setSymbol(默认标的);
}, [symbols.data, account, symbol]);
```

这个 effect 依赖 `symbol`，所以用户**每次手动切换到其他市场的标的，
都会被立刻改回账户市场的标的**——研究功能被默认值绑架了。

浏览器里表现为"选 BTCUSDT 没反应"。修法是让默认值只应用一次，
用一个 `symbolDefaultApplied` ref 记录，之后不再干预。

同时还要处理交接场景：从策略库带来的标的（可能跨市场）必须优先，
且要在 handoff ref 被清空之前捕获——所以另存一个 `initialSymbol`。

## 验证

| 检查项 | 结果 |
|---|---|
| A 股创建 4h 实例 → 422 且说明支持列表 | ✅ 新增测试 |
| 坏实例不拖垮 tick，其他实例照常评估 | ✅ 新增测试 |
| 自动运行页 A 股周期只有 4 个 | ✅ 浏览器确认 |
| 回测页 A 股标的 4 个周期、BTC 5 个 | ✅ 浏览器确认 |
| 回测页可从 A 股账户切到 BTC 研究 | ✅ 浏览器确认 |
| 交接来的跨市场标的不被覆盖 | ✅ 新增测试 |
| 前端类型检查 / 构建 | ✅ |
| 前端测试 | ✅ 54 passed（新增 2 项） |
| 后端全量回归 | ✅ 368 passed（新增 2 项） |

## 后续

- 周期表仍是前端硬编码的一份镜像。更稳妥的做法是从后端
  `/api/market/meta` 返回各市场的支持列表，前端读它。
  当前只有两处定义且都已集中，暂不改。
- `timeframesFor(undefined)` 回落到加密列表。首页在账户加载完成前是这种行为，
  加载后会被 effect 纠正。可以改成加载中不渲染周期选择器。
