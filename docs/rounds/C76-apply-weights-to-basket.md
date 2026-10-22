# 迭代记录 C76：按调整后的权重重跑篮子

## 动机

C71 把这条留在"已知限制"里：

> 组合分析无法把调整后的权重回存到篮子。

用户调整权重、看到新的相关性与分散收益，然后没有任何办法把这个决策落地——
只能回到「多标的」页重新手输参数。

## 语义：不能只改 weights_json

最省事的实现是 UPDATE 篮子的 `weights_json`。这是错的：

一条篮子记录里，`weights_json` 只是**输入**之一。与它并列存储的还有
`metrics_json`、`equity_json`、`components_json`（每个成分的 `allocation`）。
如果只改权重，记录就会变成自相矛盾：

- 权益曲线仍是旧权重的路径，却挂在新权重旁边；
- 成分表里的分配金额仍是旧的；
- 收益、回撤、Sharpe 全部对不上。

用户会继续把那条曲线当作"当前权重下的表现"来读。这比没有功能更糟。

**不同权重是不同组合**，唯一诚实的表示是产生一条新记录。

## 实现

`POST /api/backtests/portfolio/apply-weights`

```json
{"run_ids": ["..."], "weights": [6, 3, 1]}
```

服务端 `MultiSymbolBacktester.rerun_with_weights`：

1. 每个 run 必须存在且 `origin='multi'`（只有篮子成分才有"权重"这回事）；
2. 反查父篮子：扫描 `multi_backtest_runs`，找 child 集合包含全部请求 id 的那条；
3. **必须提交该篮子的全部成分**——只给一部分会静默丢掉用户仍以为自己持有的仓位；
4. 按篮子原本的 `symbols_json` 顺序重排权重，避免 UI 传来的顺序改变语义；
5. 用父篮子的 strategy / timeframe / bars / params / rebalance_bars / initial_capital
   重跑一次，返回新篮子并带 `previous_run_id`。

### 前端

组合分析面板在"分析 N 个回测"旁多一个「按当前权重重跑篮子」按钮，
仅在当前选择**恰好等于某一个篮子的完整成分集**时出现。
判断逻辑与后端一致，避免用户点了才收到 422。

## 验证

### 集成（4 个）

| 用例 | 断言 |
|---|---|
| 按新权重重跑 | 新 id、`previous_run_id` 指向原篮子、成分 share = 0.6/0.3/0.1、收益与等权不同、原篮子 `weights` 不变 |
| 只给部分成分 | 422，提示"必须全部" |
| 混入非篮子成分 | 422，提示"不是篮子回测的成分" |
| 成分来自两个篮子 | 404，提示找不到所属篮子 |

### 真实运行（2K 浏览器）

1. 建一个 3 标的篮子（等权，收益 32.13%）；
2. 在组合分析里导入，把权重改成 60/30/10；
3. 点「按当前权重重跑篮子」。

结果：

```
942176b6  weights [0.6, 0.3, 0.1]  return 57.91%  children 3
1c46c4de  weights [0.333, 0.333, 0.333]  return 32.13%  children 3
```

新篮子权重正确、收益显著不同（说明权重真的生效），原篮子成为可对比的历史。

### 回归

后端 **465 passed**（新增 4 项），前端 66 passed，`tsc` 与构建干净。

## 过程中被守卫测试拦下的一次

`rerun_with_weights` 里写了 `from app.core.errors import NotFoundError, ValidationError`，
而 `ValidationError` 已在模块级导入。这正是 C74 引入
`test_import_shadowing.py` 要防的模式：函数内 import 会让该名字在整个函数内
成为局部变量，方法开头的引用就会 `UnboundLocalError`。

守卫测试直接失败并指出 `modules\backtest\multi.py:313 rerun_with_weights -> ValidationError`。
这是它第三次在真实开发中拦住同类缺陷——说明这条规则值得长期保留。

## 已知限制

1. **重跑会重新执行策略**，耗时与「多标的」页一致；不是纯数值重算。
2. **不复用旧成分曲线**：权重变化会改变每份资金的建仓规模，从而改变成交价
   （滑点与手数取整），所以必须真的重跑。
3. **原篮子不被删除**，两条记录并存；用户自行决定保留哪条。
