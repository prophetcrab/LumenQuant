# 迭代记录 C42：币种单位一致性

## 动机

A 股模式引入后，金额同时存在两种货币：加密用 USDT，A 股用 CNY。
后端一直有 `base_currency` 字段并贯穿到账户、组合与交易接口，
但**前端图表层没有接**——它自己写死了美元符号。

## 缺陷：图表 tooltip 硬编码 `$`

`backtestEquityOption` 与 `equityOption` 的 tooltip：

```ts
valueFormatter: (value) => `$${Number(value).toLocaleString(...)}`
```

这意味着在 A 股回测里，期末权益卡片显示 `¥229,600.90`（正确，走 `cash()`），
而同一页的权益曲线 tooltip 显示 `$229,600.90`。**同一笔钱，两种货币符号。**

组合分析的成分表更明显：`price(item.allocation)` 完全不带符号，
显示成裸数字 `50,000.00`，用户无法判断这是人民币还是美元。

### 修法

`backtestEquityOption(points, comparison, currency)` 与
`equityOption(points, baseline, currency)` 都接受币种，用 `currencySymbol()` 渲染。
调用方传入真实币种：

| 页面 | 币种来源 |
|---|---|
| 回测页 | 由标的后缀推断（`.SH/.SZ/.BJ` → CNY） |
| 组合分析 | 报告里的 `market` 字段 |
| 组合页 / 总览 | `account.base_currency` |

组合分析的成分资金从 `price()` 改成 `cash(allocation, currency, 0)`。

## 顺带修掉：金额缺尾随零

写测试时发现 formatter 输出 `¥1,234.5` 而不是 `¥1,234.50`。
原因是只设了 `maximumFractionDigits: 2`，没有 `minimumFractionDigits`——
`1234.5` 在 JS 里正好是 1 位小数，于是原样输出。

对金额来说这是错的：`¥1,234.5` 看起来像被截断了，而 `¥10` 更像一个整数计数而不是金额。
固定两位小数。

## 验证

| 检查项 | 结果 |
|---|---|
| A 股回测图表 tooltip 用 ¥ | ✅ 单元测试 |
| 加密回测图表 tooltip 用 $ | ✅ 单元测试 |
| 未传币种时默认 USDT | ✅ 单元测试 |
| 组合页 A 股全程无 `$` | ✅ 浏览器确认全文无美元符号 |
| 金额固定两位小数 | ✅ 单元测试 |
| 前端类型检查 / 构建 | ✅ |
| 前端测试 | ✅ 45 passed（新增 4 项） |
| 后端全量回归 | ✅ 353 passed |

## 设计取舍

- **为什么币种作为参数而不是全局状态**：一张页面里可能同时展示不同市场的回测
  （组合分析已拒绝跨市场，但历史列表不会）。显式传参让每个图表为自己的显示负责。
- **为什么默认值是 USDT**：与后端 `Account.base_currency` 的默认一致。
  默认「没有符号」会让所有调用点立刻显式传参，但也会让忘记传的地方静默退化；
  默认 USDT 至少与绝大多数调用方的实际语义吻合。
- **为什么用 `cash()` 而不是给 `price()` 加符号**：`price()` 的职责是价格精度
  （0.0001234 这类），金额需要的是固定小数位和货币符号，两者语义不同。

## 追加：币种改由服务端声明

上面的实现仍让前端从标的后缀推断币种（`.SH/.SZ/.BJ` → CNY）。这是脆弱的：
新增一个市场（港股 `.HK`）就会走错分支，而且"货币"是数据属性，不该由展示层猜。

改为后端在回测与组合结果里直接返回 `currency`：

```python
"currency": "CNY" if row.symbol.endswith((".SH", ".SZ", ".BJ")) else "USDT"
```

前端优先使用该字段，仅在读取本轮之前存下的旧回测时回退到后缀推断。
`tests/integration/test_cn_api.py` 增加断言：A 股回测返回 CNY，加密返回 USDT。

## 后续

- 前端仍有多处直接用 `toFixed()` 拼百分比（如 `volatility_pct.toFixed(2)`）。
  这些是量值不是金额，问题不大，但可以统一成 `magnitude()` 辅助函数，
  避免再出现 C31 那种"回撤带 + 号"的问题。
- 标的后缀推断币种不够严谨：`.SH/.SZ/.BJ` 之外，未来若有港股（`.HK`）会走错分支。
  更稳的做法是让后端在回测结果里直接返回 `currency`。
