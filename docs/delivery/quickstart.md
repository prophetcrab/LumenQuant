# 快速开始

## 环境要求
- Windows 10/11（项目在 2K 2560×1440 下设计，亦可在 1080p 使用）
- Python 3.13+、Node.js 20+
- 无需外网行情；模拟行情在本地合成

## 一键启动

```powershell
cd D:\LumenQuant
powershell -ExecutionPolicy Bypass -File .\scripts\start-all.ps1
```

- 前端：<http://127.0.0.1:5173>
- 后端文档：<http://127.0.0.1:8000/docs>
- 健康检查：<http://127.0.0.1:8000/health>

## 手动启动

```powershell
# 后端
cd D:\LumenQuant\backend
..\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 前端（另开一个窗口）
cd D:\LumenQuant\frontend
npm run dev
```

首次运行会自动创建 `data\lumenquant.db`，播种 6 个标的各 11,520 根 15 分钟 K 线，
并启动四个后台引擎：行情模拟、撮合、策略调度、组合快照。

## 第一次使用
1. 打开总览页 → “创建模拟账户”。
2. 在交易页下第一笔市价单，确认持仓、成交、账本同步。
3. 到策略库挑选策略，调好参数后可直接「带去回测」或「建成自动实例」，无需重填。
4. 在自动运行页新建实例并启动，等待下一根已收盘 K 线触发。
5. 组合页查看权益、回撤、风控状态与盈亏归因；「风控」页集中查看敞口与止损保护。
6. 回测页的「组合分析」可把多个回测合并，看相关性与分散效果。
7. AI 助手可直接提问：“我的持仓怎么样”“我现在能开仓吗”“回测 MACD 最近 800 根”；
   未指定标的时按当前账户市场选择基准标的（A 股 600036.SH / 加密 BTCUSDT）。

## 配置实盘（默认关闭）

1. 复制 `backend\.env.example` 为 `backend\.env`。
2. 设置 `LQ_LIVE_TRADING_ENABLED=true`、`LQ_BINANCE_API_KEY`、`LQ_BINANCE_API_SECRET`。
3. 重启后端，`GET /api/brokers/status` 应显示 `ready: true`。
4. 在「账户」页新建账户时把类型选为「实盘」，手动下单请求必须带 `X-Live-Confirm: true`。

若要让**策略实例自动下实盘单**，还需要额外开启：

```env
LQ_LIVE_AUTO_TRADING_ENABLED=true
```

这个开关每次调度都会复查：关闭后已存在的实例立即停止开仓，
但止损等保护性离场不受影响——禁止止损会让实盘持仓失去保护。

> 实盘账户的本地现金与持仓由成交回写维护；使用前请先用 `/api/brokers/binance/verify`
> 核对交易所余额。密钥只从环境读取，不写入数据库。

## 配置认证

```env
LQ_AUTH_ENABLED=true
LQ_AUTH_TOKEN=<随机长字符串>
```

开启后除 `/health` 与文档外的所有接口都需要 `Authorization: Bearer <token>`。

## 测试

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run-tests.ps1
```

或分别执行：

```powershell
cd D:\LumenQuant\backend
..\.venv\Scripts\python.exe -m pytest -q             # 378 项
```

前端：

```powershell
cd D:\LumenQuant\frontend
npm test                # Vitest，54 项
npx tsc --noEmit        # 含测试文件
npx vite build
```

压测脚本见 `backend\tests\stress\README.md`（需先以放大的限流阈值启动服务）。

## 数据位置
- 数据库：`data\lumenquant.db`（SQLite WAL）
- 截图与验收记录：`docs\reviews\`
- 导入 CSV：数据页上传，格式 `open_time,open,high,low,close[,volume]`，时间戳为毫秒
