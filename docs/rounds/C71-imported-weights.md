# 迭代记录 C71：导入篮子时保留原始权重

## 动机

C69 的导入功能把篮子的 sleeve 加进组合分析，但做了两件错事：

1. `setWeights({})` —— 把权重重置为空（等权）
2. 只填充 `selected`，没有把 sleeve 放进可见列表

第二条更隐蔽：界面显示"已选 2 个"，表格里却一条都没有，
用户既看不到成分，也无法调整权重。

C69 的文档把第一条列为可选的后续项（"导入时可以用父记录的原始权重"），
但它不是可选项——用户配置 4:1 的篮子，导入后变成 1:1，分析的是另一个组合。

## 实现

### 恢复权重

`child_run_ids` 与 `symbols` / `weights` 是同一顺序写的，可以按下标配对：

```ts
const restored: Record<string, string> = {};
ids.forEach((runId, index) => {
  const weight = basket?.weights?.[index];
  if (typeof weight === "number" && weight > 0) {
    restored[runId] = String(weight);
  }
});
setWeights(restored);
```

权重输入框按 `run.id` 索引，正好匹配。

### 让导入的 sleeve 可见

sleeve 被 `origin='multi'` 排除在单标的历史列表之外（C69 的设计），
所以 `runs` 里没有它们。加一个按需查询：

```ts
const orphanIds = selected.filter((id) => !historyIds.has(id));
const imported = useQuery({
  queryKey: ["imported-runs", orphanIds],
  queryFn: () => Promise.all(orphanIds.map((id) => api.backtest(id))),
  enabled: orphanIds.length > 0,
});

const runs = [...extra, ...base];   // 导入的排在前面
```

导入的 sleeve 排在最前——那是用户刚操作的对象，放在列表底部等于藏起来。

## 实测

创建一个 4:1 的 BTC/ETH 篮子（$240,000 / $60,000），然后导入组合分析：

```
已选 2 个

  sma_cross  BTCUSDT  1h  -0.79%  +1.35%  300   0.8
  sma_cross  ETHUSDT  1h  -1.79%  +2.36%  300   0.2

组合初始资金  300000
```

分析结果：

```
权重  80.0% / 20.0%
资金  $240,000 / $60,000
组合收益 -0.99%
```

-0.99% 落在 -0.79% 与 -1.79% 之间，且更靠近 -0.79%——符合 80/20 的加权。
如果权重被重置成等权，结果应该在中点整 -1.29% 附近，不会是这个数。

## 设计取舍

- **为什么不用父记录的 `rebalance_bars`**：组合分析是对已存曲线的合并，
  它本身不做再平衡。父记录的再平衡已经体现在合并后的曲线上，
  但 sleeve 是未再平衡的原始曲线（C69 的设计）。两者语义不同，不能混用。
- **为什么导入的 sleeve 不进入永久列表**：它们仍是父记录的派生数据。
  只在被选中时临时显示，避免污染单标的历史。
- **为什么下标配对是安全的**：`_persist_sleeves` 按 `sleeves` 顺序写入，
  而 `sleeves` 来自 `zip(views, shares)`，与 `symbols` 同序。
  这一点在 C69 的实现里已经保证，此处依赖它。

## 验证

| 检查项 | 结果 |
|---|---|
| 4:1 篮子导入后权重为 0.8/0.2 | ✅ 浏览器确认 |
| 资金显示 $240,000 / $60,000 | ✅ |
| 导入的 sleeve 出现在表格顶部 | ✅ |
| 组合收益符合加权预期（-0.99%） | ✅ |
| 组合资金沿用篮子的 300000 | ✅ |
| 后端全量回归 | ✅ 412 passed |
| 前端测试 / 类型检查 / 构建 | ✅ 66 passed |

## 后续

- 若用户手动改过权重后重新选择篮子，当前会覆盖为篮子的原始权重。
  这是"选择"这个动作的合理语义，但可以加一个提示。
- 组合分析目前不能反向保存：把调整后的权重存回篮子需要父记录可编辑。
