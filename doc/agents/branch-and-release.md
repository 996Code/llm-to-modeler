# 分支与发布流程

## 分支模型

- **只保留 main，直接在 main 上按主题分组提交**（2026-10-27 拍板；GitHub 与本地不再有 feat 分支）。
- 提交前跑齐回归（后端 pytest / 前端 vue-tsc+vitest+build），出包纪律见下。

## 远程

| 远程 | 地址 |
|---|---|
| origin | git@github.com:996Code/llm-to-modeler.git |

## 部署纪律

**常规发布 = 服务器源码一键**（2026-11-06 恢复；此前 10-27~11-05 因 99.22
内存/磁盘受限临时改为本地出包，99.22 已冻结）：服务器上源码树直接

```bash
cd <服务器源码树> && ./deploy.sh
```

一条命令完成：git pull → 容器内构建（前端/依赖/deb，缓存卷复用）→ 打镜像 →
compose 切换（停旧启新）→ 四层健康验证 → 失败自动回滚；另有
`--status/--logs/--down/--rollback N`。

- **服务器本地差异**（searxng 网络、equipment-mcp 等）一律放
  `docker-compose.override.yml`（不入库，deploy.sh 自动附加）——
  **绝不直接改 tracked 的 docker-compose.yml**：改了 git 树变脏、pull 卡死，
  一键就断（10 月至 11-05 期间的手改 compose + 备份还原舞蹈即此教训）。
- 99.22 时期的完整部署细节见 [99.22 部署手册](deploy-99-22.md)（历史存档，
  99.22 已停更）。

**应急通道 = 本地出包推镜像**（`deploy/local-build.sh`，服务器无力构建/离线时用）：
从**最新 main** 的干净提交构建，不带任何工作区的在途文件。

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
