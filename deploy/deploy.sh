#!/usr/bin/env bash
# =============================================================================
# llm-to-modeler 一键更新发布（单容器 + compose 编排管理）
#
# 用法：
#   ./deploy.sh                # 拉码 → 预构建 → 打镜像 → compose 切换 → 健康验证
#   ./deploy.sh --skip-pull    # 跳过 git pull（本地改完直接发）
#   ./deploy.sh --status       # compose ps + 镜像清单
#   ./deploy.sh --logs         # 跟随日志
#   ./deploy.sh --down         # 停止（数据卷保留）
#   ./deploy.sh --rollback N   # 回滚到第 N 新的历史镜像（默认次新）
#
# 构建策略（对齐 njmind 镜像习惯，规避构建容器 DNS 坑）：
#   联网步骤（pnpm build / pip install / apt 下载）全在 docker run 的容器里
#   做（其网络正常）；docker build 只做纯 COPY。基础镜像缺失时自动从
#   daocloud 公共加速源补拉。
#
# 容器管理：docker compose（deploy/docker-compose.yml）——版本经 TAG 切换，
# 数据卷/健康检查声明式维护；回滚 = 旧 TAG 再 up。
# =============================================================================
set -euo pipefail

# ── 可配置项（环境变量覆盖）──────────────────────────────────────────────
IMAGE_NAME="${IMAGE_NAME:-llm-modeler}"
HOST_PORT="${HOST_PORT:-28080}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
MIRROR="${MIRROR:-docker.m.daocloud.io/library}"
NODE_IMAGE="${NODE_IMAGE:-node:20-alpine}"
PY_IMAGE="${PY_IMAGE:-python:3.12-slim}"

COMPOSE_FILE="$(cd "$(dirname "$0")" && pwd)/docker-compose.yml"

# 工作目录 = 仓库根（.env 所在处）：脚本位于 deploy/ 子目录
WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
# 本脚本绝对路径 + 启动时内容指纹：pull 若换掉脚本本体，bash 会按字节
# 偏移续读旧文件执行错位内容——检测到变化后用新脚本 re-exec（见 ① 段）
SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
SELF_HASH="$(sha256sum "$SCRIPT_PATH" 2>/dev/null | cut -d' ' -f1)"

log()  { printf '\033[32m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
err()  { printf '\033[31m[%s] ERROR\033[0m %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
cd "$WORKDIR"

dc() { docker compose -f "$COMPOSE_FILE" "$@"; }

# ── 子命令 ──────────────────────────────────────────────────────────────
case "${1:-}" in
  --status) dc ps; docker images "$IMAGE_NAME" --format "table {{.Repository}}:{{.Tag}}\t{{.CreatedAt}}" | head -6; exit 0 ;;
  --logs)   dc logs -f --tail 100; exit 0 ;;
  --down)   dc down; log "已停止（数据卷保留）"; exit 0 ;;
  --rollback)
    shift
    N="${1:-2}"   # 默认次新（第 2 新）
    ROLLBACK_TAG=$(docker images "$IMAGE_NAME" --format '{{.Tag}}' | sed -n "${N}p")
    [ -n "$ROLLBACK_TAG" ] || { err "找不到第 ${N} 新的镜像"; exit 1; }
    log "回滚到 ${IMAGE_NAME}:${ROLLBACK_TAG}"
    TAG="$ROLLBACK_TAG" HOST_PORT="$HOST_PORT" dc up -d
    log "回滚完成"; exit 0 ;;
esac

SKIP_PULL=0
for arg in "$@"; do
  case "$arg" in
    --skip-pull) SKIP_PULL=1 ;;
    *) err "未知参数: $arg（--status/--logs/--down/--rollback N）"; exit 1 ;;
  esac
done

command -v docker >/dev/null || { err "docker 未安装"; exit 1; }
docker info >/dev/null 2>&1 || { err "docker daemon 未运行"; exit 1; }
[ -f .env ] || { err "缺少 .env（后端 LLM/上游配置）"; exit 1; }

# ── ① 拉码 ──────────────────────────────────────────────────────────────
if [ "$SKIP_PULL" -eq 0 ] && [ -d .git ]; then
  log "① 拉取代码（${GIT_REMOTE}/${GIT_BRANCH}）..."
  git fetch "$GIT_REMOTE" "$GIT_BRANCH" 2>/dev/null || true
  # dirty 只看 tracked 改动:untracked(运维手工放的文件/构建产物)不阻断
  # 发布——真实事故:服务器一个 untracked Dockerfile 让 pull 永远跳过,
  # 连续多次"发布成功"实际全程构建旧代码
  if ! git diff --quiet -- || ! git diff --cached --quiet; then
    log "   tracked 文件有未提交改动，按当前工作区构建（dirty）"
    GIT_TAG="dirty-$(date '+%m%d%H%M')"
  else
    rm -f frontend/pnpm-workspace.yaml  # pnpm 新版遇 ignored-builds 会自动生成残缺模板,清掉防污染
    git pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH" 2>/dev/null || true
    # 自更新保护：本次提交改了 deploy.sh 本体时，当前进程读的仍是旧文件
    # 偏移——交给磁盘上的新脚本从 --skip-pull 重新跑（真实事故：错位执行
    # 跳过了 dist 同步步骤导致构建失败）
    NEW_HASH="$(sha256sum "$SCRIPT_PATH" 2>/dev/null | cut -d' ' -f1)"
    if [ -n "$SELF_HASH" ] && [ "$SELF_HASH" != "$NEW_HASH" ]; then
      log "   deploy.sh 已被本次更新修改，切换到新脚本继续"
      exec bash "$SCRIPT_PATH" --skip-pull
    fi
    GIT_TAG="$(git rev-parse --short HEAD)"
  fi
else
  GIT_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
fi
log "   版本标签：${GIT_TAG}"

# ── ② 基础镜像自检（缺失自动从加速源补拉）─────────────────────────────
ensure_image() {
  docker image inspect "$1" >/dev/null 2>&1 && return 0
  log "   本地缺 $1，从 ${MIRROR} 补拉..."
  docker pull "${MIRROR}/$1" && docker tag "${MIRROR}/$1" "$1" \
    || { err "拉取 $1 失败，请手工准备"; exit 1; }
}
ensure_image "$NODE_IMAGE"
ensure_image "$PY_IMAGE"
ensure_image nginx:stable-alpine

# ── ②a 前端构建（docker run 容器内 pnpm，npmmirror 源）────────────────
log "②a 构建前端（node 容器内 pnpm build）..."
mkdir -p frontend/dist
docker run --rm -v "$PWD/frontend:/app" -w /app \
  -e npm_config_registry=https://registry.npmmirror.com \
  "$NODE_IMAGE" sh -c "
    export CI=true &&
    npm config set registry https://registry.npmmirror.com &&
    npm install -g pnpm@9 &&
    pnpm config set registry https://registry.npmmirror.com &&
    pnpm install --frozen-lockfile &&
    pnpm run build
  " 2>&1 | tail -3
[ -f frontend/dist/index.html ] || { err "前端构建失败"; exit 1; }
log "   dist 就绪（$(du -sh frontend/dist | awk '{print $1}')）"

# ── ②b 后端依赖（docker run 容器内 pip -t，清华源）────────────────────
log "②b 安装后端依赖（python 容器内 pip -t）..."
rm -rf backend/.deps && mkdir -p backend/.deps
docker run --rm -v "$PWD/backend:/app" -w /app \
  "$PY_IMAGE" sh -c "
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r requirements.txt -t /app/.deps
  " 2>&1 | tail -2
[ -d backend/.deps/fastapi ] || { err "后端依赖安装失败"; exit 1; }
log "   .deps 就绪（$(du -sh backend/.deps | awk '{print $1}')）"

# ── ②c nginx deb 预下载（docker run 容器内 apt --download-only）───────
# 注意：--download-only 的 .deb 落在容器 /var/cache/apt/archives（含 partial/ 临时
# 区）。网络抖动时 rename partial→archives 会失败，因此重试 3 次（每次重跑会先
# update，已下载的 deb 不重复拉）。全部失败即退出——debs 缺失时 Dockerfile
# 里 dpkg -i 也会失败，这里更早暴露问题。
log "②c 预下载 nginx deb..."
rm -rf backend/debs && mkdir -p backend/debs
debs_ok=0
for attempt in 1 2 3; do
  docker run --rm -v "$PWD/backend:/app" -w /app \
    "$PY_IMAGE" sh -c "
      apt-get update -qq &&
      apt-get install -y --download-only --no-install-recommends nginx curl >/dev/null &&
      cp /var/cache/apt/archives/*.deb /app/debs/
    " && { debs_ok=1; break; }
  log "   nginx deb 下载第 ${attempt} 次失败，重试..."
done
[ "$debs_ok" -eq 1 ] && [ "$(ls backend/debs/*.deb 2>/dev/null | wc -l)" -ge 1 ] \
  || { err "nginx deb 下载失败"; exit 1; }
log "   debs 就绪（$(ls backend/debs | wc -l) 个包）"

# ── ③ 打镜像（纯 COPY，零联网 build）───────────────────────────────────
log "③ 打镜像 ${IMAGE_NAME}:${GIT_TAG}（纯 COPY）..."
# 构建上下文 = backend/（deps/src/dist/debs/Dockerfile 同处）
# ⚠ dist 必须从 frontend/ 同步过来:Dockerfile 的 COPY dist/ 指 backend/dist。
# 缺这步时镜像会一直打进 backend/ 下的陈旧 dist（真实事故:线上前端
# 停在 8-21 的手工拷贝,管理页缺失、新功能两周未上线）
rm -rf backend/dist && cp -R frontend/dist backend/dist
cp deploy/single/Dockerfile backend/Dockerfile
cp deploy/single/nginx.conf backend/nginx-single.conf
cp deploy/single/start.sh backend/start-single.sh
docker build -t "${IMAGE_NAME}:${GIT_TAG}" -t "${IMAGE_NAME}:latest" backend/
rm -f backend/Dockerfile backend/nginx-single.conf backend/start-single.sh

# ── ④ compose 切换容器（记录旧 TAG 供回滚）────────────────────────────
OLD_TAG=$(dc ps --format '{{.Image}}' 2>/dev/null | sed 's/.*://' || true)
log "④ compose 切换（旧：${OLD_TAG:-无} → 新：${GIT_TAG}）..."
TAG="$GIT_TAG" HOST_PORT="$HOST_PORT" dc up -d

# ── ⑤ 健康验证（失败自动回滚旧 TAG）────────────────────────────────────
log "⑤ 健康探测（最长 ${HEALTH_TIMEOUT}s）..."
elapsed=0
until curl -sf --max-time 3 "http://127.0.0.1:${HOST_PORT}/ai-modeler/api/health" | grep -q '"status": *"healthy"'; do
  sleep 2; elapsed=$((elapsed + 2))
  if [ "$elapsed" -ge "$HEALTH_TIMEOUT" ]; then
    err "健康探测超时。最近日志："
    dc logs --tail 25 >&2 || true
    if [ -n "$OLD_TAG" ]; then
      err "回滚到 ${OLD_TAG}..."
      TAG="$OLD_TAG" HOST_PORT="$HOST_PORT" dc up -d
      log "已回滚"
    fi
    exit 1
  fi
done
log "   健康就绪 ✓（${elapsed}s）"

log "发布完成："
log "   前端入口  http://<主机>:${HOST_PORT}/ai-modeler/（网关同前缀反代）"
log "   管理      ./deploy.sh --status / --logs / --down"
log "   回滚      ./deploy.sh --rollback（次新版本）"
[ -n "$OLD_TAG" ] && log "   本次旧版  ${OLD_TAG}（镜像保留可回）"
