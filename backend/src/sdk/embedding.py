"""固定的本地嵌入能力及缓存标签,全平台共用。"""

LOCAL_EMBED_MODEL = "bge-m3"
LOCAL_EMBED_DIM = 1024


def embed_model() -> str:
    """向量索引指纹与模型一致性校验共用的模型名。"""
    return LOCAL_EMBED_MODEL


def embed_cache_label() -> str:
    """嵌入缓存独立于 chat 模型与供应商配置。"""
    return f"local/{LOCAL_EMBED_MODEL}"
