# 测试环境与纪律

## 命令

```bash
cd backend && .venv/bin/python -m pytest tests/ -q        # 全量（~1871 例，~7 分钟）
cd backend && .venv/bin/python -m pytest tests/domains/test_xxx.py -q   # 单文件
cd frontend && npx vue-tsc --noEmit                        # 前端类型检查
```

## 关键事实

- **必须用 `backend/.venv`（uv 管理）**；旧的 `backend/venv` 是 7 月遗物，starlette/httpx2 不齐会在收集期报错。
- 测试库需要本机 Docker（chatbi-postgres 容器）；5432 拒连先 `open -a Docker`。
- 测试库并发互踩已根治（advisory lock fail-fast）；僵尸 mock 进程是慢测隐形元凶。
- 后端测试用真 PG（conftest 每测试 TRUNCATE 业务表）；专用 pytest 库与运行库分离（见 backend/.env 注释）。

## 纪律

- 批量改动：先读签名/调用方 → 改 → **即跑该文件测试**（验证后置曾致 31 测试连锁失败）。
- 新功能交付前跑全量；UI 改动浏览器实测。
- 本地起服务：`bash start_dev.sh --backend-only`（后端 18080）+ `cd frontend && npm run dev`（13080，代理已配）。
