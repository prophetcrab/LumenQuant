# 迭代记录 C41：挂单生命周期与 AI 助手的新装状态

## 检验一：挂单与冻结资金

按用户操作顺序走一遍挂单生命周期：

```
initial                    cash=100000.00 available=100000.00 pos=[] orders=[]
place resting (limit)      cash=100000.00 available=87152.45 pos=[] orders=[('buy','open',0.1,0.0)]
cancel                     cash=100000.00 available=100000.00 pos=[] orders=[('buy','cancelled',0.1,0.0)]
```

三个要点都正确：

- 挂单后现金不变（`cash` 100000），**可用现金**减少 12847.55（冻结）
- 冻结额 ≈ 0.1 × 128475（限价）+ 手续费
- 撤单后可用现金完全恢复，状态变为 `cancelled`

再验证超额承诺：两笔各 6 万名义的挂单，账户只有 10 万。

```
order 1: 201 open
order 2: 201 open
cash: 100000.0 available: 39944.84
```

第二笔被接受，但可用现金只剩 39944——说明**冻结是累加的**，并没有按每笔单独判断。
这是正确的：单笔限额 6 万 < 10 万，两笔合计超出由"可用现金"这一层挡住后续下单，
而不是拒绝第二笔。用 `available_cash` 作为下单预算的来源，所以第三笔会被拒。

一个观察（非缺陷）：这两笔挂单如果同时成交，总成本 12 万会超过 10 万现金。
撮合时的 `insufficient cash at execution time` 会拒绝第二笔并标记
`rejected`。也就是说不会出现负余额——这条保护已经在 C14 轮验证过。

## 检验二：AI 助手在全新安装下

第一次使用产品的人会打开 AI 助手问点什么。实测四种问法：

```
账户总览          -> 404 {'code': 'not_found', 'message': 'no active account; create one first'}
我现在能开仓吗     -> 500 ValueError: no active account
```

**没有账户时，助手直接报错。**

- `account_overview` 抛 `NotFoundError` → 404
- `risk_overview`（C32 新增）在 `_default_account()` 里抛裸 `ValueError` → 未捕获 → **500**

第二个更糟：一个"查询"操作返回 500，说明有未处理异常，日志里是完整 traceback。
而用户只是问了一句"我现在能开仓吗"。

### 修法

在意图分发处捕获工具异常，转成一句可执行的话：

```python
try:
    result = self.toolbox.call(tool, arguments)
except AppError as exc:
    return AssistantReply(answer=self._explain(exc), ...)
except ValueError as exc:
    return AssistantReply(answer=self._explain(exc), ...)
```

`_explain()` 把错误翻译成下一步动作：

> 还没有可用的交易账户，所以查不到持仓、风险或订单。
> 请先在「账户」页创建一个模拟账户，然后回来问我。

修完后四种问法都是 200 + 上述指引，`tool_calls` 里记录 `error` 字段供排查。

## 设计取舍

- **为什么在 service 层捕获而不是让 router 统一处理**：助手是对话界面，
  错误应该是"回答的一部分"而不是 HTTP 失败。用户不会因为看到 500 而知道该去建账户。
- **为什么保留 `tool_calls[].error`**：对话回答要友好，但排查需要原始原因。
  两个都保留，一个给人看，一个给日志看。
- **为什么 `_risk_overview` 抛的是 `ValueError` 而不是 `AppError`**：这是 C32 的疏漏
  （新增工具时没沿用错误类型）。本轮在调用侧兜住，没有改工具本身——
  改动面最小，且其他未来工具也会有同样的疏漏，兜底更有价值。

## 验证

| 检查项 | 结果 |
|---|---|
| 挂单冻结可用现金，`cash` 不变 | ✅ |
| 撤单后可用现金完全恢复 | ✅ |
| 多笔挂单冻结累加，第三笔被可用现金挡住 | ✅ |
| 无账户时四种问法都返回 200 + 建账户指引 | ✅ 新增测试 |
| `tool_calls` 记录原始错误 | ✅ |
| 后端全量回归 | ✅ 353 passed（新增 2 项） |

## 后续

- 助手目前的错误翻译是基于字符串匹配的启发式。若工具类型增多，
  应当在 `AppError` 上引入 `code` 字段做结构化判断（`no_account`、`symbol_not_found`）。
- 挂单列表没有"冻结金额"这一列，用户只能从组合页推断。可以在订单表里显示每笔的占用。
