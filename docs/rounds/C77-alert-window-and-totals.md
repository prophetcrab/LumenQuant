# 迭代记录 C77：告警的时间范围与跨账户合计

## 动机

C67/C68 的文档留下了两条限制：

> 告警缺少时间范围查询与跨账户合计；侧栏角标只统计当前账户。

接入实时数据后这两条不再是"功能欠缺"，而是**可观测性缺陷**：

- 行情中断告警没有 `account_id`，按账户过滤时会漏；
- 断线、熔断这类事件是"昨晚发生了什么"，没有一个时间窗口就无法回答；
- 熔断发生在**另一个账户**时，侧栏角标完全不动——用户必须逐个切换账户才发现。

## 实现

### 1. 时间窗口

`AlertService.list()` 与 `summary()` 都接受 `since` / `until`（左闭右开）。

两个边界在比较前统一转 UTC：列是 timezone-aware，而 SQLite 会把裸 datetime
按 UTC 处理；不归一化会得到一个差几小时的窗口，且**不报错**——只是安静地
少给或多给数据。

```python
def _apply_window(stmt, *, since, until):
    if since is not None:
        stmt = stmt.where(Alert.created_at >= ensure_utc(since))
    if until is not None:
        stmt = stmt.where(Alert.created_at < ensure_utc(until))
    return stmt
```

前端提供「全部时间 / 近 24 小时 / 近 7 天 / 近 30 天」，把选择转成 ISO 字符串
直接作为查询参数，不在后端维护一套枚举。

### 2. 跨账户合计

`/alerts/summary?all_accounts=true` 额外返回 `accounts` 数组：

```json
{
  "unacknowledged": 3,
  "by_level": {"critical": 2, "warning": 1},
  "accounts": [
    {"account_id": "...", "unacknowledged": 1, "by_level": {"critical": 1}},
    {"account_id": "",    "unacknowledged": 1, "by_level": {"critical": 1}}
  ]
}
```

无账户归属的系统告警归到 `account_id: ""`，**不塞给任何一个账户**——
把它算进某个账户会让那个账户的数字凭空变多，也让真正的全局性无从判断。

### 3. 侧栏角标

由"当前账户"改为跨账户合计，`aria-label` 变为「N 条待确认（全部账户）」。
告警中心新增第 5 张卡片「其他账户待确认」，显示全局总数减去当前账户的差值，
并注明"含无账户归属的系统告警"。

## 验证

### 集成（2 个）

| 用例 | 断言 |
|---|---|
| 时间窗口 | 3 天前的告警在 `since=1 天前` 中不出现；在 `until=1 天前` 中出现 |
| 跨账户合计 | 当前账户视图 2 条（自身+系统）；`all_accounts` 3 条；明细含空 `account_id` 一条 |

### 2K 浏览器

构造：账户 A 一条熔断（严重）、账户 B 一条 5 天前的止损（警告）、
一条无账户归属的行情中断（严重）。

- 切到账户 A：待确认 2（自身熔断 + 系统告警），「其他账户待确认」1，
  「全局 3 条，含无账户归属的系统告警」，侧栏角标 **3**，`aria-label`
  为「3 条待确认（全部账户）」；
- 切到账户 B：「全部时间」能看到 5 天前的旧止损；改为「近 24 小时」后
  该行消失。

### 回归

后端 **467 passed**（新增 2 项），前端 66 passed，`tsc` 与构建干净。

## 已知限制

1. **窗口只作用于告警列表与统计**，不影响 `categories` 的计数（分类下拉
   代表"存在哪些类别"，按时间收窄会让选项忽隐忽现）。
2. **跨账户合计不含已确认**：与其余接口一致，确认过的即视为已处理。
3. **时间粒度到秒**，没有按小时/天聚合的趋势视图。
