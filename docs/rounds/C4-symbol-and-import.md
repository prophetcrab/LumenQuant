# 迭代记录 C4：标的创建与真实数据导入

## 发现的问题

### 1. 创建标的时 `market` 字段被静默丢弃（严重）

调用：

```json
POST /api/market/symbols
{"code":"601899.SH","name":"紫金矿业","initial_price":18.5,"market":"cn_stock","board":"main"}
```

返回的标的却是 `"market": "crypto"`。

原因：`SymbolCreate` 这个 Pydantic 模型里根本没有 `market`、`exchange`、`board`、
`prev_close`、`limit_pct`、`is_st` 字段。Pydantic 默认忽略多余字段，
所以 A 股标的被按加密标的创建：报价 `USDT`、手数 `0.0001`、涨跌停 `0`。

连锁后果：
- 日线 CSV 导入被拒绝（提示"只能导入基础周期 15m"）；
- 该标的没有涨跌停约束，交易规则全部失效；
- 前端把它显示成加密标的。

这是典型的"接口看起来成功、数据全错"。修复：

- `SymbolCreate` 补齐全部市场字段，并用 `model_validator` 按市场填默认值
  （A 股：`CNY` 报价、100 股手数、默认主板、从代码后缀推断交易所）；
- 路由把这些字段透传给 `add_symbol`；
- 没有传 `market` 时仍按 crypto 处理，保持既有行为不变。

### 2. 真实形态 CSV 导入验证

用随机游走 + 涨跌停夹紧的方式生成 420 个交易日、每根严格满足
`high ≥ max(open, close)`、`low ≤ min(open, close)` 的日线 CSV，导入新标的
`601988.SH`：

```
imported 420, skipped 0, errors []
```

随后回测 400 根日线：

| 指标 | 值 |
|---|---|
| 交易数 | 7 |
| 总收益 | +33.88% |
| 最大回撤 | 22.16% |
| Sharpe | 1.27 |
| 年化基准 | 242 |
| 买入持有 | -7.10% |
| 强制规则 | `market=cn_stock, t_plus_one=true, lot_size=100, limit_pct=0.1` |

说明：这是一条合成但形态真实的数据，仅用于验证"导入 → 规则 → 回测"链路，
不代表策略有效性。

## 验证

| 检查项 | 结果 |
|---|---|
| 创建 A 股标的 | `market=cn_stock, quote=CNY, lot=100, exchange=SH, limit=0.1` |
| 创建加密标的 | `market=crypto, quote=USDT, lot=0.0001`（无回归） |
| 日线 CSV 导入 | 420 行全部导入，0 错误 |
| 回测规则 | 服务端强制 A 股规则，客户端无法关闭 |
| 全量测试 | **193 passed**（新增 2 项） |

## 清理

巡检过程中创建的错误标的（`601899.SH` 被存成 crypto）已从数据库删除，
其 K 线一并清除。`601988.SH` 保留作为真实数据导入的样例。

## 教训

**Pydantic 的默认行为是"忽略未知字段"，这让 API 契约错误变得不可见。**
凡是客户端可能传、但服务端未声明的字段，都应该：

1. 要么显式声明；
2. 要么用 `model_config = ConfigDict(extra="forbid")` 直接报错。

本项目在策略参数上已经用了 `extra="forbid"`，但请求体模型没跟上。
这是一个应当系统排查的模式，已列入后续任务。
