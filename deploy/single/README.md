
## 部署契约

本目录只支持**单容器、单 Uvicorn worker**。`start.sh` 不传 `--workers`，
插件启停、定时任务和运行态都以这一约束设计。

不要把该镜像扩成多副本或改成多 worker；动态插件启停只会更新当前进程的
registry、routes 和任务 handler。未来若需要横向扩容，应先设计集中式插件状态
广播和 scheduler leader election，而不是直接增加副本数。

## 本地改码同步到服务器（rsync 更新法）

服务器目录：`/njmind/deploy/middle/llm-modeler-kg/repo`（99.22）。本地改完
代码直接推，**必须**用 `deploy/rsync-exclude.txt`：

```
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt \
  -e ssh ./ root@192.168.99.22:/njmind/deploy/middle/llm-modeler-kg/repo/
ssh root@192.168.99.22 'cd /njmind/deploy/middle/llm-modeler-kg/repo && bash deploy.sh --skip-pull'
```

血泪教训（2026-10-17 事故，`--delete` 抹掉两样东西，缺一不可少排除）：

- **顶层 `data/`**：PG bind mount 在 `repo/data/postgres`——rsync 把它当
  本地不存在的目录删掉，postgres 容器数据目录被实时清空后 entrypoint 重新
  initdb，**全库丢失**；同目录 `data/models/embedding` 是 bge-m3 模型
  （543MB，deploy.sh 会重下，但库丢是灾难）。
- **`docker-compose.yml` 与 `.env`**：服务器侧配置与本地不同，覆盖即坏。

排除清单是唯一防线，改清单前先在服务器上核对 bind mount：
`docker inspect <app容器> --format '{{range .Mounts}}{{.Source}}{{println}}{{end}}'`

