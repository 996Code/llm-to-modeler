"""嵌入(embedding)口径单一事实源 —— 全平台唯一的 env 解析点。

【定死能力】(用户裁决,沿用 10-15 "本地 bge-m3 定死"方向收严):
  - 后端:EMBEDDING_BACKEND(local 默认 | api)。仅此一个 env 开关。
  - 模型:local 恒 bge-m3(dim=1024 定死,不接受 env 改写);
    api 后端读 LLM_EMBED_MODEL(未配置 = 启动/调用期报错,不静默回退)。

此前这套解析散在 llm/client.py(3 处)与 domains/knowledge_graph/tasks.py
(3 处)各写各的,口径漂移过(建库预填与导入校验不同源会误触模型一致性
降级)——全部收敛到这里,消费方只调用,不再各自读 env。
"""
import os

LOCAL_EMBED_MODEL = "bge-m3"
LOCAL_EMBED_DIM = 1024


def embed_backend() -> str:
    """当前嵌入后端:local(默认) | api。"""
    return os.getenv("EMBEDDING_BACKEND", "local").strip().lower() or "local"


def embed_model() -> str:
    """当前嵌入模型名(local 恒 bge-m3;api 取 LLM_EMBED_MODEL)。

    KG 向量索引指纹/库的 embedding_model 记录/模型一致性校验都用它。
    """
    if embed_backend() == "local":
        return LOCAL_EMBED_MODEL
    return os.getenv("LLM_EMBED_MODEL", "").strip() or LOCAL_EMBED_MODEL


def embed_model_or_raise() -> str:
    """api 后端强校验版:未配置直接抛(调用方降级,不静默换模型)。"""
    if embed_backend() == "local":
        return LOCAL_EMBED_MODEL
    model = os.getenv("LLM_EMBED_MODEL", "").strip()
    if not model:
        raise RuntimeError(
            "EMBEDDING_BACKEND=api 且 LLM_EMBED_MODEL 未配置,向量能力不可用"
            "——请配置后重试(本地后端无需该配置)")
    return model


def embed_cache_label() -> str:
    """嵌入缓存键的模型标签:local 用常量(local/bge-m3);api 用模型名。

    与观测端点 embeddings:local/bge-m3 同源;不用 chat 模型名——切
    chat 模型不应冲掉嵌入缓存。
    """
    if embed_backend() == "local":
        return f"local/{LOCAL_EMBED_MODEL}"
    return embed_model()
