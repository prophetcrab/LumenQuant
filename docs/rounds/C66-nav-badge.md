# 迭代记录 C66：侧栏未确认告警角标

## 动机

C64 的告警中心解决"告警存在哪里"，但没解决"我怎么知道有告警"。
用户必须主动进入告警页才能看到——而告警的意义恰恰是**不需要去找**。

C65 的文档把这条列为下一个功能点。这一轮做掉。

## 实现

`Layout` 新增一个查询：

```tsx
const alertSummary = useQuery({
  queryKey: ["alert-summary", accountId],
  queryFn: () => api.alertSummary(accountId),
  enabled: Boolean(accountId),
  refetchInterval: 15_000,
});
```

告警导航项标记 `badge: "alerts"`，渲染时若有未确认数就显示：

```tsx
{item.badge === "alerts" && unacked > 0 ? (
  <span className="nav-badge" aria-label={`${unacked} 条待确认`}>
    {unacked > 99 ? "99+" : unacked}
  </span>
) : null}
```

### 样式

```css
.nav-badge {
  margin-left: auto;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--negative);
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
  text-align: center;
}
```

`margin-left: auto` 把徽章推到右边缘——数字出现或消失时，标签文字不会左右跳动。

### 为什么用同一个 queryKey

`["alert-summary", accountId]` 与告警页的查询键相同。React Query 会复用缓存：
从告警页确认完再回到其他页面，角标立刻消失，不会等 15 秒轮询。

## 实测

有 1 条未确认熔断告警时：

```
告警1     ← 侧栏徽章
aria-label: "1 条待确认"
```

点「全部确认」后：

```
告警      ← 徽章消失
页面显示：没有待确认的告警
```

## 设计取舍

- **为什么轮询 15 秒而不是 WebSocket**：轮询已足够——告警不是毫秒级决策，
  而且系统没有常驻推送通道。15 秒与风险页的 10 秒、自动运行页的 5 秒同一量级。
- **为什么超过 99 显示 "99+"**：三位数会撑破徽章宽度，且具体数字不再有信息量。
- **为什么加 `aria-label`**：屏幕阅读器读到"告警 3"时不知道 3 是什么。
  完整标签让语义明确。
- **为什么不去掉 `enabled: Boolean(accountId)`**：没有账户时告警查询会返回空，
  但多一次无意义的请求。与告警页保持一致。
- **为什么不在总览页也放大字报**：那是另一个层面的设计（首屏摘要）。
  侧栏角标已经保证用户在任意页面都能看到。

## 验证

| 检查项 | 结果 |
|---|---|
| 有未确认时显示数字 | ✅ 浏览器确认（"告警1"） |
| `aria-label` 完整 | ✅ 组件测试 |
| 确认后角标消失 | ✅ 浏览器确认 + 组件测试 |
| 超过 99 显示 "99+" | ✅ 组件测试 |
| 标签文字不因徽章出现而位移 | ✅ `margin-left: auto` |
| 前端全量 66 passed | ✅ |
| 类型检查 / 构建 | ✅ |
| 后端全量 403 passed | ✅ |

## 后续

- 告警可以按 `category` 过滤（当前只看级别与是否确认）。
- 长期运行后需要清理策略：已确认且超过 N 天的告警可以归档，
  否则表会无限增长。
- 侧栏角标目前只统计当前账户。若要多账户汇总，需要在切换器旁另给一个总数。
