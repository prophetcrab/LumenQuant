# 迭代记录 C91：实盘对账只比持仓，不比资金

## 排查过程

先确认一个怀疑点：`LiveReconciler.unlock_t1()` 这个方法存在但全仓库没有任何
调用点。看起来"实盘 A 股 T+1 永不释放"。

查证后是**误报**：`TradingEngine._session_duties()` 在每个交易日开盘做同样的事，
且作用范围是"所有 A 股账户"（含实盘），`T+1 settlement: released N position(s)`
的日志确实来自那里。`unlock_t1` 是历史遗留的冗余方法，不是缺陷。这一点记下来，
免得下次再查一遍。

继续看对账接口，发现了真问题。

## 缺陷：现金从不参与对账

`GET /api/brokers/ths/reconcile` 只比较持仓数量：

```python
rows = [...]                      # 每个 symbol 的本地/网关数量
return {"all_match": all(row["match"] for row in rows), "positions": rows}
```

实测（实盘账户初始 50 万，演练网关余额 100 万）：

```
positions_match: True    ← 都没有持仓，确实一致
all_match:       True    ← 但资金差了 50 万
```

**本地账本与网关余额差了一整个初始资金，对账却报"全部一致"。**

### 为什么这很危险

实盘账户的现金会以多种方式偏离网关：

- 用户在同花顺/券商 App 里手工交易；
- 外部转账、银证转入转出；
- 网关按自己的费率扣费，与平台配置的费率不同；
- 分红、送股、配股等公司行为。

持仓可能完全一致（都没买），而资金差异巨大。`all_match: true` 会让操作者
认为不需要做任何事。

## 修复

把资金纳入对账：

```python
cash_row = {
    "currency": account.base_currency,
    "local_cash": local_cash,
    "gateway_cash": balances.get(currency),
    "difference": gateway_cash - local_cash,
    "match": abs(gateway_cash - local_cash) <= 0.01,   # 网关按分取整
}
"all_match": positions_match and cash_match is True
```

三个细节：

1. **1 分钱容差**：网关通常按分取整，浮点直比会把舍入误差当成差异。
2. **网关未报告该币种时 `match` 为 `null`**，不是 `false` 也不是 `true`：
   "没拿到"与"拿到了但不等"是两件事，用 `null` 才能区分。
3. **`all_match` 要求 `cash_match is True`**：未知不能算一致。

网关不可达时 `cash.note` 记录原因，`all_match` 自然为 false——
对账本来就做不了，只是不再谎报成功。

## 前端

实盘对账卡片增加一行「资金 CNY」，展示本地/网关金额与差异：

```
资金 CNY    ¥500,000.00    ¥1,000,000.00    差 500000.00
```

`null` 显示为「未获取」的黄色徽章，与「一致/差异」在视觉上分开。

## 验证

集成测试 `test_reconcile_reports_a_cash_difference`：

```python
assert body["positions_match"] is True
assert body["cash_match"] is False
assert body["all_match"] is False
assert cash_row["gateway_cash"] - cash_row["local_cash"] == 500_000
```

把 `all_match` 改回只判断持仓后该测试失败。

## 回归

- 后端 **517 passed**（新增 1 项）
- 前端 66 passed，`tsc` 干净，构建通过

## 已知限制

1. **只对账总现金**：不区分可用/冻结/在途。网关的字段各不相同，强行映射容易
   产生假差异。
2. **不自动修正**：发现差异只报告，不调整本地账本。改账本需要人工判断原因，
   自动"以网关为准"会把平台侧的记账错误一起抹掉。
3. **加密通道无对账**：Binance 侧目前只有 `verify_credentials` 返回余额，
   没有同等的位置/资金比对接口。
