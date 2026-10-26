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

## 部署出包纪律（10-26 定型）

主工作区常带用户在途文件（feat/bid-demo 的 tools/mcp-e2e/*、chatbi/controlled.py 等），
rsync --delete 会把它们带上服务器——**部署前从干净 main 建 git worktree 出包**：

```bash
git worktree add /tmp/deploy-clean origin/main     # main 被主工作区占用时用 origin/main
cd /tmp/deploy-clean
rsync -az --delete --exclude-from=deploy/rsync-exclude.txt -e ssh ./ root@192.168.99.22:/njmind/deploy/middle/llm-modeler-kg/repo/
ssh root@192.168.99.22 'cd /njmind/deploy/middle/llm-modeler-kg/repo && bash deploy.sh --skip-pull'
git worktree remove /tmp/deploy-clean             # 用完清理（/tmp 重启会清空，留注册会失效）
```
