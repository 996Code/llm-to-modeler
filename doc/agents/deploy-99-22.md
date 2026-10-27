# 部署手册（192.168.99.22 llm-modeler-kg）

## 形态

- 完整 compose 栈（**全部中间件一站编排**）：app（nginx+uvicorn 一体）+
  **metrology-mcp**（mock 计量业务平台，客服 Demo）+ neo4j + postgres(pgvector)
- 默认 **19090**（HOST_PORT），免密（无 ADMIN_TOKEN）
- neo4j 17687(bolt) / pgvector 15432；**表在 knowledge_graph schema**（psql 查询要带前缀）
- LLM = 996code 中转站 qwen3.7-plus（速度波动大，每批 4 块 50-80s 常见）；embedding = 本地 bge-m3
- 凭证（root SSH / Neo4j 密码）在服务器 `.env` 与运维备忘，**不入库**
- 红线：**28080（别人的部署）绝对不动**；1Panel PG(5432) 是共享生产库

## metrology-mcp（mock 计量业务平台）

- **复用 app 镜像**（`command: python /app/mcp-e2e/metrology_server.py`）——mcp SDK
  已在后端依赖里，零额外镜像；内存护栏 128m（实测 ~40MB）
- **不映射宿主端口**：仅 compose 内网可达，app 经 `http://metrology-mcp:9771/mcp`
  连接（**MCP 注册表 URL 用这个**，不是 127.0.0.1——server 与 app 不在同一容器）
- healthcheck：`/mcp` 对无协议头 GET 返回 **406=活**（streamable HTTP 协商拒绝），
  200/406 均判活；app 对它是 `service_started`（非阻断——mock 挂了不拖垮主服务）
- 不要该服务：注释掉 compose 里整个块即可，其余服务零影响

## 客服 Demo 服务器侧一次性配置

1. `.env` 扩容：`PACKS_ENABLED` 加 `chatbi,metrology_assistant`（或删掉该键=全量），
   配 `METROLOGY_PROGRESS_DATASOURCE_ID` / `METROLOGY_INSTRUMENT_DATASOURCE_ID`
2. 注册数据（对着 19090 API 跑 `tools/bid-demo/setup_demo.py`，MCP URL 用上面的
   服务名形式）：MCP server（code=metrology）+ chatbi 两个固定工具数据源
3. 知识库导入：「强检目录」「检定规程」两组文档（强检判定链的资料来源）
4. 验收：`curl http://127.0.0.1:19090/ai-modeler/api/packs/metrology_assistant/test/env`
   应显示两库 ok + 数据源 ok + mcp ok

## 部署命令

```bash
cd <干净worktree>   # 见 branch-and-release.md 的出包纪律
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt -e ssh ./ root@192.168.99.22:/njmind/deploy/middle/llm-modeler-kg/repo/
ssh root@192.168.99.22 'cd /njmind/deploy/middle/llm-modeler-kg/repo && bash deploy.sh --skip-pull'
```

- **deploy.sh 守卫**：导入队列有 running/pending 时拒绝构建。FORCE_DEPLOY=1 强制——仅限导入处于 LLM 抽取期（CPU 低）时用；重启后任务框架断点续跑自动接上（实测可靠，96 块进度无损）。
- **deploy.sh ⑤ 健康验证（四层,失败自动回滚旧 TAG）**：postgres（pg_isready 收敛）→
  neo4j（HTTP :7474 收敛）→ metrology-mcp（/mcp 200/406 收敛）→ app（容器健康 +
  宿主端口端到端 curl 穿 nginx）。探测等 compose healthcheck 收敛（`docker inspect
  State.Health.Status`），检查逻辑单一事实源在 compose 文件
- 回滚：`bash deploy.sh --rollback`（次新镜像；app 与 metrology-mcp 同镜像同切换）。

## 内存红线与大文件

- app 容器 **2g 上限**，宿主机 19G/可用仅 ~2.8G，**不要加内存**。
- 大文件（数百 MB）导入曾把原始字节全程持有 + bge-m3 叠加顶爆 cgroup（450MB 实测 OOM）；现行代码解析完即 `del data`。
- 上传走流式落盘（不整载内存）；>64MB 跳过解析预检（坏文件由导入任务兜底报错）。
- 单文件上传上限 **500MB**（schema `max_upload_mb`，nginx `client_max_body_size 520m`）。

## 常用排查

- 任务队列：`curl http://127.0.0.1:19090/ai-modeler/api/tasks?status=running`
- 文档状态：psql `select left(filename,30), import_status, chunk_count from knowledge_graph.kg_documents`
- 容器内存：`docker stats llm-modeler-kg --no-stream`
- OOM 取证：`dmesg -T | grep -i oom`（cgroup kill 会显示 anon-rss）
- force 重导被截的坑：守卫看 importStatus=succeeded 标"已由成功导入取代"不重放——需手动只补缺失项，不全量重发。
