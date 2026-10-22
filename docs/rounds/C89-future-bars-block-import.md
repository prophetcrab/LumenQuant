# 迭代记录 C89：切到真实数据后，加密历史从未被导入

## 发现过程

按用户要求启动服务、打开界面检查时，行情页的 K 线横轴显示 **02/08–02/16**，
而当天是 2026-10-22。进一步查数据库：

```
BTCUSDT  15m: n=22896  2026-06-22 -> 2027-02-15
ETHUSDT  15m: n=22896  2026-06-22 -> 2027-02-15
```

两万多根 15m、时间横跨到**四个月后**——全部是合成引擎用 60x 加速时钟
生成的历史，C74 的实时导入从未替换它们。

## 缺陷：未来时间戳被判定为"最新"

`sync_history` 有一条"已最新就跳过"的优化（C74 加入，避免每次启动重下数据）：

```python
def _history_is_fresh(self, code, max_age_ms):
    latest = <max(open_time)>
    return self.now_ms() - int(latest) <= max_age_ms
```

当 `latest` 落在未来时，`now - latest` 是**负数**，而 `负数 <= 正数` 恒为真。
于是：

- 合成引擎的加速时钟写出的未来 bar 被当成"最新数据"；
- `sync_history` 对每个标的都 `continue`，启动日志里连一行 "imported" 都没有；
- 真实报价和未来日期的假历史同时存在，行情图横轴直接跑到明年。

这是一个只在**从合成模式切到真实模式**时触发的缺陷。全新部署不会有旧合成数据，
所以此前所有测试和验证都没碰到——它们用的都是干净数据库。

## 修复

```python
age = self.now_ms() - int(latest)
if age < 0:
    # 未来时间戳只可能来自合成引擎的加速时钟，不是交易所数据
    return False
return age <= max_age_ms
```

## 验证

测试 `test_future_dated_bars_are_not_treated_as_fresh` 写入一根 90 天后的 bar，
断言 `_history_is_fresh` 返回 False。还原 `if age < 0` 分支后该测试失败。

真实运行（同一个库，重启后）：

```
imported 2000 real bars for BTCUSDT (1000 daily, 1000 intraday)
imported 2000 real bars for ETHUSDT ...
（6 个标的各 2000 根）
```

数据范围恢复正常：

```
BTCUSDT  15m: n=1000  2026-10-11 -> 2026-10-22
BTCUSDT  1d:  n=1000  2024-01-27 -> 2026-10-22
```

界面横轴变为 10/18–10/22，成交量柱也从"几乎空白"（合成量级）变成真实起伏。

## 顺带修掉的第二个问题：测试会连外网

复现时发现全量测试从约 2.5 分钟变成**跑不完**。原因是我新建的 `.env` 里
写了 `LQ_CN_DATA_SOURCE=tencent` + `LQ_CN_SYNC_ON_START=true`，而
`conftest.py` 的 `Settings(...)` 会读取项目 `.env`：

```python
settings = Settings(seed_market=False, cn_symbols_enabled=False, ...)
# cn_data_source / cn_sync_on_start 未指定 → 从 .env 继承
```

于是每个 `create_app()` 都在测试里去同步真实的腾讯/Binance 数据。

### 修复

`conftest.py` 顶部强制离线默认值（`os.environ.setdefault`），并在 `settings`
fixture 里显式写出 `cn_data_source="synthetic"` 等字段，不再依赖继承：

```python
_OFFLINE_DEFAULTS = {
    "LQ_CN_DATA_SOURCE": "synthetic",
    "LQ_CRYPTO_DATA_SOURCE": "synthetic",
    "LQ_CN_SYNC_ON_START": "false",
    ...
}
for _key, _value in _OFFLINE_DEFAULTS.items():
    os.environ.setdefault(_key, _value)
```

### 验证

在 `.env` 明确配置 `tencent` + `binance` + `sync_on_start=true` 的前提下：

- 单元测试 18 秒跑完（此前会挂起）；
- 全量 **496 passed**，2.5 分钟。

## 回归

后端 **496 passed**（新增 1 项）。

## 已知限制

1. **只处理"未来时间戳"**：若库里的旧合成数据落在过去（例如上次运行在很久以前），
   `age` 会是一个很大的正数，`_history_is_fresh` 同样返回 False，导入会正常触发。
   两种情况都已被覆盖。
2. **不校验价格量级**：合成数据与真实数据的价差（如茅台 1680 vs 1310）本可作为
   额外信号，但导入时的整表替换已经足够，加一层启发式判断反而容易误伤。
3. **`.env` 仍会被生产代码读取**：这是有意的（真实部署就该这样配），
   隔离只加在测试入口。
