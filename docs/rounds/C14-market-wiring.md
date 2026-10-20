# 迭代记录 C14：行情装配一致性

## 背景

C13 发现"网关实例未共享"，我顺着 `getattr(app.state, ...)` 做了一次全面排查，
重点看**行情服务是否都拿到了支持双市场的 provider**。

## 缺陷：策略路由绕过复合 provider

`app/modules/strategies/router.py` 是唯一还在自己装配行情的地方：

```python
engine = getattr(request.app.state, "market_engine", None)   # 只有加密引擎
market = MarketService(session, clock=..., live=engine)
```

后果：A 股标的的策略信号预览拿不到实时 bar，
`closed_only`、涨跌停区间等信息全部走不到正确的数据源。

修复为统一走 `build_market(request, session)`（复合 provider），
与其他 12 个路由保持一致。

### 新增测试

`test_cn_signal_preview_uses_live_provider`：对 A 股标的请求信号预览，
断言返回 200 且叠加序列长度一致——证明它确实拿到了数据。

## 缺陷：复合 provider 缺少 clock

三个后台引擎（撮合、策略调度、组合快照）都用：

```python
MarketService(session, clock=getattr(self.market_engine, "clock", None), ...)
```

`CompositeLiveProvider` 没有 `clock` 属性，`getattr` 兜底返回 `None`。
虽然不会崩溃，但 `MarketService.clock` 为空，任何依赖时钟的逻辑都会静默失效。

修复：为 `CompositeLiveProvider` 增加 `clock` 属性代理，优先返回加密引擎时钟
（连续推进）；需要北京时间会话时钟的调用方仍通过 `cn.clock` 或
`/api/cn/session` 获取。

## 排查结论

| 位置 | 装配方式 | 状态 |
|---|---|---|
| accounts / market / trading / backtest / portfolio / runtime / assistant / brokers / risk / system / data / cn | `build_market()` 或显式复合 provider | ✅ |
| strategies | 曾自建，已改为 `build_market()` | ✅ 已修 |
| 三个后台引擎 | 由 `main` 注入复合 provider | ✅ |
| `CompositeLiveProvider.clock` | 曾缺失 | ✅ 已补 |

## 验证

| 检查项 | 结果 |
|---|---|
| A 股信号预览 | 200，叠加序列对齐 |
| 复合 provider 暴露 `clock`/`is_locked`/`prev_close` | ✅ |
| 全量测试 | **217 passed** |

## 教训

"模块化" 的敌人不是重复代码，而是**同类职责有多个装配入口**。
13 个路由里 12 个用统一装配，剩下 1 个自建——正是这一个漏掉了新能力。

这也是项目最初架构文档里"允许重复代码换清晰边界"没有覆盖到的情况：
重复的是**装配代码**，而装配一旦重复就会漂移。

更稳的做法：路由层不装配服务，由 `api/wiring.py` 统一提供；
路由只做参数解析与响应序列化。本轮已让 strategies 迁移到该模式，
后续新增路由应遵循同一规则。
