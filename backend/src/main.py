"""
FastAPI 应用入口模块。

本模块负责：
  1. 加载 .env 配置
  2. 配置日志（含凭证脱敏过滤器）
  3. 在 lifespan 中初始化所有核心组件（会话存储、上游客户端、LLM 客户端、
     LangGraph 图、MCP 服务），并挂到 app.state 供路由共享
  4. 创建 FastAPI app、注册中间件（CORS）和路由

LangGraph StateGraph 架构：
  - engine/graph.py: StateGraph 构建 + compile（图的装配）
  - engine/nodes.py: classify_intent / execute_tool / handle_result（图的节点）
  - engine/stream.py: graph.stream → SSE 桥接（图的流式输出）

核心设计（Java 视角）：
  - lifespan：类比 Spring 的 @PostConstruct / ServletContextListener。
    应用启动时跑一次（初始化资源），关闭时跑一次（清理资源）。
    比 startup/shutdown 事件更现代，FastAPI 推荐写法。
  - app.state：类比 Spring ApplicationContext（全局单例容器）。
    所有组件都挂在这里，路由通过 request.app.state.xxx 访问。
  - 依赖加载顺序很重要：先底层（数据库/上游），再上层（图/MCP）。

配置加载顺序（从项目根目录往上找 .env）：
  1. <repo>/backend/../.env（项目根的 .env，部署环境常用）
  2. <repo>/backend/.env（backend 目录的 .env，开发常用）
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（从 backend/ 向上查找项目根目录的 .env）
# resolve() 返回绝对路径，parent.parent.parent 从 src/ 往上三级到项目根
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if not _env_path.exists():
    # 兜底：项目根没有 .env，就用 backend/ 目录下的 .env
    _env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# 业务路由模块（各模块内部定义 router，这里集中挂载）
from src.api.config import router as config_router
from src.api.conversations import router as conversations_router
from src.api.health import router as health_router
from src.api.skills import router as skills_router
# LLM 客户端：调用 OpenAI 兼容 API（Qwen/本地模型）做意图识别和配置生成
from src.llm.client import LLMClient
# 会话存储：SQLite，以追加写事件流方式记录对话
from src.services.conversation_store import ConversationStore
# 上游客户端：调用 njmind-modeler（:7001）做校验/增删改/拉模板
from src.services.upstream_client import UpstreamClient

# 全局日志配置：INFO 级别，标准格式（时间-模块-级别-消息）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 挂载 RedactFilter 到 root logger(日志凭证脱敏)
# 必须在 basicConfig 之后、组件初始化之前安装，确保所有后续日志都经过过滤
from engine.logging_filter import install_redact_filter
install_redact_filter()  # 挂到 root logger,所有子 logger 继承


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理（启动初始化 + 关闭清理）。

    类比 Spring 的 @PostConstruct（yield 之前）+ @PreDestroy（yield 之后）。
    yield 把控制权交给应用运行期，应用退出后再执行 yield 之后的清理。

    初始化顺序（依赖链：底层 → 上层）：
      1. 数据库（会话存储）—— 最底层
      2. 上游客户端（HTTP，可能依赖日志通道）
      3. LLM 客户端（依赖日志通道）
      4. LangGraph 图（依赖上游、LLM、会话存储）
      5. MCP 服务（依赖上游、图）

    Args:
        app: FastAPI 应用实例，组件挂到 app.state 供路由访问。
    """
    logger.info("Starting LLM Form Modeler (LangGraph architecture)...")

    # Conversation store (SQLite, append-only 事件流)
    # DATABASE_PATH 未配置时默认 data/conversations.db（相对当前工作目录）
    db_path = os.getenv("DATABASE_PATH", "data/conversations.db")
    conv_store = ConversationStore(db_path)
    app.state.conversation_store = conv_store

    # 上游客户端：注入会话存储，用于持久化上游调用日志（调试/监控用）
    upstream = UpstreamClient(conversation_store=conv_store)
    app.state.upstream = upstream

    # 启动时探测上游可达性（不发 fatal，只 warning）
    # Fail-Open：上游不可达不阻断启动，因为前端还能浏览历史会话，
    # 只是生成/校验功能会失败
    if upstream.health_check():
        logger.info("Upstream njmind-modeler reachable")
    else:
        logger.warning("Upstream njmind-modeler NOT reachable - generation will fail")

    # LLM 客户端：注入会话存储，用于持久化 LLM 调用日志
    llm_client = LLMClient(conversation_store=conv_store)
    app.state.llm_client = llm_client

    # 新架构:LangGraph StateGraph
    # 自动发现和加载所有工具包
    # 延迟导入：这些模块依赖 app.state 之外的重型组件，延迟到启动时加载
    # 减少模块导入阶段的副作用，也让 lifespan 内的初始化顺序更清晰
    from domains import load_all_packs
    from engine.graph import build_graph
    from engine.conversation import ConversationManager
    from adapters.http_asset_client import HttpAssetClient

    # 自动扫描 domains/ 下的工具包，加载工具注册表和提示词模板
    registry, prompt_loader = load_all_packs()
    # 会话管理器：封装会话上下文（历史消息、压缩历史）的读写
    conversation_manager = ConversationManager(store=conv_store)
    # 资产客户端：拉取表单资产数据（字段元信息等），走上游接口
    asset_client = HttpAssetClient(upstream=upstream)
    # asset_client 的数据操作 base_url 从环境变量 ASSET_BASE_URL 读取,默认 mock API

    # 构建 LangGraph StateGraph(替代旧 ToolDispatcher)
    # build_graph 装配节点（classify_intent/execute_tool/handle_result）并编译
    graph = build_graph(
        registry=registry,
        llm_client=llm_client,
        asset_client=asset_client,
        conversation=conversation_manager,
        prompt_loader=prompt_loader,
    )
    app.state.graph = graph
    logger.info("LangGraph StateGraph architecture initialized")

    # MCP Server(使用 LangGraph StateGraph)
    # MCP（Model Context Protocol）：让外部 AI 客户端（如 Claude Code）能调用本服务工具
    from src.mcp_server import create_mcp_server
    mcp_server = create_mcp_server(upstream, graph=graph)
    app.state.mcp = mcp_server
    # 把 MCP 的 Streamable HTTP 应用挂到 /mcp 子路径
    app.mount("/mcp", mcp_server.streamable_http_app())

    logger.info("LLM Form Modeler started")
    yield  # 此处之后应用开始对外服务，直到收到关闭信号

    # === 关闭阶段（@PreDestroy 等价物）===
    logger.info("Shutting down...")
    upstream.close()  # 关闭 httpx 连接池，释放 socket


# 创建 FastAPI 应用，lifespan 绑定生命周期
# version 会暴露在 app.version，health.py 和 root 接口直接读取
app = FastAPI(
    title="LLM Form Modeler",
    description="Natural language to form config generator (bridge to njmind-modeler)",
    version="0.4.0",
    lifespan=lifespan,
)

# CORS 中间件：允许所有来源跨域（开发期宽松，生产应收敛 allow_origins）
# 注意：allow_origins=["*"] + allow_credentials=True 在浏览器规范上互斥，
#       但 FastAPI/Starlette 会自动处理（回显具体 origin 而非 *），不会报错
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册各业务路由（顺序不影响路由匹配，FastAPI 按精确路径优先）
app.include_router(health_router)
app.include_router(config_router)
app.include_router(skills_router)
app.include_router(conversations_router)


if __name__ == "__main__":
    # 直接 python main.py 运行时的入口（开发用）
    # reload=True：代码改动自动重启，配合 LangGraph 调试很方便
    # 生产一般用 uvicorn 命令行启动，不走这里
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=18080, reload=True)
