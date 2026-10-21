# 迭代记录 C69：篮子回测接入组合分析

## 动机

C63 结尾写着：

> 多标的回测结果目前不能进入组合分析（那需要按 sleeve 拆成单标的曲线）。

这造成一条断裂的研究链路：用户跑完一篮子回测，看到组合收益，
想知道"这几个标的到底有多分散"——而这个答案在组合分析页，
需要先逐个重跑单标的回测。

**数据早就在数据库里了，只是形状不对。**

## 实现

### sleeve 落成普通回测

多标的回测保存时，为每个 sleeve 额外写一条标准 `BacktestRun`：

```python
BacktestRun(
    strategy=..., symbol=sleeve["symbol"], timeframe=...,
    metrics_json=json.dumps(result.metrics),
    equity_json=json.dumps(result.equity_curve),
    trades_json=...,
    initial_capital=sleeve["allocation"],
    origin="multi",          # 新增标记
)
```

父记录 `MultiBacktestRun` 增加 `child_run_ids_json`，按 `symbols_json` 的顺序保存。

这样组合分析**完全不需要改动**——它本来就接受 `run_ids`，
而 sleeve 就是普通的回测记录。

### `origin` 标记的作用

单标的回测历史（`GET /api/backtests`）必须过滤掉 sleeve：

```python
select(BacktestRun).where(BacktestRun.origin != "multi")
```

否则一次三标的回测会在历史列表里插入三行噪音，而它们单独看没有意义
（资金只有三分之一，且是同一策略的重复）。

sleeve 仍可通过 `GET /api/backtests/{id}` 单独访问——组合分析需要它。

### 前端

组合分析页顶部加"从多标的回测导入"下拉，选中后：

- 自动勾选该篮子的全部 sleeve
- 权重重置为等权
- 初始资金填为该篮子原本的组合资金

一个已经保存过、但没有子记录的旧记录会被过滤掉——选了也不会有任何反应。

## 实测

跑一次 BTC/ETH 的篮子回测，进组合分析，导入，分析：

```
相关性矩阵
           BTCUSDT   ETHUSDT
BTCUSDT      1.00     -0.01
ETHUSDT     -0.01      1.00

组合收益   -0.93%
分散效果   28.9%（加权 8.55% → 实际更低）
```

负相关表示两个 sleeve 的波动互相抵消，分散有效。

## 顺带修掉：相关性矩阵标签只看得到策略名

C31 写矩阵标签时只保留 ` · ` 之前的部分（策略名），理由是"太长"。
但当同一策略跑多个标的时（正是篮子的典型情形），所有行头都是 `SMA_CROSS`，
矩阵无法阅读。

改为显示标的（并在重复时保留 run 后缀）：

```
BTCUSDT          ETHUSDT
BTCUSDT   1.00   -0.01
ETHUSDT  -0.01    1.00
```

### 一个实现细节：id 在 flush 之后才存在

第一版每个 sleeve 的 id 都是 `None`：

```python
self.session.add(run)
ids.append(run.id)      # None: default 是 Python 端 lambda，flush 前未生成
```

改为 `add` → `flush` → `append(run.id)`。

## 设计取舍

- **为什么 sleeve 用普通表而不是新表**：组合分析、详情页、删除都已经
  对 `BacktestRun` 工作良好。新表意味着所有这些路径都要写第二份。
- **为什么 sleeve 的 `initial_capital` 是分配额而非组合总额**：
  sleeve 的曲线是"这三分之一的钱在这个标的上怎么走"。用总额会让
  收益率看起来只有实际的三分之一。
- **为什么再平衡不影响 sleeve**：再平衡是组合层面的资金调度，
  不是某个标的的策略行为。sleeve 应当保存"该策略在该标的上的原始曲线"，
  这样相关性分析才有意义。已加测试锁定。
- **为什么导入时不导入权重**：父记录有原始权重，但对相关性研究而言
  等权更直观。用户可以再手动调整。

## 验证

| 检查项 | 结果 |
|---|---|
| 每次篮子回测产生 N 条 sleeve | ✅ 新增测试 |
| sleeve 可通过 `/api/backtests/{id}` 访问 | ✅ |
| sleeve 不出现在单标的历史列表 | ✅ 新增测试 |
| sleeve 可直接用于组合分析 | ✅ 新增测试 |
| 再平衡前后 sleeve 曲线一致 | ✅ 新增测试 |
| 导入下拉过滤无子记录的旧记录 | ✅ 浏览器确认 |
| 导入后自动勾选并可分析 | ✅ 浏览器确认 |
| 矩阵标签显示标的 | ✅ 浏览器确认 |
| 后端全量回归 | ✅ 410 passed（新增 4 项） |
| 前端测试 / 类型检查 / 构建 | ✅ 66 passed |

## 后续

- 删除父记录时 sleeve 不会跟着删除，会留下孤儿行。需要一个级联或
  定期清理（`origin='multi'` 且父记录不存在的行）。
- 导入时可以用父记录的原始权重而不是等权，作为可选项。
