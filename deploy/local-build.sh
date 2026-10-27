#!/usr/bin/env bash
# =============================================================================
# 本地构建 → 推镜像 → 服务器免构建切换(99.22 内存/磁盘受限专用流程)
#
# 动机:服务器 19G 内存(可用 ~3G)/磁盘 94%,在其上跑 pnpm+pip+docker
# build 既慢又与导入队列抢资源。本脚本把全部构建搬到开发机(Mac):
#   ① 前端 pnpm build(容器;JS 产物平台无关)
#   ② 后端依赖 pip -t + nginx deb 下载(容器;⚠ 必须 --platform
#      linux/amd64——Mac 是 ARM,pull 默认 arm64,装出的二进制在
#      x86 服务器上不可用)
#   ③ docker buildx --platform linux/amd64 打镜像
#   ④ docker save | gzip | ssh docker load(局域网传 ~300MB 压缩件)
#   ⑤ 最小同步 deploy.sh(镜像直启:服务器无源码树)
#   ⑥ 服务器上 SKIP_BUILD=1 IMAGE_TAG=<tag> bash deploy.sh(④⑤ 切换+健康验证)
#
# embedding 模型不打进镜像(服务器 data/models 已有,compose 卷挂载)。
# 用法: bash deploy/local-build.sh [--server root@192.168.99.22]
# =============================================================================
set -euo pipefail

SERVER="${SERVER:-root@192.168.99.22}"
SREPO="${SREPO:-/njmind/deploy/middle/llm-modeler-kg/repo}"
[ "$#" -gt 0 ] && [ "$1" != "--server" ] && { echo "用法: $0 [--server user@host]"; exit 1; }
[ "$#" -gt 1 ] && [ "$1" = "--server" ] && SERVER="$2"

WORKDIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$WORKDIR"
if [ -n "$(git status --porcelain)" ]; then
  printf '出包需要干净 worktree,请先提交当前改动\n' >&2
  exit 1
fi
IMAGE_NAME="${IMAGE_NAME:-llm-modeler}"
GIT_TAG="$(git rev-parse --short HEAD)-$(date '+%m%d%H%M')"
PLATFORM="linux/amd64"
PY_IMAGE_AMD64="python:3.12-slim-trixie-amd64"   # 钉 trixie+amd64(显式钉套件,防上游悄悄换版)

log()  { printf '\033[32m[%s]\033[0m %s\n' "$(date '+%H:%M:%S')" "$*"; }
err()  { printf '\033[31m[%s] ERROR\033[0m %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

# ── ⓪ amd64 基础镜像自检(缺则拉;Mac 上用别名单独存 amd64 版) ─────────
ensure_amd64() {
  docker image inspect "$PY_IMAGE_AMD64" >/dev/null 2>&1 && return 0
  log "⓪ 拉 python:3.12-slim-trixie(linux/amd64)..."
  docker pull --platform "$PLATFORM" python:3.12-slim-trixie \
    && docker tag python:3.12-slim-trixie "$PY_IMAGE_AMD64"
}
ensure_amd64

# ── ① 前端(宿主机直接构建:本机即 pnpm 开发环境,产物平台无关) ──────
log "① 构建前端(宿主 pnpm build → dist)..."
( cd frontend && pnpm run build ) 2>&1 | tail -3
[ -f frontend/dist/index.html ] || { err "前端构建失败"; exit 1; }

# ── ②a 后端依赖(amd64 容器 pip -t;缓存命中跳过) ─────────────────────
log "②a 后端依赖(pip -t, amd64)..."
REQ_SHA=$(sha256sum backend/requirements.txt | awk '{print $1}')
if [ "${FORCE_DEPS:-0}" != "1" ] && [ -d backend/.deps.amd64/fastapi ] \
   && [ "$(cat backend/.deps.amd64/.requirements.sha256 2>/dev/null)" = "$REQ_SHA" ]; then
  log "   .deps.amd64 缓存命中"
else
  rm -rf backend/.deps.amd64 && mkdir -p backend/.deps.amd64
  docker run --rm --platform "$PLATFORM" -v "$PWD/backend:/app" \
    -v llm_pip_cache_amd64:/root/.cache/pip -w /app \
    "$PY_IMAGE_AMD64" sh -c "
      pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt -t /app/.deps.amd64 &&
      rm -rf /app/.deps.amd64/cv2 /app/.deps.amd64/opencv_python-*.dist-info /app/.deps.amd64/opencv_python.libs &&
      pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
        --no-deps -t /app/.deps.amd64 opencv-python-headless
    " 2>&1 | tail -2
  ls -d backend/.deps.amd64/opencv_python_headless-*.dist-info >/dev/null 2>&1 \
    || { err "opencv-headless 覆盖失败"; exit 1; }
  echo "$REQ_SHA" > backend/.deps.amd64/.requirements.sha256
fi

# ── ②b nginx deb(amd64 容器 apt --download-only) ────────────────────
log "②b nginx deb..."
# 优先复用服务器上已验证的 deb(上一版镜像就是它们构建的);本地没有则
# scp 拉取——避开 apt(新底座 trixie 与 bookworm 源不兼容,国内源也救不了
# 套件错位)
if [ "${FORCE_DEBS:-0}" != "1" ] \
   && [ "$(ls backend/debs/*.deb 2>/dev/null | wc -l)" -ge 2 ]; then
  log "   debs 缓存命中"
elif ssh "$SERVER" "test -d $SREPO/backend/debs"; then
  log "   从服务器拉取已验证 deb..."
  mkdir -p backend/debs
  scp -q "$SERVER:$SREPO/backend/debs/*.deb" backend/debs/
  [ "$(ls backend/debs/*.deb 2>/dev/null | wc -l)" -ge 2 ] \
    || { err "服务器 deb 拉取失败"; exit 1; }
else
  rm -rf backend/debs && mkdir -p backend/debs
  ok=0
  for attempt in 1 2 3; do
    docker run --rm --platform "$PLATFORM" -v "$PWD/backend:/app" -w /app \
      "$PY_IMAGE_AMD64" sh -c "
        suite=. \$\$(. /etc/os-release && echo \'\$VERSION_CODENAME) &&
        rm -f /etc/apt/sources.list.d/*.sources &&
        echo \"deb https://mirrors.aliyun.com/debian/ \$suite main\" > /etc/apt/sources.list &&
        echo \"deb https://mirrors.aliyun.com/debian/ \$suite-updates main\" >> /etc/apt/sources.list &&
        apt-get update -qq &&
        apt-get install -y --download-only --no-install-recommends nginx curl >/dev/null &&
        cp /var/cache/apt/archives/*.deb /app/debs/
      " && { ok=1; break; }
    log "   第 ${attempt} 次失败,重试..."
  done
  [ "$ok" = "1" ] || { err "deb 下载失败"; exit 1; }
fi

# ── ③ 打镜像(buildx amd64;Dockerfile 读 backend/.deps → 先落到 .deps)──
log "③ 打镜像 ${IMAGE_NAME}:${GIT_TAG}(linux/amd64)..."
rm -rf backend/dist && cp -R frontend/dist backend/dist
rm -rf backend/.deps && cp -R backend/.deps.amd64 backend/.deps
docker buildx build --platform "$PLATFORM" \
  -f deploy/single/Dockerfile \
  -t "${IMAGE_NAME}:${GIT_TAG}" --load .
docker tag "${IMAGE_NAME}:${GIT_TAG}" "${IMAGE_NAME}:latest-local"

# ── ④ 推镜像到服务器(save|gzip|ssh load) ────────────────────────────
log "④ 推镜像到 ${SERVER}(save|gzip|ssh load)..."
docker save "${IMAGE_NAME}:${GIT_TAG}" | gzip \
  | ssh "$SERVER" "gunzip | docker load && docker tag ${IMAGE_NAME}:${GIT_TAG} ${IMAGE_NAME}:latest"

# ── ⑤ 最小同步(镜像直启:服务器只留 deploy.sh/compose/.env/data,
#    不再推源码树——2026-10-27 起服务器已清理 1.7G 源码,产物全在镜像)──
log "⑤ 同步 deploy.sh..."
rsync -az -e ssh deploy.sh "$SERVER:$SREPO/deploy.sh"

# ── ⑥ 服务器切换(SKIP_BUILD:只 ④⑤ 段——compose up+四层健康验证+回滚) ──
log "⑥ 服务器切换(SKIP_BUILD=1 IMAGE_TAG=${GIT_TAG})..."
ssh "$SERVER" "cd $SREPO && FORCE_DEPLOY=${FORCE_DEPLOY:-0} SKIP_BUILD=1 IMAGE_TAG=${GIT_TAG} bash deploy.sh"

log "⑦ 清理本次本地构建镜像(服务器副本保留用于回滚)..."
docker image rm "${IMAGE_NAME}:latest-local" "${IMAGE_NAME}:${GIT_TAG}"

log "完成: ${IMAGE_NAME}:${GIT_TAG} @ ${SERVER}"
