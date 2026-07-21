"""
FastAPI Main Application.

LangGraph StateGraph 架构:
- engine/graph.py: StateGraph 构建 + compile
- engine/nodes.py: classify_intent / execute_tool / handle_result
- engine/stream.py: graph.stream → SSE 桥接
"""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# 加载 .env 文件（从 backend/ 向上查找项目根目录的 .env）
_env_path = Path(__file__).resolve().parent.parent.parent / ".env"
if not _env_path.exists():
    _env_path = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_env_path)

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.config import router as config_router
from src.api.conversations import router as conversations_router
from src.api.health import router as health_router
from src.api.skills import router as skills_router
from src.llm.client import LLMClient
from src.services.conversation_store import ConversationStore
from src.services.upstream_client import UpstreamClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# 挂载 RedactFilter 到 root logger(日志凭证脱敏)
from engine.logging_filter import install_redact_filter
install_redact_filter()  # 挂到 root logger,所有子 logger 继承


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting LLM Form Modeler (LangGraph architecture)...")

    # Conversation store (SQLite, append-only 事件流)
    db_path = os.getenv("DATABASE_PATH", "data/conversations.db")
    conv_store = ConversationStore(db_path)
    app.state.conversation_store = conv_store

    upstream = UpstreamClient(conversation_store=conv_store)
    app.state.upstream = upstream

    if upstream.health_check():
        logger.info("Upstream njmind-modeler reachable")
    else:
        logger.warning("Upstream njmind-modeler NOT reachable - generation will fail")

    llm_client = LLMClient(conversation_store=conv_store)
    app.state.llm_client = llm_client

    # 新架构:LangGraph StateGraph
    # 自动发现和加载所有工具包
    from domains import load_all_packs
    from engine.graph import build_graph
    from engine.conversation import ConversationManager
    from adapters.http_asset_client import HttpAssetClient

    registry, prompt_loader = load_all_packs()
    conversation_manager = ConversationManager(store=conv_store)
    asset_client = HttpAssetClient(upstream=upstream)
    # asset_client 的数据操作 base_url 从环境变量 ASSET_BASE_URL 读取,默认 mock API

    # 构建 LangGraph StateGraph(替代旧 ToolDispatcher)
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
    from src.mcp_server import create_mcp_server
    mcp_server = create_mcp_server(upstream, graph=graph)
    app.state.mcp = mcp_server
    app.mount("/mcp", mcp_server.streamable_http_app())

    logger.info("LLM Form Modeler started")
    yield

    logger.info("Shutting down...")
    upstream.close()


app = FastAPI(
    title="LLM Form Modeler",
    description="Natural language to form config generator (bridge to njmind-modeler)",
    version="0.4.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health_router)
app.include_router(config_router)
app.include_router(skills_router)
app.include_router(conversations_router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host="0.0.0.0", port=18080, reload=True)
