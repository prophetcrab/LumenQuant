# 迭代记录 C87：`prev_close` 在跨日后从不更新

## 缺陷

A 股的涨跌停区间以 `prev_close`（上一交易日收盘）为基准。合成引擎中这个值：

- `seed()` 结束时调用一次 `_sync_prev_close()` 写入；
- 之后**再也没有更新**。

`refresh_prev_close()` 方法存在，但全仓库没有任何调用点；它负责维护的
`_last_bar_day` 字段也从未被初始化或读取——是一段完整的死代码。

### 后果

跨过 seed 日期之后，每个交易日的 ±10%（或 ±20%）区间都在拿**几天前**的
收盘价计算。因此：

- 一个已经涨了 15% 的股票，界面仍显示"距涨停还有 5%"，而实际早已触及；
- 反过来，一个跌了不少的标的可能一直显示"接近跌停"，实际离得很远。

这不影响成交价，但影响**所有基于涨跌停的判断**：界面状态徽章、
`at_or_above(price, band.up)` 的买入拦截、`at_or_below(price, band.down)` 的卖出拦截。

## 修复

在 `tick()` 里检测交易日切换：

```python
if self._last_bar_day != current_day:
    self._roll_prev_close(session, rows, current_day)
    self._last_bar_day = current_day
```

`_roll_prev_close` 取**严格早于今天**的最后一根日线收盘：

```python
last = session.scalar(
    select(Candle.close).where(
        Candle.symbol == row.code,
        Candle.timeframe == "1d",
        Candle.open_time < day_start,   # 关键：排除今天
    ).order_by(Candle.open_time.desc()).limit(1)
)
```

排除今天是必须的：当日日线由盘中 15m 收盘持续覆盖更新（`upsert_daily_bars`），
"最新日线收盘"是一个还在变动的值。若用它，涨跌停区间会随盘中价格一起漂移，
本身就失去意义。

同时把 `_last_bar_day` 在 `__init__` 里初始化为 `None`——它此前的缺失正是
这段逻辑从未接线的痕迹。

## 验证

| 用例 | 断言 |
|---|---|
| 跨日滚动 | 前一交易日收盘改为 1500 后期 `prev_close` 变为 1500（内存缓存同步） |
| 忽略当日部分 bar | 今日写入一根 1900 的未完成日线，`prev_close` 不得变成 1900 |

第一个测试还原（让 `_roll_prev_close` 直接 return）后失败。

真实数据核对：seed 后 `prev_close == 最新日线收盘`（1631.6）；
随后把前一交易日收盘手工改为 1500 再调用滚动，`prev_close` 精确变为 1500。

## 回归

后端 **493 passed**（新增 2 项）。

## 已知限制

1. **只在合成引擎内修复**：真实数据模式下 `prev_close` 由腾讯报价刷新
   （`_persist_prev_close`，C74 已实现），不受此缺陷影响。
2. **依赖 15m→日线滚动**：若某日完全没有收盘 bar（全天停牌），该日不会
   产生日线，`prev_close` 会沿用再前一个交易日的收盘——这与交易所口径一致
   （停牌日不产生新的收盘价）。
3. **跨多日一次性跳转**：60x 模拟时钟可能一次跳过多个交易日，此时取"今天
   之前最后一根日线"，语义仍然正确。
