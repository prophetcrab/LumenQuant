# API 摘要

Base URL：`http://localhost:8000/api`

## 系统
- `GET /health` — 进程、数据库状态
- `GET /api/meta` — 已启用能力

## 账户
- `GET /api/accounts?include_archived=true`
- `POST /api/accounts` — `{name, kind, initial_balance, base_currency, leverage}`
- `GET /api/accounts/{id}`
- `PATCH /api/accounts/{id}` — `{name?, status?, leverage?}`
- `POST /api/accounts/{id}/deposit` — `{amount, note?}`
- `POST /api/accounts/{id}/withdraw` — `{amount, note?}`（仅模拟账户）
- `GET /api/accounts/{id}/ledger?limit=100`

错误统一为：`{"error": {"code": "...", "message": "..."}}`。
