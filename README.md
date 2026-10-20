# LumenQuant

面向个人研究者的自动化交易平台：行情、模拟/实盘账户、策略运行、回测、组合分析与 AI 助手。

- 后端：Python 3.13 / FastAPI / SQLAlchemy / SQLite（可替换 PostgreSQL）
- 前端：React 19 / TypeScript / Vite / ECharts，面向 2560×1440 浅色高密度界面
- 架构：模块化单体，模块只通过服务接口、领域事件与只读端口通信
- 默认安全：模拟交易开箱即用；实盘需显式开启并提供交易所密钥

## 快速启动

```bash
# 后端
cd backend
../.venv/Scripts/python.exe -m uvicorn app.main:app --reload --port 8000

# 前端
cd frontend
npm install
npm run dev
```

打开 <http://localhost:5173>，API 文档 <http://localhost:8000/docs>。

## 测试

```bash
cd backend
../.venv/Scripts/python.exe -m pytest -q
../.venv/Scripts/python.exe -m pytest -m stress -q
```

## 文档

- 思路文档：[docs/thinking/architecture.md](docs/thinking/architecture.md)、[docs/thinking/roadmap.md](docs/thinking/roadmap.md)
- 交付文档：[docs/delivery/](docs/delivery/)
- 迭代记录（20 轮）：[docs/rounds/](docs/rounds/)

> 风险提示：本项目用于研究与教学，不构成投资建议。实盘交易可能造成资金损失。
