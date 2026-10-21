# 迭代记录 C68：告警模块的依赖方向

## 动机

C64–C67 加的告警模块有三处业务代码直接 `from app.modules.alerts.service import AlertService`
并在方法内部构造：

- `risk/service.py::trip()` — 熔断
- `runtime/service.py` — 止损离场、离场失败、下单被拒
- `trading/service.py` — 网关故障

这与项目声明的耦合规则不符："业务模块之间不互相 import，通过端口与事件交互"。

## 问题

这些 import 都写在方法体内（延迟导入），能跑通，但有三个实际成本：

1. **依赖方向倒置**：alerts 是横切关注点，却是 risk/runtime/trading 依赖它
2. **无法替换**：想在测试里用 no-op 告警，必须构造真实数据库服务
3. **隐式耦合**：延迟导入让依赖在模块图上看不出来，只能靠读代码发现

## 修法

新增 `app/modules/alerts/port.py`：

```python
class AlertPort(Protocol):
    def raise_alert(self, *, category, title, message="", level="warning",
                    account_id="", ref_type="", ref_id="", commit=False) -> object: ...

class NullAlerts:
    def raise_alert(self, **_kwargs) -> None:
        return None
```

业务服务改造成接受可选的 `alerts` 参数：

```python
class RiskService:
    def __init__(self, session, *, alerts=None):
        self.alerts = alerts
    ...
    if self.alerts is not None:
        self.alerts.raise_alert(...)
```

`None` 作为默认值（而不是 `NullAlerts()`）是刻意的：调用点已经做了 `is not None`
判断，且测试里传 `None` 比构造一个对象更直白。

组合根负责注入：

```python
# app/api/wiring.py
risk=RiskService(session, alerts=AlertService(session)),
alerts=AlertService(session),
```

装配点（router、engine）同样注入。

## 改造中暴露的问题

第一次改完跑全量，5 个告警测试失败：

```
FAILED test_breaker_trip_creates_a_critical_alert
FAILED test_alert_can_be_acknowledged
...
```

原因：`/api/risk/observe` 路由自己构造 `RiskService(session)`，没传 alerts。
之前它靠方法体内的延迟导入"意外地"总是能拿到服务，改造后这个隐式路径消失，
问题才显形。

这正说明改造的价值：**它把"谁提供告警服务"从隐式变成了显式**，
漏掉注入的地方会立刻表现为测试失败，而不是在生产环境里悄悄不告警。

## 当前依赖图

```
alerts/port.py        ← 只有协议与 no-op 实现，不依赖任何业务模块
alerts/service.py     ← 依赖 alerts/models
business services     ← 依赖 alerts/port（仅类型层面，运行时是注入的实例）
api/wiring.py         ← 唯一把两者接起来的地方
```

`app/modules/risk/router.py` 等装配层直接 import `AlertService` 是合理的：
它们的工作就是组装，不是业务逻辑。

## 设计取舍

- **为什么用 Protocol 而不是抽象基类**：Python 的结构化子类型不需要继承。
  `AlertService` 不用知道自己实现了 `AlertPort`，测试里的假对象也不用。
- **为什么 `raise_alert` 返回 `object`**：调用方不该需要 alert 的 id。
  返回具体类型会诱导调用方开始对告警做分支判断——那不是业务代码该关心的。
- **为什么保留 `NullAlerts`**：目前没有地方用，但它把"不用告警"这个意图
  表达成一个具名对象，比到处传 `None` 更清晰。留给后续需要显式关闭告警的场景。

## 验证

| 检查项 | 结果 |
|---|---|
| 业务服务不再 import alerts 实现 | ✅ 仅剩 router/engine/wiring |
| 熔断、离场、网关三类告警仍产生 | ✅ 全部告警测试通过 |
| 漏注入会立刻失败 | ✅ 改造中实际发生并修复 |
| 后端全量回归 | ✅ 406 passed |
| 前端测试 | ✅ 66 passed |

## 后续

- `AlertPort` 目前只有一个方法。若将来加入"关闭告警"或"批量确认"，
  仍然只应在 port 上暴露业务方真正需要的操作。
- 事件总线（`EventBus`）是另一条可以走的路径：业务方发布 `OrderRejected`
  等事件，alerts 订阅后决定是否告警。当前直接注入更简单，
  因为告警的条件判断（级别、去重）与业务动作紧密相关，不适合完全解耦。
