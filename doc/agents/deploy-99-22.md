# 部署手册（192.168.99.22 llm-modeler-kg）

## 形态

- 完整 compose 栈（**全部中间件一站编排**）：app（nginx+uvicorn 一体）+
  **metrology-mcp**（mock 计量业务平台，客服 Demo）+ neo4j + postgres(pgvector)
- 默认 **19090**（HOST_PORT），免密（无 ADMIN_TOKEN）
- neo4j 17687(bolt) / pgvector 15432；**表在 knowledge_graph schema**（psql 查询要带前缀）
- **LLM 供应商 = DB 唯一事实源**（`llm_providers` 表，管理端「插件页→模型供应商」维护，热生效）：
  .22 配 996code 中转站 qwen3.7-plus（速度波动大，每批 4 块 50-80s 常见）。
  **env 的 LLM_BASE_URL/API_KEY/MODEL 已废弃**（双事实源被裁决去掉）——env 只管温度/超时等
  非端点参数。新库/新部署须先配置供应商，否则所有 LLM 调用前置闸报错
  （deploy.sh 健康验证后自动探测 `configured` 并提醒）。
- embedding = 本地 bge-m3
- 凭证（root SSH / Neo4j 密码）在服务器 `.env` 与运维备忘，**不入库**
- 红线：**28080（别人的部署）绝对不动**；1Panel PG(5432) 是共享生产库

## metrology-mcp / equipment-mcp（mock 业务平台）

- **equipment-mcp（10-28 新增,第二业务域）**：同款复用 app 镜像
  （`command: python /app/mcp-e2e/equipment_server.py`，9772 仅 compose 内网），
  注册表 code=equipment、数据源「设备管理平台(通用)」host=equipment 绑定；
  服务器 compose 已含该服务（docker-compose.yml.bak-1028 为加它之前的备份）。

## metrology-mcp（mock 计量业务平台）

- **复用 app 镜像**（`command: python /app/mcp-e2e/metrology_server.py`）——mcp SDK
  已在后端依赖里，零额外镜像；内存护栏 128m（实测 ~40MB）
- **不映射宿主端口**：仅 compose 内网可达，app 经 `http://metrology-mcp:9771/mcp`
  连接（**MCP 注册表 URL 用这个**，不是 127.0.0.1——server 与 app 不在同一容器）
- healthcheck：`/mcp` 对无协议头 GET 返回 **406=活**（streamable HTTP 协商拒绝），
  200/406 均判活；app 对它是 `service_started`（非阻断——mock 挂了不拖垮主服务）
- 不要该服务：注释掉 compose 里整个块即可，其余服务零影响

## 客服 Demo 服务器侧一次性配置

0. **LLM 供应商迁移**（旧 .env → DB，一次性）：管理页手工填，或直接写库——
   `insert into llm_providers (scope, revision, content, updated_by, updated_at)
   values ('global', 1, '{"primary":{"base_url":"http://996code.top:18080/v1","api_key":"<旧env值>","model":"qwen3.7-plus","enabled":true,"price_in":null,"price_out":null}}', 'migration', '<时间>')`
   （表在 public schema；写完删 .env 里废弃的 LLM_BASE_URL/API_KEY/MODEL 三行）
1. `.env` 扩容：`PACKS_ENABLED` 加 `chatbi,metrology_assistant`（或删掉该键=全量），
   配 `METROLOGY_PROGRESS_DATASOURCE_ID` / `METROLOGY_INSTRUMENT_DATASOURCE_ID`
2. 注册数据（对着 19090 API 跑 `tools/bid-demo/setup_demo.py`，MCP URL 用上面的
   服务名形式）：MCP server（code=metrology）+ chatbi 两个固定工具数据源
3. 知识库导入：「强检目录」「检定规程」两组文档（强检判定链的资料来源）
4. 验收：`curl http://127.0.0.1:19090/ai-modeler/api/packs/metrology_assistant/test/env`
   应显示两库 ok + 数据源 ok + mcp ok

## 部署命令（本地打镜像，服务器免构建）

服务器 19G 内存（可用 ~3G）/磁盘紧张，在其上跑构建既慢又与导入队列抢资源——
构建全在开发机（Mac）上做，服务器只收镜像直接切换：

```bash
cd <干净worktree>   # 出包纪律见 branch-and-release.md:必须先同步到最新 main
                    # 再出包(worktree 停在旧提交会打出旧包——脚本有新鲜度守卫,
                    # 但先 remove 再 add + git log 核对是第一原则);不带主工作区在途文件
bash deploy/local-build.sh
```

脚本一键串完（细节见其头部注释）：

1. 前端 `pnpm build`（宿主机；JS 产物平台无关）
2. 后端依赖 pip -t + nginx deb（**amd64 容器**——Mac 是 ARM，装出的
   arm64 二进制在 x86 服务器不可用；requirements sha 与 deb 有本地缓存，命中跳过）
3. `docker buildx --platform linux/amd64` 打镜像（tag = `<git短哈希>-<月日时分>`）
4. `docker save | gzip | ssh docker load` 推镜像到服务器并打 `latest` 标签
5. 最小同步 `deploy.sh`（服务器不留源码树，产物全在镜像里）
6. 服务器 `SKIP_BUILD=1 IMAGE_TAG=<tag> bash deploy.sh` 仅切换 `app`/`metrology-mcp`
   （`up -d --no-deps`），不重建 PostgreSQL/Neo4j；四层健康验证失败则回滚业务容器
7. 健康验证通过后自动删除本次本地镜像标签；服务器副本保留用于回滚

CPU 配置需在服务器 compose 的业务服务中声明 `cpus: ${APP_CPU_LIMIT:-4}` /
`cpus: ${MCP_CPU_LIMIT:-0.5}`；首次增加时只修改这两段，不覆盖服务器专有的
容器名、端口、数据卷和中间件配置。

- **deploy.sh 守卫**：导入队列有 running/pending 时拒绝切换。FORCE_DEPLOY=1 强制——仅限导入处于 LLM 抽取期（CPU 低）时用；重启后任务框架断点续跑自动接上（实测可靠，96 块进度无损）。
- **deploy.sh ⑤ 健康验证（四层,失败自动回滚旧 TAG）**：postgres（pg_isready 收敛）→
  neo4j（HTTP :7474 收敛）→ metrology-mcp（/mcp 200/406 收敛）→ app（容器健康 +
  宿主端口端到端 curl 穿 nginx）。探测等 compose healthcheck 收敛（`docker inspect
  State.Health.Status`），检查逻辑单一事实源在 compose 文件
- **停全栈后再部署的顺序坑**：`deploy.sh --down` 删掉全部容器后，切换脚本只拉
  app/metrology-mcp（`up -d --no-deps`）不重建 neo4j——app 启动时 KG 探针
  DNS 失败 → knowledge_graph 插件被跳过 →"指定的插件当前不可用"。**先
  `docker compose up -d neo4j postgres` 等健康，再跑切换**（10-28 实测）。
- 回滚：`bash deploy.sh --rollback`（次新镜像；app 与 metrology-mcp 同镜像同切换）。
- **收尾清理（本地）**：部署验证通过后删本地构建镜像（服务器已 `docker load`
  副本，回滚用服务器上的）：`docker rmi llm-modeler:<tag> llm-modeler:latest-local`。
  `.deps.amd64`/`debs` 构建缓存保留（requirements 未变时下次构建直接跳过）。
- embedding 模型不打进镜像（服务器 data/models 已有，compose 卷挂载）。

## 本地跑全量测试（约定）

- 测试库**直接用本机常驻的 chatbi-postgres 容器**（`pgvector/pgvector:pg16` 镜像，
  root:root@localhost:5432）里建独立测试库（名字必须含 `test` 过 conftest 安全闸）：
  `docker exec chatbi-postgres psql -U root -d llm_modeler_test_run -c "CREATE EXTENSION IF NOT EXISTS vector;"`
- 项目根 `.env` 已配 `TEST_DATABASE_URL=postgresql://root:root@localhost:5432/llm_modeler_test_run`
  （conftest 兜底自动读）；worktree 里跑测试需把根 `.env` 拷到 worktree 根。
- 已有测试库被其他会话占用时，在同一个常驻 PG 容器中另建名字含 `test` 的
  隔离数据库，用 `TEST_DATABASE_URL` 显式指定；不终止其他会话或清它的库。
- 新建的隔离测试库先使用现有 `domains.chatbi.runtime._init_pack_schema` 完成
  ChatBI 启动迁移，避免旧 DDL 缺 `scope_id`/统计表产生假失败；全量测试同时设置
  `SOAK_DATABASE_URL` 为同一隔离测试库，监控脚本才能检查本轮数据。
- 只清理本次创建的临时容器/镜像，不删除常驻 PG 镜像，不做全局 prune。
- **不要**另起 `postgres:xx-alpine` 测试容器——没有 vector 扩展，向量/标注类用例
  会整片失败；也不要与其他套件并行跑同一测试库（租约/并发用例互相踩）。

## 内存红线与大文件

- app 容器 **2g 上限**，宿主机 19G/可用仅 ~2.8G，**不要加内存**。
- **CPU 同限**：compose `cpus` 已设 app=4 / metrology-mcp=0.5（宿主 10 核与他人共享，
  防 OCR/embedding 推理吃满全部核；经 `.env` 的 `APP_CPU_LIMIT`/`MCP_CPU_LIMIT` 调整）。
- 大文件（数百 MB）导入曾把原始字节全程持有 + bge-m3 叠加顶爆 cgroup（450MB 实测 OOM）；现行代码解析完即 `del data`。
- 上传走流式落盘（不整载内存）；>64MB 跳过解析预检（坏文件由导入任务兜底报错）。
- 单文件上传上限 **500MB**（schema `max_upload_mb`，nginx `client_max_body_size 520m`）。

## 常用排查

- 任务队列：`curl http://127.0.0.1:19090/ai-modeler/api/tasks?status=running`
- 文档状态：psql `select left(filename,30), import_status, chunk_count from knowledge_graph.kg_documents`
- 容器内存：`docker stats llm-modeler-kg --no-stream`
- OOM 取证：`dmesg -T | grep -i oom`（cgroup kill 会显示 anon-rss）
- force 重导被截的坑：守卫看 importStatus=succeeded 标"已由成功导入取代"不重放——需手动只补缺失项，不全量重发。
