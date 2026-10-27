# 分支与发布流程

## 分支模型

- **main 直提**（小修/加固）；计划型功能走 feat 分支，完成后快进合并。
- **feat/bid-demo 长期并存**：竞标 demo 在途。修复要**双落**（feat/bid-demo + main 各一份，cherry-pick），双推送。

## 双远程

| 远程 | 地址 | 说明 |
|---|---|---|
| origin | git@github.com:996Code/llm-to-modeler.git | GitHub（大传输走 SSH） |
| njmind | http://192.168.99.225:8888/njmind-ai/llm-modeler.git | 内网 GitLab（HTTP，凭证在 macOS 钥匙串 yinjintao） |

- **每次推送两边一起推**：`git push origin main && git push njmind main`
- 内网 SSH 方式不可用（22 端口公钥未登记），走 HTTP 8888。

## 部署出包纪律

从干净的已验证 main 提交构建，不带其他工作区的在途文件。现行发布方式为
开发机本地构建 linux/amd64 业务镜像，传至服务器后仅切换业务容器；不推源码树，
不在服务器构建，不重建 PostgreSQL/Neo4j。完整命令及清理规则见
[99.22 部署手册](deploy-99-22.md)。

```bash
git worktree add --detach /tmp/deploy-clean main
cd /tmp/deploy-clean
bash deploy/local-build.sh
```

出包需要前端依赖与 amd64 依赖/deb 缓存时，复用本地已验证缓存。
部署健康验证通过后脚本删除本次本地镜像标签，保留服务器镜像供回滚。
临时 worktree 用完且检查无用户文件后移除。
