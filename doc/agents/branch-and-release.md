# 分支与发布流程

## 分支模型

- **只保留 main，直接在 main 上按主题分组提交**（2026-10-27 拍板；GitHub 与本地不再有 feat 分支）。
- 提交前跑齐回归（后端 pytest / 前端 vue-tsc+vitest+build），出包纪律见下。

## 远程

| 远程 | 地址 |
|---|---|
| origin | git@github.com:996Code/llm-to-modeler.git |

## 部署出包纪律

从**最新 main** 的干净提交构建，不带任何工作区的在途文件。现行发布方式为
开发机本地构建 linux/amd64 业务镜像，传至服务器后仅切换业务容器；不推源码树，
不在服务器构建，不重建 PostgreSQL/Neo4j。完整命令及清理规则见
[99.22 部署手册](deploy-99-22.md)。

```bash
git fetch origin
git worktree remove --force /tmp/deploy-clean 2>/dev/null || true   # 清掉旧 worktree,绝不复用
git worktree add --detach /tmp/deploy-clean origin/main
cd /tmp/deploy-clean
git log -1 --oneline   # ⓪ 肉眼核对:这就是你要部署的提交
bash deploy/local-build.sh
```

**为什么不复用旧 worktree（10-28 实录）**：某次部署复用了 /tmp 里遗留的 worktree，
它停在昨天的 main——服务器拿到落后 7 个提交的镜像，页面与本地差异巨大。
`local-build.sh` 已加新鲜度守卫（HEAD ≠ origin/main 直接拒建，历史版本重打用
`ALLOW_STALE_HEAD=1`），但**先 remove 再 add 仍是第一原则**：worktree 停在哪个
提交只有 git log 知道，眼见为实。

出包需要前端依赖与 amd64 依赖/deb 缓存时，复用本地已验证缓存
（`rsync -a <主仓库>/backend/.deps.amd64 <主仓库>/backend/debs backend/`，
`rsync -a <主仓库>/frontend/node_modules frontend/`——worktree 是新检出没有缓存）。
部署健康验证通过后脚本删除本次本地镜像标签，保留服务器镜像供回滚。
临时 worktree 用完且检查无用户文件后移除。
