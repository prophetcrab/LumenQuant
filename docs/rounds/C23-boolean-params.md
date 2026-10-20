# 迭代记录 C23：布尔型策略参数的前端缺陷

## 缺陷

给策略新增了 `require_regime`（布尔开关）后，schema 里正确标注了：

```json
"require_regime": {
  "type": "boolean",
  "default": false,
  "title": "只在长期均线上方做多"
}
```

但前端三个页面的参数表单**只渲染数字输入**，并且提交前统一做：

```ts
parsed[key] = Number(value);   // Number(false) === 0, Number(true) === 1
```

后果：布尔参数被当成数字提交。后端 Pydantic 强类型校验会拒绝
（`Input should be a valid boolean`），用户看到的只是一个莫名其妙的 422。

更糟的是勾选框根本没渲染——用户无法知道这个开关存在。

## 修复

新增 `src/shared/strategy-params.ts`，集中处理参数的类型映射：

| 函数 | 作用 |
|---|---|
| `initialParams(meta)` | 按 schema 类型初始化：布尔保持 boolean，数字转字符串供输入框使用 |
| `coerceParam(schema, raw)` | 提交前按 schema 还原类型（boolean / number / string） |
| `buildParams(meta, form)` | 批量转换，三个页面共用 |

三个页面（策略库、回测、自动运行）统一改为：

- `schema.type === "boolean"` → 渲染 checkbox；
- 其他 → 渲染数字输入；
- 提交时用 `buildParams()` 而非手工 `Number()`。

另外把 `StrategyMeta.defaults` 的类型从 `number | string` 放宽为
`number | string | boolean`，避免布尔默认值被类型系统静默丢弃。

## 验证

浏览器实测（2560×1440，策略库 → EMA 趋势过滤）：

```
EMA 周期      [50]     5 – 300
ADX 周期      [14]     5 – 50
ADX 阈值      [20]     5 – 50
牛熊过滤均线  [200]    20 – 400
只在长期均线上方做多  [✓] 启用
  关闭后策略会在下跌市持续开多，回测胜率通常极低
```

接口层验证（真实数据，招商银行 800 日线）：

| 参数 | 信号数 |
|---|---|
| `require_regime: true` | 19 |
| `require_regime: false` | 32 |
| 过滤确实只减不增 | ✅ |

## 顺带统一：数据不足的提示

信号预览此前只报 `not enough candles: need 204, got 151`，
现在与回测接口一致，会说明原因与解法：

```
数据不足：仅有 151 根 K 线，需要至少 204 根；
该策略启用了牛熊过滤（100 日均线），可增大 limit 或设置 require_regime=false
```

## 新增测试（2 项）

1. `test_boolean_strategy_param_is_accepted`
   - 布尔参数经 HTTP 往返后仍为 boolean；
   - 开启过滤的信号数 ≤ 关闭时（过滤只能减少入场）。
2. `test_catalog_exposes_boolean_param_type`
   - 目录里 `require_regime` 的 `type` 必须是 `"boolean"`。

## 全量测试

**247 passed**。

## 教训

**动态表单必须按 schema 的 `type` 分派控件，不能假设参数都是数字。**
一处 `Number(value)` 就把新增的参数类型能力全部抹掉了，
而且失败点离根因很远（用户看到的是后端 422）。
这类"类型在传输层丢失"的问题，最好在前端入口统一处理一次，
而不是在每个页面各写一套转换逻辑。
