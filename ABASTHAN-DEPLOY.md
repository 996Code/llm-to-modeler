# Abasthan 部署说明（abasthan-deploy 分支）

本分支在 main 基础上追加了一个约 16 行的 env 门控补丁（`backend/src/main.py`）：
设置环境变量 `FRONTEND_DIST_DIR` 指向 vite 构建产物目录时，FastAPI 会同源挂载
`/ai-modeler/*` 静态前端，并将 `/` 重定向到 `/ai-modeler/`；不设置时行为与
原版完全一致。

## 前置条件（重要）

**该应用强制要求 PostgreSQL + pgvector 的 `DATABASE_URL` 才能启动（fail-closed）。**
Abasthan 目前没有数据库服务（控制台显示 Soon），因此必须由用户提供一个外部
PostgreSQL（含 pgvector 扩展）连接串，并加入环境变量
`DATABASE_URL=postgresql://user:password@host:port/dbname`
后，应用才能启动成功。缺少该变量时应用会直接退出，属于预期行为。

## Abasthan Web Service 配置

| 配置项 | 值 |
| --- | --- |
| 运行时 | Python 3.12 |
| Root Directory | 留空（仓库根目录） |
| Build Command | `pip install -r backend/requirements.txt` |
| Start Command | `python -m uvicorn main:app --host 0.0.0.0 --port $PORT` |

注意：Start Command 中的 `main:app` 相对 `backend/src/main.py`，需保证 uvicorn
的工作目录能解析到该模块（按 Abasthan 实际工作目录行为调整，必要时改为
`python -m uvicorn backend.src.main:app --factory` 或在 start 命令前 cd）。

## 必需环境变量

| 变量 | 值 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql://user:password@host:port/dbname` | 外部 PG + pgvector 连接串，**必需，否则无法启动** |
| `FRONTEND_DIST_DIR` | `frontend/dist` | 开启 FastAPI 同源托管前端 |
| `PORT` | `3000` | 服务监听端口 |
| `BIND` | `0.0.0.0` | 监听地址 |
| `PACKS_ENABLED` | `njmind_form,njmind_list,njmind_bpm,metrology_assistant,web_search` | 启用的功能包 |
| `ADMIN_TOKEN` | （自定义强口令） | 管理口令 |

## 部署步骤

1. 在 Abasthan 创建 Web Service，选择本仓库的 `abasthan-deploy` 分支。
2. 运行时选 Python 3.12，Root Directory 留空。
3. Build Command 填 `pip install -r backend/requirements.txt`。
4. Start Command 填 `python -m uvicorn main:app --host 0.0.0.0 --port $PORT`。
5. 按上表配置全部环境变量（尤其是 `DATABASE_URL`）。
6. 部署并观察启动日志：若因缺少 `DATABASE_URL` 退出，请补齐外部 PG 连接串后重新部署。
7. 访问服务根路径 `/`，会自动跳转到 `/ai-modeler/` 前端页面。
