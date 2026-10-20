# 迭代记录 C13：实盘网关单例与成交回报验证

## 缺陷：网关每请求新建，实盘回报永远拿不到订单

`app/api/wiring.py` 里写着：

```python
cn_broker = getattr(request.app.state, "cn_gateway", None) or build_ths_gateway(...)
```

看起来是"优先用共享实例，否则新建"。但 `app.state.cn_gateway` **从未被赋值**，
所以每次请求都走 `build_ths_gateway()` 新建一个对象。

后果（对 Mock 网关尤其致命）：

1. 请求 A 下单 → 网关 A 接受，返回 `MOCK-xxx`；
2. 后台 `LiveReconciler` 用**另一个**网关实例轮询 → 查不到该订单；
3. 成交永远回写不到本地，委托永远停在 `open`。

对真实 HTTP 网关影响小些（订单存在交易所侧），但每次请求新建客户端仍然浪费连接，
并且丢失本地会话状态（如登录态、限流计数）。

## 修复

在 `create_app()` 中构造**一个**网关实例：

```python
cn_gateway = build_ths_gateway(settings)
...
reconcile_engine = LiveReconciler(db, settings, gateway=cn_gateway)
...
app.state.cn_gateway = cn_gateway
```

下单路径（`wiring.build_trading`）与对账路径（`LiveReconciler`）现在共享同一实例。

## 新增集成测试（3 项）

| 测试 | 断言 |
|---|---|
| `test_gateway_is_a_process_singleton` | `app.state.cn_gateway` 存在且多次读取同一对象 |
| `test_live_order_is_visible_to_the_gateway` | 下单返回的 `broker_order_id` 能被同一网关查到 |
| `test_reconciler_sees_gateway_fills` | 网关回报成交 → 对账器写回持仓（T+1 冻结）→ 订单变 `filled` → 二次轮询不重复记账 |

第三条是端到端的：它同时验证了
**下单 → 网关受理 → 网关成交 → 对账器增量回写 → 幂等** 的完整链路。

## 验证

| 检查项 | 结果 |
|---|---|
| 网关实例唯一 | ✅ |
| 实盘订单可见性 | ✅ |
| 成交回写持仓 | ✅ quantity=200, available=0（T+1） |
| 幂等 | ✅ 二次轮询数量不变 |
| 全量测试 | **216 passed** |

## 教训

`getattr(state, "x", None) or build_x()` 这种写法很危险：
**它把"配置缺失"和"实例未创建"混为一谈**，而且默认分支会静默成功，
让缺陷看起来像正常工作。

更好的做法是显式区分：

- 配置未启用 → 返回 `None`，调用方给出明确错误；
- 配置已启用 → 必须在启动时创建并存到 `app.state`，调用方只读取，
  读到 `None` 就是装配错误，应当直接失败。

本项目已改为后者：`cn_gateway` 在 `create_app` 里一次性构建并写入 `state`。
