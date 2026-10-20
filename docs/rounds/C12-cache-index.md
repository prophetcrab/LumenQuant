# 迭代记录 C12：统计缓存与索引补建

## 背景

C11 巡检发现两个全表统计接口偏慢：

- `/api/data/coverage`：186ms
- `/api/data/quality`：177ms

并且系统页的"最慢接口"榜长期被它们占据，掩盖了真正的热点。

## 改动

### 1. 统计接口缓存（30 秒）

新增 `app.state.stats_cache`（`TTLCache(ttl=30s)`），覆盖：

- `GET /api/data/coverage`：全表按标的聚合
- `GET /api/data/quality?symbol=&timeframe=`：单标的全量 OHLC 校验

数据覆盖率与质量不会秒级变化，30 秒足够新鲜。

实测：

| 接口 | 冷 | 热 | 热 |
|---|---|---|---|
| coverage | 60.7ms | 24.0ms | **2.9ms** |
| quality | 162.5ms | 8.0ms | **2.3ms** |

（首次请求已含进程预热，所以冷值低于 C11 记录。）

### 2. 索引补建的通用修复（重要）

C11 之前声明的两个索引实际**从未生效**：

- `ix_candles_lookup (symbol, timeframe, open_time)`
- `ix_audit_action_time (action, created_at)`

原因：`Base.metadata.create_all()` **不会**给已存在的表补建索引。
项目最早的数据库在索引声明之前就建好了表，之后每次启动都跳过。

这与 C1 的迁移缺失是同一类问题，但更隐蔽——索引缺失不报错，
只是查询慢，而且 `duckdb`/SQLite 的 EXPLAIN 不会提醒你"少了索引"。

修复：`Database._apply_light_migrations()` 末尾增加：

```python
for table in Base.metadata.tables.values():
    for index in table.indexes:
        index.create(self.engine, checkfirst=True)
```

`checkfirst=True` 保证幂等。补建后确认：

```
audit_logs indexes: autoindex, ix_created_at, ix_action, ix_audit_action_time
market_candles indexes: autoindex, ix_open_time, ix_candles_lookup, ix_symbol
```

### 3. 指标页暴露

`/api/system/metrics` 的 `caches` 现在包含 `market`、`candles`、`stats` 三项，
可以在系统页直接看到命中率。

## 验证

| 检查项 | 结果 |
|---|---|
| 缓存冷热延迟 | 见上表，热命中约 2–3ms |
| 索引实际存在 | 已在 SQLite 元数据中确认 |
| 全量测试 | **211 passed** |

## 教训

**DDL 的"声明"和"生效"是两件事。** 模型里写了 `Index(...)` 不等于库里存在索引。
凡是依赖 DDL 的优化（索引、约束、默认值），都必须：

1. 在启动时校验实际结构；
2. 或提供一次性迁移脚本并记录已执行版本。

本项目采用的是"启动时幂等补齐"，对本规模足够；再大就需要 Alembic。
