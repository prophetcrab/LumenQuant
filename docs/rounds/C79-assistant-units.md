# 迭代记录 C79：助手工具的数值必须自带单位

## 动机

排查目标是"AI 助手读行情/持仓时与前端缓存的一致性"。先核对数值本身：

```
API  price: 1310.8  change: 0.0848
AI   price: 1310.8  change: 0.0848     ✓ 一致
API/AI 持仓市价: 1310.8 / 1310.8        ✓ 一致
API/AI 权益: 499965.9192 / 499965.9192 ✓ 一致
assistant 不经过 TTLCache，8 次采样零差异
```

数值层面没有不一致：助手与前端读同一套服务、同一个实时源。缓存也不构成问题——
`market_cache` 的 TTL 是 1.5 秒，而报价轮询间隔 15 秒，同一轮内的值本来就相同。

真正的问题在**语义层**。

## 缺陷：跨市场的价格以相同键名返回

`market_snapshot` 不带标的时返回全部标的：

```json
[{"symbol": "000001.SZ", "price": 11.34, "change_24h_pct": -0.61},
 {"symbol": "ADAUSDT",   "price": 0.45,  "change_24h_pct": 0.68}]
```

14 行里 CNY 与 USDT 混排，字段名完全一样。LLM 若被问"哪个最便宜"，
会把 0.45 USDT 和 11.34 CNY 当作同一单位比较；若问"总市值"，
它可能直接把两种币种的数字相加。本地模式（不接 LLM）的文案同样是一串
无单位的数字。

### 还有一层：`change_24h_pct` 对 A 股是错的

A 股没有"24 小时涨跌"这个概念——它涨跌幅是**相对昨收**。字段名沿用了加密的语义，
助手的解释、LLM 的措辞都会跟着错。

## 修复

### 1. 每行都带 `market` / `currency` / `change_label`

```json
{"symbol": "600519.SH", "market": "cn_stock", "currency": "CNY",
 "price": 1310.5, "change_24h_pct": 0.0588, "change_label": "较昨收"}
```

`change_label` 显式区分"A 股较昨收"与"加密近 24 小时"，而不是让调用方去猜。

放在**每一行**而不是响应级：混排列表里响应级的标记会被读者忽略。

### 2. `market` 过滤参数

```json
{"market": "cn_stock"}   // 只返回 A 股
```

工具描述里也写明"A 股是 CNY、加密是 USDT，两种价格不能相加或直接比较"，
让模型在调用前就看到约束。

### 3. 账户与持仓摘要带币种

- `list_accounts` 增加 `market`、`currency`
- `account_overview` 增加 `currency`
- 本地模式文案：`权益 399,965.93 CNY` 而不是 `权益 399,965.93`

### 4. 本地模式按币种分组

```
行情：[CNY] 600519.SH 1310.5000（较昨收 +0.06%）；… |
      [USDT] BTCUSDT 70997.8890（近 24 小时 -2.10%）；…
```

不在一个句子里混排两种货币的数字。

## 验证

新增 4 个单元测试：

| 用例 | 断言 |
|---|---|
| 每行带市场与币种 | `market ∈ {crypto, cn_stock}`，`currency` 对应，`change_label` 正确 |
| 按市场过滤 | 只返回该市场，币种全部一致 |
| 账户工具带币种 | `list_accounts` / `account_overview` 都有 `currency` |
| 本地文案带单位 | 回答中出现 `USDT` |

还原 `_snapshot_row` 到旧的三字段版本后，2 个测试失败；恢复后 11 个全通过。

真实数据实测（A 股 + 合成加密，账户买入 600519.SH）：

```
行情：[CNY] 600519.SH 1310.5000（较昨收 +0.06%）；… |
      [USDT] BTCUSDT 70997.8890（近 24 小时 -2.10%）；…
账户「AI验收」权益 399,965.93 CNY，现金 268,915.93 CNY，持仓市值 131,050.00 CNY（敞口 32.8%）。
```

## 回归

后端 **479 passed**（新增 4 项），前端 66 passed，`tsc` 与构建干净。

## 已知限制

1. **不做汇率换算**：助手只说清单位，不会把 CNY 折成 USDT；跨市场合计仍需人工。
2. **本地模式只展示前 12 条**：按市场分组后可能某个市场被截断，但每个分组都带标签，
   不会产生错误结论。
3. **`change_24h_pct` 字段名保留**：改名会破坏既有前端与测试契约，
   用 `change_label` 说明语义是更低成本的方案。

## 同一轮发现的第二个缺陷：LLM 模式下标的默认值跨市场

本地模式有一条 `_default_symbol(account_id)` 会按账户市场推断标的，
**LLM 模式没有**：它只补 `account_id`，其余参数交给模型。

两个工具的参数默认值是硬编码的：

```python
def _signals(self, strategy_key, symbol="BTCUSDT", timeframe="1h")
def _backtest(self, strategy_key, symbol="BTCUSDT", timeframe="1h", bars=800)
```

于是 A 股用户问"给我双均线信号"，模型只传 `strategy_key` 时，会拿到一份
**BTCUSDT 的加密结果**，而回答读起来像是在描述 A 股。这比报错严重得多：
错误答案是静默的。

### 修复

1. `_llm_reply` 在调用工具前，按参数声明补齐默认值：
   - 缺 `symbol` → `_default_symbol(account_id)`（按账户市场）；
   - 缺 `timeframe` → `_default_timeframe(symbol)`（A 股用 `1d`，加密用 `1h`）。
2. 两个工具的 Python 默认值改为 `None`，由调用方解析，不再内置常量。
3. `_default_symbol` 在市场内没有任何标的时**抛错而不是回退到 BTCUSDT**：

```python
raise ValidationError(
    f"账户所属市场（{market}）下没有可用标的，无法确定默认标的；"
    "请先在数据页导入该市场的标的"
)
```

跨市场回退是这类缺陷的根源——"有数字返回"不等于"回答正确"。

### 验证

新增 `test_llm_tool_calls_get_the_account_market_symbol`：用 `MockTransport`
模拟一个只回 `{"strategy_key": "sma_cross"}` 的模型，断言注入后的 `symbol`
以 `.SH/.SZ/.BJ` 结尾、`timeframe` 为 `1d`。

- 还原注入逻辑 → 该测试失败（拿到 BTCUSDT）；
- 恢复后 → 12 个测试全通过。

这是 LLM 路径的**第一个**测试。此前该路径完全未被覆盖，
所以这个缺陷能一直存在而不被发现。
