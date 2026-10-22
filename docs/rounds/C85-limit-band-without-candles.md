# 迭代记录 C85：没有行情数据时，涨跌停价一起变成 0

## 排查过程

排查 A 股涨跌停在回测与实时两条路径上的口径是否一致。先用一个真实的使用场景
构造数据：切换数据源到腾讯、同步尚未完成，此时标的已登记但一根 K 线都没有。

```
000001.SZ board=main  limit_pct=0.1   up=0.0    down=0.0
300750.SZ board=gem   limit_pct=0.2   up=0.0    down=0.0
688981.SH board=star  limit_pct=0.2   up=0.0    down=0.0
```

`limit_pct`（板块限幅）是对的，但 `limit_up` / `limit_down` 全是 0。

## 缺陷

`_cn_snapshot` 在拿不到任何价格时直接抛异常：

```python
price = quote.price if quote else (current.close if current else (daily[-1].close if daily else 0.0))
if price <= 0:
    raise NotFoundError(f"no price for {row.code}")
```

`/api/cn/symbols` 捕获所有异常后把整行填 0：

```python
except Exception:
    snap = None
...
"limit_up": snap.limit_up if snap else 0.0,
```

问题在于：**涨跌停并不依赖当前价格**。它由 `prev_close`（登记数据）和
板块规则算出，这两者都已经在库里。把整个快照作废，连带把可以确定的信息
一起丢了。

界面后果：标的列表显示「涨停 0.00 / 跌停 0.00」，用户看到的是一支
"没有涨跌停限制"的股票。若在此状态下下单，价格校验也失去了参考。

## 修复

价格缺失时回落到 `prev_close`，并照常计算 band：

```python
if price <= 0:
    if prev_close > 0:
        band = price_band(prev_close, row.board, row.is_st, row.tick_size or 0.01)
        return Snapshot(price=prev_close, change_24h_pct=0.0,
                        high_24h=prev_close, low_24h=prev_close, volume_24h=0.0,
                        time=0, ..., limit_up=band.up, limit_down=band.down)
    raise NotFoundError(...)
```

`change_24h_pct=0.0`、`volume_24h=0.0`、`time=0` 都是明确表示"还没有成交"，
而不是猜测一个数字。价格用 `prev_close` 是唯一有依据的值——它就是
交易所的昨收，紧接着的涨跌停区间正是以它为基准。

## 验证

| 用例 | 断言 |
|---|---|
| 无 K 线的主板标的 | price=1680、涨停 1848、跌停 1512（±10%） |
| 无 K 线的创业板标的 | 涨停 240、跌停 160（±20%，不是硬编码 10%） |
| 没有伪造数据 | 快照算完后 `market_candles` 仍为空 |

还原 `raise NotFoundError(...)` 后两个测试都失败。

修复后实测：

```
000001.SZ price= 11.3 up= 12.43 down= 10.17 prev=11.3
000858.SZ price=142.0 up= 156.2 down=127.8 prev=142.0
300750.SZ price=182.0 up= 218.4 down=145.6 prev=182.0   ← 创业板 ±20%
```

## 回归

后端 **490 passed**（新增 2 项）。

## 已知限制

1. **价格是昨收不是现价**：数据源接通前的这一小段时间里，界面上的"现价"
   实际是昨收，`change_24h_pct` 恒为 0。同步完成后自动纠正。
2. **不覆盖 `prev_close=0` 的情况**：全新手工添加、又没给昨收的标的仍会
   抛 NotFound。这种情况下确实无从推算，报错比编一个数字更诚实。
3. **只修了 A 股**：加密快照的 24h 窗口依赖真实成交，没有可回落的参考价。
