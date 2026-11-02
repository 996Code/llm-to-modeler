## 部署契约

本目录描述仓库根 `docker-compose.yml` 的完整部署栈：`app` 提供前端与 API，
`metrology-mcp` 提供客服示例所需的 MCP 业务平台，`postgres` 提供平台关系数据、
图谱存储（knowledge_graph schema）与 pgvector。Compose 会创建并管理这些服务及其
数据卷；`app` 依赖 `postgres` 的 `service_healthy`，等待
`metrology-mcp` 的 `service_started`。
发布脚本会在切换后再次检查各服务的健康状态，并检查宿主端口的网关路径。

镜像名与端口均按“进程环境 > 根 `.env` > 默认值”解析。镜像名默认为
`llm-modeler`，由 `IMAGE_NAME` 配置；版本用 `TAG` 选择。端口默认为 `19090`，
由 `HOST_PORT` 配置；
根 `.env` 由 Compose 解析，不会作为 shell 脚本执行。`.env` 只应保存部署所需
的本地配置和凭证，不要把凭证提交到版本库或写入日志。

常用命令（在仓库根目录执行）：

```sh
docker compose config --format json
docker compose ps
docker compose logs -f app
docker compose down
```

发布脚本负责前端构建、后端依赖与 nginx/curl 离线包准备、镜像构建、切换和
健康验证。默认 Python 基础镜像为 `python:3.12-slim-trixie`，也可用
`PY_IMAGE` 与 Docker build arg 一起覆盖；依赖和 deb 缓存会绑定实际基础镜像
ID，避免不同 Debian 发行版的文件混用。脚本不执行真实部署环境外的远程操作，
也不会打印供应商密钥。供应商探测使用 `/ai-modeler/api/admin/llm-providers`
网关路径；若管理端需要鉴权，则从 `app` 容器内读取现有供应商服务，仅输出
`configured` 布尔结果。

完整栈契约意味着删掉 `metrology-mcp` 或 `postgres` 会使对应的健康检查与
服务依赖失效；若使用外部数据库，仍需提供 Compose 声明的必填 PostgreSQL
配置，应用仍按完整栈契约启动本地服务。
