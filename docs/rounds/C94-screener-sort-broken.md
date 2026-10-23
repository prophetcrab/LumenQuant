# 迭代记录 C94：选股的所有排序字段全部失效

## 缺陷

按成交额降序筛选，返回的顺序是 `10.2, 15.2, 4.6, 13.9, 3.4` —— 完全无序。
按市值**升序**返回的第一名是 1778 亿，而最小的是 0.8 亿。

`criteria.sort_by` 正确地回显了 `"amount"`，接口没有任何报错。

## 根因：请求字段名与数据字段名不一致

请求用读者友好的名字，行数据把单位写进了键名：

| 请求 `sort_by` | 归一化后的行字段 |
|---|---|
| `amount` | `amount_yi` |
| `turnover` | `turnover_pct` |
| `market_cap` | `market_cap_yi` |

排序代码直接拿请求名去取行字段：

```python
present = [row for row in matched if row.get(key) is not None]
```

`row.get("amount")` 恒为 `None`，于是**每一行都进了 `absent` 列表**，
而 `absent` 保持源数据顺序、不做任何排序。整张表按数据源返回顺序展示，
看起来"像是排过"只是因为源数据恰好有点规律。

`change_pct`、`pe`、`pb`、`price` 这四个字段名恰好一致，所以它们是对的——
这让问题更难发现：换个字段试一次可能就"好了"。

## 修复

显式映射，不再假设两边同名：

```python
SORT_KEYS = {
    "change_pct": "change_pct",
    "amount": "amount_yi",
    "turnover": "turnover_pct",
    "pe": "pe",
    "pb": "pb",
    "market_cap": "market_cap_yi",
    "price": "price",
}
key = SORT_KEYS[f.sort_by]
```

校验也改为 `if f.sort_by not in SORT_KEYS`，新增字段必须同时登记映射。

## 验证

一个测试逐个字段核对：构造三行已知数值，对 7 个排序字段逐一断言降序结果
（`amount` → `[3.0, 2.0, 1.0]`、`market_cap` → `[500, 300, 100]`……）。
把 `key = SORT_KEYS[f.sort_by]` 改回 `key = f.sort_by` 后测试失败。

真实市场复验：

```
amount      [279.2, 254.0, 213.6, 198.0, 129.1]   降序正确
market_cap  [29653.0, 29560.8, 29088.3, 25163.8, 23335.2]
turnover    [96.9, 64.6, 63.0, 52.8, 47.4]
pe          [6130.0, 3153.0, 2915.4, 2834.0, 2822.7]
market_cap 升序 [0.8, 0.8, 6.0, 6.1, 6.3]
```

## 为什么这个缺陷挂了这么久

C90 的测试里有一条"缺失值排序"：

```python
worst_first = svc.screen(ScreenFilter(sort_by="pe", ascending=False))
assert worst_first["candidates"][0]["code"] == "601318.SH"
```

`pe` 恰好是两边同名的字段，所以它通过了。测试选了一个**不能暴露问题**的字段，
而六个预设里只有"小盘活跃"（按 turnover 排）真正会用到不同名的字段——
预设的匹配结果不依赖顺序，因此也没暴露。

教训：验证"某个功能生效"时，要挑**能失败的输入**，而不是碰巧能通过的输入。

## 回归

后端 **523 passed**（新增 1 项覆盖全部 7 个排序字段）。

## 已知限制

无新增。排序本身是纯内存操作，修复不改变性能。
