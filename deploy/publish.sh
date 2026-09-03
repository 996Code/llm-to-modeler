#!/usr/bin/env bash
# =============================================================================
# 一键发布（本机执行）：push 到 GitHub → SSH 服务器执行 deploy.sh
#
# 用法：
#   ./deploy/publish.sh              # push + 远端发布
#   ./deploy/publish.sh --no-push    # 不 push，只发远端（已推送过时）
#
# 可用环境变量覆盖目标：
#   DEPLOY_HOST（默认 192.168.99.22） DEPLOY_USER（默认 root）
#   DEPLOY_DIR（默认 /njmind/deploy/middle/llm-modeler/repo）
#
# 认证：优先走 ssh key；没有 key 时 ssh 会交互式提示输密码
# （密码不写入仓库；想免密请把公钥追加到服务器 ~/.ssh/authorized_keys）
#
# 服务器初始化（新环境首次部署）：
#   1. git clone https://gh-proxy.org/https://github.com/996Code/llm-to-modeler.git
#      （国内服务器直连 GitHub 慢且常失败，gh-proxy 实测 1-3 秒；
#       已有检出则 set-url 切到同地址——本机 push 仍走 GitHub 直连）
#   2. 配好仓库根 .env（LLM key 等，参考 .env.example）
#   3. ./deploy/deploy.sh 首次发布
# =============================================================================
set -euo pipefail

HOST="${DEPLOY_HOST:-192.168.99.22}"
USER_="${DEPLOY_USER:-root}"
DIR="${DEPLOY_DIR:-/njmind/deploy/middle/llm-modeler/repo}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

log() { printf '\033[32m[publish]\033[0m %s\n' "$*"; }

if [ "${1:-}" != "--no-push" ]; then
  log "推送 origin/main ..."
  git -C "$ROOT" push origin main
fi

log "SSH ${USER_}@${HOST} 执行远端发布（${DIR}）..."
ssh -t -o StrictHostKeyChecking=no "${USER_}@${HOST}" \
  "cd ${DIR} && ./deploy/deploy.sh"

log "完成。远端管理：ssh ${USER_}@${HOST} 'cd ${DIR} && ./deploy/deploy.sh --status|--logs|--rollback'"
