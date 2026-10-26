# AGENTS.md — 开发协作规约与运维手册

> 给 AI agent / 新接手开发者的操作手册。架构与业务文档见 `README.md`（权威）、
> `doc/插件开发与嵌入指南.md`、`backend/src/domains/ARCHITECTURE.md`。
> 本文只写"怎么干活"：规约、纪律、流程、环境事实。日期戳 2026-10-26。

## 一、协作规约（用户裁决沉淀，违反会返工）

1. **完成标准 = 全量测试绿**：合并/大改动的"完成"是全量 pytest 通过（~1871 例，约 7 分钟），不是相关用例过了就行。
2. **新功能配新用例**：主路径 + 边界都要覆盖；"能跑" ≠ "测过"。UI 改动要浏览器实测（chrome-devtools MCP 真点真输，browser-use 视口不可靠）。
3. **验证用真实调用路径**：兼容性论断必须探真实依赖（Jackson ≠ FastJSON 那种事故）；假对象单测会吞掉框架约束。
4. **用户质疑交付 = 先跑真实验证**：拿证据再回答，不要凭记忆辩护（单日五次质疑全中的教训）。
5. **根治不打补丁**：值域/契约问题先全链路取证（存什么/读什么），拒绝逐症状补丁。
6. **改写后必须重验**：amend/重打包后旧验证作废，重新构建再测端点。
7. **破坏性操作先盘点现场**：rsync --delete / 重建 / 删库前先查相邻部署与 bind mount。
8. **LLM 失败不静默降级**：交互链路 LLM 挂了直接抛异常，假可用比报错更有害。
9. **设计从简**：先给最小版 + "不做清单"；砍机制性复杂度（key 体系/拦截器/迁移框架）。
10. **文档只描述当前行为**：不写"从 X 改成 Y"的历史叙事。
11. **对外地址一律 `/ai-modeler/api/...` 网关形式**，不显示后端裸路径；后端路径不改，网关剥前缀。
12. **文件名用英文**（URL 编码友好）；行内按钮图标化 + tooltip，工具栏图标+文字。
13. **方案讲解用大白话**：先流程例子再规则表，术语随用随翻译。
14. **超长文件写入分段**：~2.6 万字符以上的一次性 Write 会撞输出上限截断，分两次写。

## 二、测试环境

- 全量测试：`cd backend && .venv/bin/python -m pytest tests/ -q`
- **必须用 `backend/.venv`（uv 管理）**；旧的 `backend/venv` 是 7 月遗物，starlette/httpx2 不齐会在收集期报错。
- 测试库需要本机 Docker（chatbi-postgres 容器）；5432 拒连先 `open -a Docker`。
- 测试库并发互踩已根治（advisory lock fail-fast）；僵尸 mock 进程是慢测隐形元凶。

## 三、分支与发布

- **main 直提**（小修/加固）；计划型功能走 feat 分支，完成后快进合并。
- **feat/bid-demo 长期并存**：竞标 demo 在途。修复要**双落**（feat/bid-demo + main 各一份，cherry-pick），双推送。
- 双远程：`origin`（GitHub 996Code/llm-to-modeler）+ `njmind`（内网 GitLab 192.168.99.225:8888，HTTP，凭证在 macOS 钥匙串 yinjintao）。**每次推送两边一起推**。
- 主工作区常带用户在途文件 → **部署出包必须从干净 main 建 git worktree**（`git worktree add /tmp/deploy-clean origin/main`），rsync 从 worktree 走，用完 remove。

## 四、部署（192.168.99.22 llm-modeler-kg）

```
cd <干净worktree>
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt -e ssh ./ root@192.168.99.22:/njmind/deploy/middle/llm-modeler-kg/repo/
ssh root@192.168.99.22 'cd /njmind/deploy/middle/llm-modeler-kg/repo && bash deploy.sh --skip-pull'
```

- 形态：仅 KG 插件（PACKS_ENABLED=knowledge_graph），19090，免密（无 ADMIN_TOKEN）；neo4j 17687 / pgvector 15432；**表在 knowledge_graph schema**（psql 要带前缀）。
- 凭证（root/Neo4j 密码）在服务器 `.env` 与本地备忘，**不入库**。
- **deploy.sh 守卫**：导入队列有 running/pending 时拒绝构建。FORCE_DEPLOY=1 强制——仅限导入处于 LLM 抽取期（CPU 低）时用；重启后任务框架会断点续跑自动接上（实测可靠）。
- LLM = 996code 中转站 qwen3.7-plus（速度波动大，每批 4 块 50-80s 常见）；embedding = 本地 bge-m3。
- **内存红线**：app 容器 2g 上限，宿主机 19G/可用仅 ~2.8G，**不要加内存**。大文件（数百 MB）导入曾把原始字节全程持有 + bge-m3 叠加顶爆 cgroup（450MB 实测 OOM）；现行代码解析完即 `del data`。上传走流式落盘，>64MB 跳过解析预检。
- 单文件上传上限 500MB（schema `max_upload_mb`，nginx `client_max_body_size 520m`）。
- 红线：28080（别人的部署）绝对不动；1Panel PG(5432) 是共享生产库。

## 五、在途工作快照（2026-10-26，会过时，接手先核对）

- **常州院 KB 初始化**（99.22）：常州院（文件夹）> 外部文件 + 内部文件（两库）。内部 3 份：450MB 程序文件 succeeded（282 块）、162MB 管理手册导入中、87KB CMA 汇编排队。外部 3118 个规程 PDF（3.8GB，`~/Downloads/国家规程规范文件`）待批量上传+导入——用批量导入端点 `POST /kbs/{kb_id}/import`，预计数十小时（LLM 逐块抽取）。
- **导入体验已定型**：上传成功即自动提交导入任务（前端 DocManager + 建库向导同口径）；预估弹框只在 >30 分钟文档出现。
- **feat/bid-demo**：南京院智能客服竞标 demo 在途（用户手上有未提交文件：tools/mcp-e2e/*、chatbi/controlled.py、kg/service.py 等，不要动）。
- 多层文件夹：上限 4 层（建+移按整棵子树核算）；树 UI 图标即开关 + 竖向连接线。

## 六、惯发坑（踩过的，别再踩）

- route() 元组漂移：改路由返回契约时 grep 全量调用点，不是只改入口。
- 批量改必落点验证：先读签名/调用方 → 改 → 即跑该文件测试；验证后置曾致 31 测试连锁失败。
- 后台 agent 会静默失败零产出：汇报完成前必须核实文件真实存在。
- 部署后"页面没变"三因：后端未重启 / HMR 未刷到 iframe / 看错组件。
- psql 远程查询：`created_at` 是 TEXT，比较要 `::timestamptz`；zsh 里 `--include` 要引号。
- SSH 远端多命令输出会拼接（无尾换行）：用 grep -o + cut 提取单值，别用 sed 行号。
