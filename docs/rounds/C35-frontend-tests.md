# 迭代记录 C35：前端自动化测试

## 动机

交付文档里最后一条未完成项是"前端自动化测试（Playwright）"。此前的所有前端验证
都是**人工在浏览器里点**——C29 那个"参数被默认值覆盖"的缺陷，正是因为手动验证时
只看了标的和周期、没回头看参数才漏掉，第二次专门查才发现。

人工验证的问题不是不准，而是**不会重复**。每轮改完都要重新点一遍，
漏掉一次就回归了。

## 选择：Vitest + Testing Library，不是 Playwright

先说明为什么不是文档里原来写的 Playwright。

| 方案 | 适合 | 代价 |
|---|---|---|
| Playwright | 真实浏览器、跨页面、拖拽、Canvas | 要起前后端、要浏览器二进制、慢、CI 脆 |
| Vitest + RTL | 组件逻辑、状态流、格式化、参数解析 | 不覆盖真实渲染与网络 |

这一轮的目标是**把已经修过的 bug 钉住**，这些 bug 全部是组件内部的状态与类型问题：
参数类型、StrictMode 双执行、格式化符号。Playwright 在这里只会更慢而不会更准。

真实渲染和完整链路仍由既有的浏览器检查覆盖（每轮手动截图 + 真实数据链路验证）。

## 基础设施

```
vitest.config.mts   jsdom 环境，include src/**/*.test.{ts,tsx}
src/test/setup.ts   每个用例后 cleanup，避免 DOM 残留导致查询歧义
package.json        npm test → vitest run
```

`vitest.config.mts` 与 `vite.config.ts` 分开：应用配置里的开发代理和分包策略
对 jsdom 测试没有意义。

## 抽取：交接解析成为可测的纯函数

回测页和自动运行页原来各自内联了一段 25 行的 URL 交接解析，逻辑相同但复制了两份。
复制是刻意的（低耦合原则允许），但它同时意味着**两份都可能出错且只修一份**。

抽成 `shared/handoff.ts`：

```ts
parseStrategyHandoff(searchParams, allowedTimeframes) -> StrategyHandoff | null
```

行为在抽取时明确了几条原来隐含的约定：

- 没有 `strategy` 返回 `null`（区分"没交接"和"交接了空参数"）
- JSON 损坏退化为空参数，而不是抛异常——坏链接应该打开默认页，不是白屏
- JSON 不是对象（数组、数字、`null`）同样退化为空
- 不在允许列表里的周期被丢弃，否则 `<select>` 会显示一个不存在的选项

## 覆盖的测试（37 项）

### `shared/strategy-params.test.ts`（10）

参数类型转换。核心回归：`Number(false) === 0`，所以布尔参数曾经以数字 0
到达后端并触发 schema 校验失败。

### `shared/format.test.ts`（14）

货币、百分比、量价格式化。包括 `currencySymbol` 对未知币种返回空串而不是猜 `$`——
猜错会让金额看起来是另一个币种。

### `shared/handoff.test.ts`（8）

交接解析。覆盖每种字段缺失、非法 JSON、非对象 JSON、不支持周期。

### `features/backtest/BacktestPage.test.tsx`（5）

组件级，包在 `<StrictMode>` 里渲染：

1. URL 里的策略/标的/周期/参数全部预填，缺失字段由 schema 默认值补齐
2. **StrictMode 双执行后参数仍是交接值**——C29 的原始 bug
3. 交接后 query 被清除（刷新不会盖回旧值）
4. 无交接时回落到默认策略
5. 用户切换策略时参数重置为新策略的默认值，而不是保留旧策略的字段

第 2 项是关键：它把"effect 必须幂等"这个约束变成了可执行断言，
而不是留在代码注释里。

## 验证

| 检查项 | 结果 |
|---|---|
| `npm test` | ✅ 37 passed，4 个文件，约 2 秒 |
| `npx tsc --noEmit`（含测试文件） | ✅ |
| `npx vite build` | ✅ |
| 后端全量回归 | ✅ 335 passed |

## 已知限制

- 不覆盖真实网络与真实渲染。API mock 意味着后端契约变化不会被这些测试发现——
  这由后端集成测试与每轮的真实数据链路验证覆盖。
- 未覆盖 Canvas/图表。ECharts 在测试里被替换为 `null`，图表正确性靠人工截图确认。
- 未做视觉回归（像素比对）。2K 布局由每轮截图人工核对。

## 后续

- 若要覆盖跨页面完整流程（策略库 → 回测 → 自动运行），Playwright 仍然是正确工具；
  但它需要稳定的后端 fixture，建议单独作为一轮。
- 现有 mock 策略列表可以抽成共享 fixture，供后续新增的页面测试复用。
