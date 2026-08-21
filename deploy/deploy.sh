#!/usr/bin/env bash
# =============================================================================
# llm-to-modeler 一键更新发布（单容器一体：uvicorn 伺服 API + 前端静态）
#
# 用法：
#   ./deploy.sh                # 完整流程：拉码 → 预构建 → 打镜像 → 切换 → 验证
#   ./deploy.sh --skip-pull    # 跳过 git pull（本地改完直接发）
#   ./deploy.sh --status       # 查看运行状态
#
# 构建策略（对齐 njmind 系列镜像的习惯）：
#   所有联网步骤（pnpm install/build、pip install）都在 docker run 的容器里做
#   ——docker run 的容器网络/DNS 正常；Dockerfile 只做纯 COPY，不在 build 里
#   联网（规避构建容器 DNS 依赖 daemon 启动时 resolv.conf 快照的坑）。
#   基础镜像用服务器已有：python:3.12-slim / nginx:stable-alpine（缺失时自动
#   从 daocloud 公共加速源补拉）。
#
# 流程：
#   ① 拉码（可跳） ②a run 容器构建前端 dist ②b run 容器装后端依赖 .deps
#   ③ 纯 COPY 打一体镜像 ④ 切换单容器 ⑤ 健康验证+失败自动回滚
# =============================================================================
set -euo pipefail

# ── 可配置项（环境变量覆盖）──────────────────────────────────────────────
IMAGE_PREFIX="${IMAGE_PREFIX:-llm-modeler}"      # 镜像名前缀（-backend/-frontend）
CONTAINER_NAME="${CONTAINER_NAME:-llm-modeler}"  # 一体容器名
HOST_PORT="${HOST_PORT:-28080}"                   # 宿主端口（宿主网关可反代 /ai-modeler）
DATA_VOLUME="${DATA_VOLUME:-llm-modeler-data}"   # 数据卷（conversations.db + checkpoint）
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
GIT_REMOTE="${GIT_REMOTE:-origin}"
GIT_BRANCH="${GIT_BRANCH:-main}"
# 基础镜像（服务器缺失时从该加速源补拉）：daocloud 是免认证公共代理
MIRROR="${MIRROR:-docker.m.daocloud.io/library}"
NODE_IMAGE="${NODE_IMAGE:-node:20-alpine}"
PY_IMAGE="${PY_IMAGE:-python:3.12-slim}"

# 工作目录 = 仓库根（.env/Dockerfile 所在处）：脚本位于 deploy/ 子目录时取上级
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "${SCRIPT_DIR}/../.env" ]; then
  WORKDIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
else
  WORKDIR="${SCRIPT_DIR}"
fi

log()  { printf '\033[32m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
err()  { printf '\033[31m[%s] ERROR\033[0m %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
usage() { sed -n '2,20p' "$0"; exit 1; }

# ── 子命令 ──────────────────────────────────────────────────────────────
case "${1:-}" in
  --status)
    docker ps -a --filter "name=${CONTAINER_NAME}" --format "table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}"
    docker images "${IMAGE_PREFIX}" --format "table {{.Repository}}:{{.Tag}}\t{{.CreatedAt}}" | head -8
    exit 0 ;;
esac

SKIP_PULL=0
for arg in "$@"; do
  case "$arg" in
    --skip-pull) SKIP_PULL=1 ;;
    -h|--help) usage ;;
    *) err "未知参数: $arg"; usage ;;
  esac
done

cd "$WORKDIR"
command -v docker >/dev/null || { err "docker 未安装"; exit 1; }
docker info >/dev/null 2>&1 || { err "docker daemon 未运行"; exit 1; }
[ -f .env ] || { err "缺少 .env（后端 LLM/上游配置）"; exit 1; }

# ── ① 拉码 ──────────────────────────────────────────────────────────────
if [ "$SKIP_PULL" -eq 0 ] && [ -d .git ]; then
  log "① 拉取代码（${GIT_REMOTE}/${GIT_BRANCH}）..."
  git fetch "$GIT_REMOTE" "$GIT_BRANCH" 2>/dev/null || true
  if [ -n "$(git status --porcelain)" ]; then
    log "   工作区有未提交改动，按当前工作区构建（dirty）"
    GIT_TAG="dirty-$(date '+%m%d%H%M')"
  else
    git pull --ff-only "$GIT_REMOTE" "$GIT_BRANCH" 2>/dev/null || true
    GIT_TAG="$(git rev-parse --short HEAD)"
  fi
else
  GIT_TAG="$(git rev-parse --short HEAD 2>/dev/null || echo manual)"
fi
log "   版本标签：${GIT_TAG}"

# ── ② 基础镜像自检（缺失自动从加速源补拉）─────────────────────────────
ensure_image() {
  local img="$1"
  if docker image inspect "$img" >/dev/null 2>&1; then return 0; fi
  log "   本地缺 ${img}，从 ${MIRROR} 补拉..."
  if docker pull "${MIRROR}/${img}"; then
    docker tag "${MIRROR}/${img}" "$img"
  else
    err "拉取 ${img} 失败（加速源不可达？），请手工准备该基础镜像"
    exit 1
  fi
}
ensure_image "$NODE_IMAGE"
ensure_image "$PY_IMAGE"
ensure_image nginx:stable-alpine

# ── ②a 前端构建（docker run 容器内 pnpm build，联网走容器网络）────────
log "②a 构建前端（node 容器内 pnpm build）..."
mkdir -p frontend/dist
docker run --rm \
  -v "$PWD/frontend:/app" -w /app \
  -e npm_config_registry=https://registry.npmmirror.com \
  "${NODE_IMAGE}" sh -c "
    npm config set registry https://registry.npmmirror.com &&
    (command -v pnpm >/dev/null || npm install -g pnpm) &&
    pnpm config set registry https://registry.npmmirror.com &&
    (pnpm install --frozen-lockfile || pnpm install) &&
    pnpm run build
  " 2>&1 | tail -3
[ -f frontend/dist/index.html ] || { err "前端构建失败（无 dist/index.html）"; exit 1; }
log "   dist 就绪（$(du -sh frontend/dist | awk '{print $1}')）"

# ── ②b 后端依赖（docker run 容器内 pip install -t，清华源）────────────
log "②b 安装后端依赖（python 容器内 pip -t）..."
rm -rf backend/.deps && mkdir -p backend/.deps
docker run --rm \
  -v "$PWD/backend:/app" -w /app \
  "${PY_IMAGE}" sh -c "
    pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
      -r requirements.txt -t /app/.deps
  " 2>&1 | tail -2
[ -d backend/.deps/fastapi ] || { err "后端依赖安装失败（.deps 无 fastapi）"; exit 1; }
log "   .deps 就绪（$(du -sh backend/.deps | awk '{print $1}')）"

# ── ②c nginx deb 预下载（docker run 容器内 apt --download-only）────────
# 运行镜像要装 nginx 但不在 docker build 里联网（DNS 坑）：容器内只下载不安装，
# 导出 deb 后由 Dockerfile COPY + dpkg -i 本地安装（零联网 build 的最后一环）
log "②c 预下载 nginx deb..."
rm -rf backend/debs && mkdir -p backend/debs
docker run --rm \
  -v "$PWD/backend:/app" -w /app \
  "${PY_IMAGE}" sh -c "
    apt-get update -qq &&
    apt-get install -y --download-only --no-install-recommends nginx curl >/dev/null &&
    cp /var/cache/apt/archives/*.deb /app/debs/
  "
[ "$(ls backend/debs/*.deb 2>/dev/null | wc -l)" -ge 1 ] || { err "nginx deb 下载失败"; exit 1; }
log "   debs 就绪（$(ls backend/debs | wc -l) 个包）"

# ── ③ 打镜像（纯 COPY，零联网 build）───────────────────────────────────
log "③ 打镜像（纯 COPY，单容器一体）..."
# dist 拷进后端构建上下文（Dockerfile.single 的 context 是 backend/，
# 一体镜像同时装 deps/src/dist，uvicorn 伺服 API + 前端静态）
rm -rf backend/dist && cp -r frontend/dist backend/dist
cp deploy/nginx-single.conf deploy/start-single.sh backend/
APP_IMAGE="${IMAGE_PREFIX}:${GIT_TAG}"
docker build -f deploy/Dockerfile.single -t "${APP_IMAGE}" -t "${IMAGE_PREFIX}:latest" backend/

# ── ④ 容器切换（记录旧镜像 → 删旧容器 → 网络起双容器）────────────────
OLD_IMAGE=$(docker inspect --format '{{.Config.Image}}' "${CONTAINER_NAME}" 2>/dev/null || true)

rollback() {
  err "部署失败，回滚上一版本..."
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  if [ -n "${OLD_IMAGE:-}" ] && docker image inspect "${OLD_IMAGE}" >/dev/null 2>&1; then
    start_container "${OLD_IMAGE}"
    log "已回滚到 ${OLD_IMAGE}"
  else
    err "无可用旧版本（首次部署失败）"
  fi
  exit 1
}

start_container() {
  docker rm -f "${CONTAINER_NAME}" >/dev/null 2>&1 || true
  # 单容器一体：nginx 伺服静态 + 反代 API（启动脚本后台拉 uvicorn）；
  # 数据卷持久化会话与 checkpoint
  docker run -d --name "${CONTAINER_NAME}" \
    --restart unless-stopped \
    -p "${HOST_PORT}:80" \
    --env-file .env \
    -v "${DATA_VOLUME}:/app/data" \
    "$1"
}

log "④ 切换容器（旧：${OLD_IMAGE:-无，首次部署} → 新：${APP_IMAGE}）..."
if ! start_container "${APP_IMAGE}"; then
  err "容器启动失败"
  rollback
fi

# ── ⑤ 健康验证（失败自动回滚）───────────────────────────────────────────
log "⑤ 健康探测（最长 ${HEALTH_TIMEOUT}s）..."
HEALTH_URL="http://127.0.0.1:${HOST_PORT}/ai-modeler/api/health"
elapsed=0
until curl -sf --max-time 3 "$HEALTH_URL" | grep -q '"status": *"healthy"'; do
  sleep 2; elapsed=$((elapsed + 2))
  if [ "$elapsed" -ge "$HEALTH_TIMEOUT" ]; then
    err "健康探测超时。最近日志："
    docker logs --tail 25 "${CONTAINER_NAME}" >&2 || true
    
    rollback
  fi
done
log "   健康就绪 ✓（${elapsed}s）"

log "发布完成："
log "   前端入口  http://<主机>:${HOST_PORT}/ai-modeler/"
log "   健康检查  ${HEALTH_URL}"
log "   日志      docker logs -f ${CONTAINER_NAME}"
log "   数据卷    ${DATA_VOLUME}（跨版本保留）"
[ -n "$OLD_IMAGE" ] && log "   回滚      旧镜像 ${OLD_IMAGE} 保留，可手动恢复"
