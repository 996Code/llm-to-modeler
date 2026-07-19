"""
FastAPI Main Application.
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
    logger.info("Starting LLM Form Modeler (new architecture)...")

    upstream = UpstreamClient()
    app.state.upstream = upstream

    if upstream.health_check():
        logger.info("Upstream njmind-modeler reachable")
    else:
        logger.warning("Upstream njmind-modeler NOT reachable - generation will fail")

    llm_client = LLMClient()
    app.state.llm_client = llm_client

    # Conversation store (SQLite, append-only 事件流)
    db_path = os.getenv("DATABASE_PATH", "data/conversations.db")
    conv_store = ConversationStore(db_path)
    app.state.conversation_store = conv_store

    # 新架构:ToolDispatcher
    from domains.njmind_form.pack import create_registry, create_prompt_loader
    from engine.dispatcher import ToolDispatcher
    from engine.conversation import ConversationManager
    from adapters.http_asset_client import HttpAssetClient

    registry = create_registry()
    prompt_loader = create_prompt_loader()
    conversation_manager = ConversationManager(store=conv_store)
    asset_client = HttpAssetClient(upstream=upstream)

    dispatcher = ToolDispatcher(
        registry=registry,
        llm_client=llm_client,
        conversation_store=conversation_manager,
        prompt_loader=prompt_loader,
        asset_client=asset_client,
    )
    app.state.dispatcher = dispatcher
    logger.info("New architecture (ToolDispatcher) initialized")

    # MCP Server(使用新架构 dispatcher)
    from src.mcp_server import create_mcp_server
    mcp_server = create_mcp_server(upstream, dispatcher)
    app.state.mcp = mcp_server
    app.mount("/mcp", mcp_server.streamable_http_app())

    logger.info("LLM Form Modeler started")
    yield

    logger.info("Shutting down...")
    upstream.close()


app = FastAPI(
    title="LLM Form Modeler",
    description="Natural language to form config generator (bridge to njmind-modeler)",
    version="0.3.0",
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
