# 迭代记录 C37：信号预览与回测的一致性

## 动机

在真实 A 股数据上把 9 个策略全部横扫一遍，对比"信号预览"和"回测成交"。
两个入口描述的是同一个策略、同一个标的、同一段数据，数字应该能对上。

## 缺陷：预览显示 799 个买入，回测只成交 1 笔

```
strategy            回测成交   预览信号
buy_and_hold            1       799
donchian_breakout      14       142
atr_channel            10        67
stochastic_reversal    19       104
```

`buy_and_hold` 最刺眼：800 根 K 线上 799 个"买入"标记，图上会糊成一片绿色三角。

### 根因

策略层和交易层职责划分清楚：**策略只回答"我想不想持有"，由持仓状态决定是否真下单。**
`buy_and_hold` 的注释写得很明确：

```python
# Emit on every bar from warmup onward rather than only at index==1:
# the runtime evaluates just the latest bar, so a "fire once at the start"
# signal would never reach a live/paper instance.
```

这个设计是对的。回测引擎在收到 `buy` 时检查 `position is None`，运行时检查 `position <= 0`，
两者都正确地只成交一次。

**但信号预览把这个过滤完全跳过了**——它是裸的 `strategy.evaluate()` 输出，
所以每个"我想持有"的确认都变成一个买入标记。

### 后果

预览页是用户调参数的地方。看到一个策略在 800 根 K 线上发出 799 个买入信号，
合理的理解是"这个策略在疯狂交易"，而实际它只交易一次。用户会据此否掉一个好策略，
或者对交易频率产生完全错误的预期。

## 修法

在预览里复现执行层的持仓状态机：

```python
in_position = False
suppressed = 0
for index in range(strategy.warmup(parsed), len(candles)):
    signal = strategy.evaluate(state, index)
    if not signal.is_actionable:
        continue
    if (signal.action == "buy" and in_position) or (signal.action == "sell" and not in_position):
        suppressed += 1
        continue
    in_position = signal.action == "buy"
    points.append(...)
```

同时返回 `suppressed_signals`，前端在标题里说明：

> 1 个可执行信号（另有 144 个与当前持仓状态重复，已忽略）

这样"策略触发了 145 次但图上只有 1 个标记"是**可解释**的，而不是看起来像丢数据。

## 修完后的真实数据对比

```
strategy            回测成交   可执行信号   抑制
buy_and_hold            1          1        798
atr_channel            10         19         48
donchian_breakout      14         27        115
rsi_reversion           3          6         19
bollinger_reversion    12         24         39
stochastic_reversal    19         38         66
macd_momentum          36         71          0
ema_trend              10         19         25
sma_cross              15         31          1
```

预览数与回测成交数的关系符合预期：

- `macd_momentum` 抑制 0——它的信号天然交替，从不重复
- `sma_cross` 抑制 1——偶尔出现连续同向
- `buy_and_hold` 抑制 798——每根都喊买，只有第一次算数
- 其余策略的可执行信号数 ≈ 成交数 × 2（一进一出），差异来自回测的成交延迟、涨跌停拦截与 T+1

## 设计取舍

- **为什么在服务端做，不放在前端**：持仓状态是执行语义，不是展示细节。
  任何消费这个接口的地方（未来的移动端、脚本）都应该看到同一个答案。
- **为什么不干脆改策略、让它只在入场时发信号**：`buy_and_hold` 必须每根都发，
  否则运行时的"只看最新一根"逻辑永远收不到信号。这是策略接口的契约，
  预览应该适配它，而不是反过来改策略。
- **为什么保留 `suppressed_signals`**：只显示 1 个信号会让人怀疑"是不是只算了一根"。
  把抑制数摆出来，数字自洽。

## 验证

| 检查项 | 结果 |
|---|---|
| buy_and_hold 预览只返回 1 个买入信号 | ✅ 新增测试 |
| 抑制数与实际重复次数一致 | ✅ |
| 多空交替策略的预览严格 buy/sell 交替 | ✅ 新增测试 |
| 真实数据 9 策略横扫：预览与成交语义一致 | ✅ |
| 前端标题显示抑制数 | ✅ 浏览器确认 |
| 前端类型检查 / 构建 / 37 项测试 | ✅ |
| 后端全量回归 | ✅ 343 passed（新增 2 项） |

## 后续

- 预览仍不模拟涨跌停、T+1、资金约束，所以"可执行信号"不等于"回测成交"。
  两者相差 2 倍关系（进出场配对）符合预期；若要更精确，预览需要引入完整的执行模拟，
  那基本等于再跑一次回测，收益不足以抵消复杂度。
- 横扫中发现所有 9 个策略在招商银行日线上样本外表现都很弱（见 C36）。
  这是市场特性，不是缺陷。
