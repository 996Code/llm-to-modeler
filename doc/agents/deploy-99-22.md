# 部署手册（192.168.99.22 llm-modeler-kg）

## 形态

- 仅 KG 插件（PACKS_ENABLED=knowledge_graph），**19090**，免密（无 ADMIN_TOKEN）
- neo4j 17687(bolt) / pgvector 15432；**表在 knowledge_graph schema**（psql 查询要带前缀）
- LLM = 996code 中转站 qwen3.7-plus（速度波动大，每批 4 块 50-80s 常见）；embedding = 本地 bge-m3
- 凭证（root SSH / Neo4j 密码）在服务器 `.env` 与运维备忘，**不入库**
- 红线：**28080（别人的部署）绝对不动**；1Panel PG(5432) 是共享生产库

## 部署命令

```bash
cd <干净worktree>   # 见 branch-and-release.md 的出包纪律
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt -e ssh ./ root@192.168.99.22:/njmind/deploy/middle/llm-modeler-kg/repo/
ssh root@192.168.99.22 'cd /njmind/deploy/middle/llm-modeler-kg/repo && bash deploy.sh --skip-pull'
```

- **deploy.sh 守卫**：导入队列有 running/pending 时拒绝构建。FORCE_DEPLOY=1 强制——仅限导入处于 LLM 抽取期（CPU 低）时用；重启后任务框架断点续跑自动接上（实测可靠，96 块进度无损）。
- 回滚：`bash deploy.sh --rollback`（次新镜像）。

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
