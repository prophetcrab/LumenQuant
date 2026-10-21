# 测试计划

| 层级 | 工具 | 范围 | 通过标准 |
|---|---|---|---|
| 模块测试 | pytest | 指标、策略、撮合、账本、风控、回测指标 | 全绿，关键边界覆盖 |
| 集成测试 | pytest + TestClient | 模块组合与 REST 契约 | 状态码/金额/状态机符合预期 |
| 系统测试 | pytest system | 建账户→下单→持仓→策略→回测→AI 全链路 | 全链路一致，重启后数据仍在 |
| 压力测试 | httpx 并发脚本 | 行情/下单/回测/助手接口 | 无 5xx，P95 在预算内 |
| 前端检查 | tsc + vite build + 浏览器截图 | 2K 布局与交互 | 构建零错误，无溢出/遮挡 |

每次迭代执行模块 + 集成；R05/R10/R15/R19/R20 追加系统与压力测试记录。


## 前端测试

```bash
cd frontend
npm test          # vitest run，54 项
npx tsc --noEmit  # 含测试文件
npx vite build
```

覆盖：策略参数类型转换、交接解析、金额/百分比格式化与币种、回测页在 StrictMode 下的
参数预填与重置行为、实盘/模拟开户的风险提示、归档确认流程。
图表渲染本身与真实网络仍由人工截图与后端集成测试覆盖。

## 当前规模

| 层级 | 数量 |
|---|---|
| 后端单元 | 235 |
| 后端集成 | 124 |
| 后端系统（端到端） | 3 |
| 前端组件/单元 | 48 |

完整回归：

```bash
cd backend && ../.venv/Scripts/python.exe -m pytest -q   # 378 passed
cd frontend && npm test && npx tsc --noEmit && npx vite build
```
