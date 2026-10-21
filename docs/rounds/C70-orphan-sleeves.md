# 迭代记录 C70：孤儿 sleeve

## 动机

C69 让篮子的每个 sleeve 落成普通回测，父记录用 `child_run_ids` 指回它们。
但删除父记录时没有任何代码处理这些子记录：

```
删除篮子 → 父记录消失 → 三条 sleeve 还在库里
```

后果比"占空间"严重：这些 sleeve 会出现在组合分析的候选列表里，
用户看到三个自己没跑过的回测（单一标的、三分之一资金），
却无法从单标的历史里删除它们（被 `origin='multi'` 过滤掉了）。

## 实现

### 级联删除

```python
def delete_run(self, run_id):
    row = ...
    child_ids = json.loads(row.child_run_ids_json or "[]")
    for child in session.scalars(select(BacktestRun).where(BacktestRun.id.in_(child_ids))):
        session.delete(child)
    session.delete(row)
    session.commit()
```

### 兜底清理

即使有级联，两种情况下仍会留下孤儿：

1. 两次 delete 之间进程崩溃（虽然在同一事务里，理论上不会半途提交）
2. 更早的版本删除父记录时没有级联逻辑

所以加一个 `prune_orphan_sleeves()`：

```python
referenced = 所有父记录 child_run_ids 的并集
orphans = 所有 origin='multi' 但不在 referenced 里的行
删除它们
```

挂进调度器的小时级维护（与日志修剪、告警修剪同一节拍）。

## 设计取舍

- **为什么不用数据库外键 CASCADE**：`BacktestRun` 没有指向父记录的外键——
  `child_run_ids_json` 是一个 JSON 数组，SQL 无法据此建立约束。
  改成关联表更规范，但那样组合分析的 `run_ids` 查询、
  详情页、删除逻辑都要跟着改，收益不抵成本。
- **为什么 prune 扫描全部父记录再比对**：父记录数量是几十到几百量级，
  一次全表读 JSON 数组足够快。用 SQL 的 `NOT IN` 会因 JSON 存储而无法下推。
- **为什么只删 `origin='multi'`**：手工跑的单标回测永远不该被自动清理。
  这个标记把"派生数据"与"用户资产"区分开。

## 验证

| 检查项 | 结果 |
|---|---|
| 删除篮子后其 sleeve 返回 404 | ✅ 新增测试 |
| 人为制造孤儿后 prune 能清掉 | ✅ 新增测试 |
| prune 不碰普通回测 | ✅（仅过滤 origin='multi'） |
| 调度器每小时执行 | ✅ 实现 |
| 后端全量回归 | ✅ 412 passed（新增 2 项） |
| 前端测试 | ✅ 66 passed |

## 后续

- 如果将来 sleeve 支持单独保留（比如用户想只保留其中两个标的的曲线），
  需要给 `child_run_ids` 一个"已脱离"的语义，不能只靠 prune 兜底。
- 类似的问题可能存在于其它"父—子"关系：目前只有这一处。
