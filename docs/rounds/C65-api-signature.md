# 迭代记录 C65：API 参数签名统一（并解决 C64 的测试失败）

## 动机

C64 结尾记着一笔债：

> `api.alerts` 的参数签名不一致（对象 vs 其它接口的位置参数）是这次暴露出的
> 一个真实设计债务——它让 mock 变复杂，也可能是测试失败的根源。

同时 C64 没留下告警页的组件测试，理由写的是"没能让它稳定通过"。
这一轮先修签名，再看测试是否随之解决。

## 缺陷

`api.alerts` 是全项目唯一接受"选项对象"的列表接口：

```ts
alerts: (params: { account_id?; unacknowledged_only?; level?; limit? } = {}) => ...
```

其它所有列表接口都是位置参数：

```ts
orders: (accountId, status?, limit = 200) => ...
fills: (accountId, limit = 100) => ...
backtests: (limit = 30) => ...
```

## 修法

```ts
alerts: (accountId?, unacknowledgedOnly = false, limit = 100, level?) => ...
```

调用处从：

```ts
api.alerts({ account_id: accountId, unacknowledged_only: filter === "open", limit: 200 })
```

变为：

```ts
api.alerts(accountId, filter === "open", 200)
```

## 测试随之通过

重写告警页测试（`beforeEach` 里设置 `mockImplementation`，因为全局
`restoreMocks: true` 会清掉实现），三项全部通过：

```
✓ shows a pending alert with a translated category
✓ acknowledges a single alert
✓ disables bulk acknowledge when nothing is pending
```

**这证明 C64 的判断是对的**：对象参数确实让 mock 无法按预期匹配，
页面因此一直停在 loading 态。当时的排查方向（promise 没 resolve、
`restoreMocks` 顺序）都不对，真正的根源是签名本身。

## 为什么一个签名会导致测试失败

值得记下来，因为这是容易重复的坑。

对象参数意味着每次调用都构造一个新对象。mock 用
`mockImplementation(() => Promise.resolve(data))` 时本来不该受影响——
所以最初的直觉（"参数无关"）看起来合理。

但 `api.alerts` 的默认值 `= {}` 加上 `qs(params)` 的组合，
让调用方在传参时必须构造完整对象；测试里的断言与调试输出都被这个
间接层干扰，定位成本远超问题本身。

位置参数让调用变得直白：`api.alerts(accountId, true, 200)`。
mock 不需要理解参数形状，行为立刻可预测。

## 设计取舍

- **为什么参数顺序是 `(accountId, unacknowledgedOnly, limit, level)`**：
  与 `orders(accountId, status?, limit)` 对齐——账户、筛选、数量、其余。
  `level` 放最后因为用得最少。
- **为什么保留 `level`**：后端支持按级别过滤，接口保留它，
  前端暂时不用。删掉会让将来加筛选时又要改一次签名。
- **为什么不在后端也统一**：后端用的是 FastAPI 的 query 参数，
  已经是一致的位置无关形式（`?account_id=&unacknowledged_only=&limit=`）。
  不一致只存在于前端封装层。

## 验证

| 检查项 | 结果 |
|---|---|
| 签名改为位置参数，调用处同步更新 | ✅ |
| 告警页 3 项组件测试稳定通过 | ✅ |
| 前端全量 63 passed | ✅ |
| 类型检查 / 构建 | ✅ |
| 后端全量 403 passed | ✅ |

## 后续

- 可以加一条 lint 规则（或代码评审清单项）：列表接口一律用位置参数，
  避免再出现"唯一一个对象参数"的例外。
- 告警页仍缺的一项：侧栏未确认数角标。需要跨页面的查询状态，
  属于下一个功能点。
