#!/bin/bash
# llm-to-modler 一键部署脚本
# 功能:检查环境 → 拉取代码 → 检查配置 → 构建镜像 → 启动服务 → 健康检查
#
# 使用方式:
#   chmod +x deploy.sh
#   ./deploy.sh              # 首次部署或更新部署
#   ./deploy.sh logs         # 查看后端日志
#   ./deploy.sh logs frontend # 查看前端日志
#   ./deploy.sh restart      # 重启服务
#   ./deploy.sh stop         # 停止服务
#   ./deploy.sh status       # 查看状态

set -e

# ===== 配置 =====
PROJECT_NAME="llm-to-modler"
ENV_FILE=".env"
ENV_EXAMPLE="deploy.env.example"

# 颜色
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

log_info()  { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn()  { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }
log_step()  { echo -e "\n${CYAN}==== $1 ====${NC}"; }

# ===== 前置检查 =====
check_prerequisites() {
    log_step "前置检查"

    if ! command -v docker &> /dev/null; then
        log_error "未安装 Docker,请先安装"
        exit 1
    fi

    if ! docker compose version &> /dev/null; then
        log_error "Docker Compose 不可用,请确认 Docker 版本 >= 20.10(内置 compose)"
        exit 1
    fi

    log_info "Docker 环境检查通过 ✓"
}

# ===== 拉取最新代码 =====
pull_code() {
    log_step "拉取最新代码"

    if [ -d ".git" ]; then
        local branch=$(git rev-parse --abbrev-ref HEAD)
        log_info "当前分支:$branch"

        git fetch origin
        local local_hash=$(git rev-parse HEAD)
        local remote_hash=$(git rev-parse "origin/$branch")

        if [ "$local_hash" = "$remote_hash" ]; then
            log_info "代码已是最新,无需拉取 ✓"
        else
            log_info "拉取最新代码..."
            git pull origin "$branch"
            log_info "代码已更新至 $remote_hash ✓"
        fi
    else
        log_warn "当前目录不是 Git 仓库,跳过拉代码"
    fi
}

# ===== 检查环境变量 =====
check_env() {
    log_step "检查环境变量"

    if [ ! -f "$ENV_FILE" ]; then
        if [ -f "$ENV_EXAMPLE" ]; then
            log_warn "未找到 $ENV_FILE,从模板创建..."
            cp "$ENV_EXAMPLE" "$ENV_FILE"
            log_warn "请编辑 $ENV_FILE 填入真实配置后重新运行:"
            echo -e "  ${CYAN}vim .env && ./deploy.sh${NC}"
            exit 0
        else
            log_error "未找到 $ENV_FILE 和 $ENV_EXAMPLE,无法部署"
            exit 1
        fi
    fi

    # 必填项检查
    local missing=0
    for key in LLM_API_KEY NEO4J_PASSWORD; do
        local val=$(grep "^$key=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
        if [ -z "$val" ]; then
            log_warn "$key 未配置(在 $ENV_FILE 中)"
            missing=1
        fi
    done
    local np=$(grep "^NEO4J_PASSWORD=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    if [ "$np" = "change-me-in-production" ]; then
        log_warn "NEO4J_PASSWORD 还是模板默认值,请修改"
        missing=1
    fi

    if [ $missing -eq 1 ]; then
        log_error "必填配置缺失,退出(填好后重新执行 ./deploy.sh)"
        exit 1
    fi

    log_info "环境变量检查通过 ✓"
}

# ===== 构建镜像并启动 =====
build_and_start() {
    log_step "构建镜像并启动服务"

    log_info "构建镜像(首次较慢,前端 npm install + 后端 pip install)..."
    docker compose build

    log_info "启动服务(backend + frontend + neo4j + milvus)..."
    docker compose up -d

    log_info "服务已启动 ✓"
}

# ===== 健康检查 =====
health_check() {
    log_step "健康检查"

    local frontend_port=$(grep "^FRONTEND_PORT=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    frontend_port=${frontend_port:-19090}

    log_info "等待服务启动(milvus 首次初始化约 1~2 分钟)..."
    local max_retries=40
    local retry=0

    while [ $retry -lt $max_retries ]; do
        retry=$((retry + 1))
        local resp=$(curl -s -o /dev/null -w "%{http_code}" \
            "http://localhost:${frontend_port}/ai-modeler/health" 2>/dev/null)

        if [ "$resp" = "200" ]; then
            log_info "健康检查通过 ✓ (第 ${retry} 次尝试)"
            echo ""
            curl -s "http://localhost:${frontend_port}/ai-modeler/health"
            echo ""
            log_info "访问入口:"
            echo -e "  应用:      ${CYAN}http://<服务器IP>:${frontend_port}/ai-modeler/${NC}"
            echo -e "  管理端:    ${CYAN}http://<服务器IP>:${frontend_port}/ai-modeler/admin.html${NC}"
            echo -e "  嵌入演示:  ${CYAN}http://<服务器IP>:${frontend_port}/ai-modeler/embed-demo.html${NC}"
            echo -e "  知识图谱插件:管理端 → 插件 → knowledge_graph → 设置,填 Neo4j 密码(.env 里的 NEO4J_PASSWORD)"
            return 0
        fi

        echo -n "."
        sleep 3
    done

    echo ""
    log_error "健康检查失败,服务未在 ${max_retries} 次重试内就绪"
    log_warn "查看日志:./deploy.sh logs"
    log_warn "常见原因:.env 缺 LLM_API_KEY(后端能起但功能受限)/ milvus 还在初始化(再等等)"
    return 1
}

# ===== 监听日志 =====
follow_logs() {
    local svc=${2:-backend}
    log_step "监听 ${svc} 日志(Ctrl+C 退出)"
    docker compose logs -f --tail=100 "$svc"
}

# ===== 查看状态 =====
show_status() {
    log_step "服务状态"
    docker compose ps

    local frontend_port=$(grep "^FRONTEND_PORT=" "$ENV_FILE" 2>/dev/null | cut -d'=' -f2-)
    frontend_port=${frontend_port:-19090}
    echo ""
    log_info "健康检查:"
    curl -s "http://localhost:${frontend_port}/ai-modeler/health" 2>/dev/null \
        || log_warn "服务未响应"
    echo ""
}

# ===== 停止服务 =====
stop_services() {
    log_step "停止服务"
    docker compose down
    log_info "服务已停止 ✓ (数据保留在 ./data/ 下,不受影响)"
}

# ===== 重启服务 =====
restart_services() {
    log_step "重启服务"
    docker compose restart
    log_info "服务已重启 ✓"
}

# ===== 主流程 =====
main() {
    echo -e "${CYAN}================================${NC}"
    echo -e "${CYAN}  $PROJECT_NAME 一键部署${NC}"
    echo -e "${CYAN}================================${NC}"

    check_prerequisites

    case "${1:-deploy}" in
        deploy)
            pull_code
            check_env
            build_and_start
            health_check
            ;;
        logs)
            follow_logs "$@"
            ;;
        restart)
            restart_services
            ;;
        stop)
            stop_services
            ;;
        status)
            show_status
            ;;
        *)
            echo "用法: $0 {deploy|logs [service]|restart|stop|status}"
            exit 1
            ;;
    esac
}

main "$@"
