#!/bin/bash
# ── 开发环境启动脚本 ──────────────────────────────────────────
# 同时启动 Mock API Server 和 Backend Server
#
# 用法:
#   ./start_dev.sh              # 前台启动(Ctrl+C 停止全部)
#   ./start_dev.sh --mock-only  # 只启动 Mock API
#   ./start_dev.sh --backend-only  # 只启动 Backend
#
# 环境变量:
#   ASSET_BASE_URL  — 上游 API 地址(默认 http://localhost:19999)
#   MOCK_PORT       — Mock API 端口(默认 19999)
#   BACKEND_PORT    — Backend 端口(默认 18080)
# ──────────────────────────────────────────────────────────────

set -e

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 默认配置
MOCK_PORT=${MOCK_PORT:-19999}
BACKEND_PORT=${BACKEND_PORT:-18080}
ASSET_BASE_URL=${ASSET_BASE_URL:-"http://localhost:${MOCK_PORT}"}
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="${SCRIPT_DIR}/backend"
MOCK_LOG="/tmp/mock-api.log"
BACKEND_LOG="/tmp/backend.log"

# 解析参数
START_MOCK=true
START_BACKEND=true
case "${1:-}" in
  --mock-only)     START_BACKEND=false ;;
  --backend-only)  START_MOCK=false ;;
  --help|-h)
    echo "用法: $0 [--mock-only|--backend-only|--help]"
    echo ""
    echo "  --mock-only      只启动 Mock API Server"
    echo "  --backend-only   只启动 Backend Server"
    echo "  --help           显示帮助"
    exit 0
    ;;
esac

# 清理函数:退出时杀掉所有子进程
cleanup() {
  log_warn "正在停止所有服务..."
  if [ -n "$MOCK_PID" ]; then
    kill "$MOCK_PID" 2>/dev/null || true
    log_info "Mock API (PID $MOCK_PID) 已停止"
  fi
  if [ -n "$BACKEND_PID" ]; then
    kill "$BACKEND_PID" 2>/dev/null || true
    log_info "Backend (PID $BACKEND_PID) 已停止"
  fi
  exit 0
}
trap cleanup EXIT INT TERM

# ── 启动 Mock API Server ──
if [ "$START_MOCK" = true ]; then
  log_info "启动 Mock API Server (端口 ${MOCK_PORT})..."
  log_info "  日志: ${MOCK_LOG}"
  cd "$BACKEND_DIR"
  python3 mock_api_server.py --port "$MOCK_PORT" > "$MOCK_LOG" 2>&1 &
  MOCK_PID=$!
  log_info "Mock API PID: ${MOCK_PID}"

  # 等待 Mock API 就绪
  for i in $(seq 1 10); do
    if curl -s "http://localhost:${MOCK_PORT}/health" > /dev/null 2>&1; then
      log_info "Mock API 就绪 ✓"
      break
    fi
    # 尝试任意路径(万能接口)
    if curl -s "http://localhost:${MOCK_PORT}/ping" > /dev/null 2>&1; then
      log_info "Mock API 就绪 ✓"
      break
    fi
    sleep 0.5
  done
fi

# ── 启动 Backend Server ──
if [ "$START_BACKEND" = true ]; then
  # 设置环境变量
  export ASSET_BASE_URL
  log_info "ASSET_BASE_URL=${ASSET_BASE_URL}"

  log_info "启动 Backend Server (端口 ${BACKEND_PORT})..."
  log_info "  日志: ${BACKEND_LOG}"
  cd "$BACKEND_DIR"
  python3 -m uvicorn src.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload > "$BACKEND_LOG" 2>&1 &
  BACKEND_PID=$!
  log_info "Backend PID: ${BACKEND_PID}"

  # 等待 Backend 就绪
  for i in $(seq 1 20); do
    if curl -s "http://localhost:${BACKEND_PORT}/api/health" > /dev/null 2>&1; then
      log_info "Backend 就绪 ✓"
      break
    fi
    if [ $i -eq 20 ]; then
      log_warn "Backend 启动超时,请检查日志: ${BACKEND_LOG}"
    fi
    sleep 1
  done
fi

# ── 状态汇总 ──
echo ""
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo -e "${CYAN}  开发环境已启动${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
if [ "$START_MOCK" = true ]; then
  echo -e "  Mock API:   ${GREEN}http://localhost:${MOCK_PORT}${NC}  (日志: ${MOCK_LOG})"
fi
if [ "$START_BACKEND" = true ]; then
  echo -e "  Backend:    ${GREEN}http://localhost:${BACKEND_PORT}${NC}  (日志: ${BACKEND_LOG})"
  echo -e "  API Docs:   ${GREEN}http://localhost:${BACKEND_PORT}/docs${NC}"
fi
echo ""
echo -e "  按 ${YELLOW}Ctrl+C${NC} 停止所有服务"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo ""

# 前台等待(显示日志)
if [ "$START_MOCK" = true ] && [ "$START_BACKEND" = true ]; then
  # 同时显示两个日志
  tail -f "$MOCK_LOG" "$BACKEND_LOG" 2>/dev/null || wait
elif [ "$START_MOCK" = true ]; then
  tail -f "$MOCK_LOG" 2>/dev/null || wait
elif [ "$START_BACKEND" = true ]; then
  tail -f "$BACKEND_LOG" 2>/dev/null || wait
fi
