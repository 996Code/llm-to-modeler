"""进程内本地 embedding —— ONNX 推理(零外部服务,零 API 费用)。

【定位】LLMClient.embeddings 的 local 后端:与云端 API(text-embedding-v3)
同一调用契约(等长等序向量列表),调用方(KG 导入/检索)无感知切换。

【为什么是 onnxruntime 而不是 sentence-transformers】
- sentence-transformers 拉起 torch(~800MB 镜像增量)——为一个 543MB 的
  embedding 模型背这个体积不划算;onnxruntime + tokenizers + numpy
  合计 ~70MB,int8 量化模型 ~543MB,CPU 单条编码毫秒级
- 模型: Xenova/bge-m3 的 onnx 量化版(BAAI 旗舰多语言模型,1024 维,
  与云端 text-embedding-v3 维度一致,4096 token 上下文窗口)

【为什么选 bge-m3 而不是 bge-large-zh-v1.5】
- bge-large-zh-v1.5: 512 token 上限,我们的 chunk 目标 1200 字/max 3000 字,
  截断率 67%-87%,长篇小说段落后半段信息对向量检索完全不可见
- bge-m3: 4096 token 上限(实测 ONNX 支持),完整编码 3000 字 chunk 无截断;
  1024 维与云端一致,Milvus collection 无需重建;XLM-RoBERTa tokenizer,
  不需要 token_type_ids;543MB 比 bge-large 多 232MB,但换来了
  不做截断的完整语义表示

【模型分发】
- 生产环境: deploy.sh 在首次部署时从 hf-mirror 下载模型到
  deploy/models/embedding/,docker-compose 挂载到容器内
  /app/models/embedding/,EMBEDDING_MODEL_DIR 指向该路径
- 本地开发: 设置 EMBEDDING_MODEL_DIR 指向本地模型目录,或首次调用时
  从 HF_ENDPOINT(默认 hf-mirror)在线拉取,缓存到 ~/.cache/llm-embeddings

【线程安全】懒加载单例 + 双重检查锁;encode 内部 onnxruntime 自带
线程安全(一个 session 多线程共享)。
"""
import logging
import os
import threading
from typing import List, Optional

logger = logging.getLogger(__name__)

# 模型仓库(ONNX 转换版;官方 BAAI 仓库无 onnx 文件)
_MODEL_REPO = "Xenova/bge-m3"
# 量化版优先(~543MB,CPU 上比 fp32 快 2-3 倍,精度损失 <1%)
_ONNX_FILES = ["onnx/model_quantized.onnx", "onnx/model.onnx"]
_TOKENIZER_FILES = ["tokenizer.json", "config.json", "special_tokens_map.json", "tokenizer_config.json"]

_DIM = 1024  # bge-m3 与云端 text-embedding-v3 一致,切换后端无需重建集合


class _LocalEmbedder:
    """onnxruntime + tokenizers 的最小 embedding 推理器(进程级单例)。"""

    def __init__(self, model_dir: Optional[str] = None):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._np = np
        self._dir = model_dir or self._download_model()
        onnx_path = self._find_file("model_quantized.onnx") or self._find_file("model.onnx")
        if not onnx_path:
            raise RuntimeError(f"本地 embedding 模型缺失: {self._dir} 下无 onnx 文件")

        # CPU 推理;单线程足够(批内已并行在调用方),避免线程争用
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = int(os.getenv("EMBEDDING_CPU_THREADS", "2"))
        self._session = ort.InferenceSession(onnx_path, opts, providers=["CPUExecutionProvider"])
        self._tokenizer = Tokenizer.from_file(self._find_file("tokenizer.json") or "")
        self._tokenizer.no_padding()
        # bge-m3 最大 8194 token;这里取 4096 覆盖 3000 字 chunk 有余量
        # (中文 ~1.3 chars/token,3000 字 ≈ 2300 tokens,4096 有 78% 余量)
        self._max_len = 4096

    # ── 模型文件定位/下载 ─────────────────────────────────

    def _find_file(self, name: str) -> Optional[str]:
        for root, _, files in os.walk(self._dir):
            if name in files:
                return os.path.join(root, name)
        return None

    @staticmethod
    def _download_model() -> str:
        """从 HF 镜像拉模型(仅首次;构建期预下载的部署不会走到这)。"""
        cache = os.path.expanduser(
            os.getenv("EMBEDDING_CACHE_DIR", "~/.cache/llm-embeddings/bge-m3"))
        os.makedirs(cache, exist_ok=True)
        base = os.getenv("HF_ENDPOINT", "https://hf-mirror.com").rstrip("/")
        import urllib.request
        for rel in _ONNX_FILES[:1] + _TOKENIZER_FILES:  # 只拉量化的 + tokenizer
            dest = os.path.join(cache, rel)
            if os.path.exists(dest):
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            url = f"{base}/{_MODEL_REPO}/resolve/main/{rel}"
            logger.info(f"downloading embedding model: {rel}")
            urllib.request.urlretrieve(url, dest)
        return cache

    # ── 推理 ─────────────────────────────────────────────

    def encode(self, texts: List[str]) -> List[List[float]]:
        """批量编码:tokenize → onnx 前向 → CLS pooling → L2 归一化。

        bge-m3 是 XLM-RoBERTa 架构:仅需 input_ids + attention_mask,
        不需要 token_type_ids(与 bge-large-zh 的 BERT 架构不同)。
        官方 sentence-transformers 用 CLS pooling;这里按官方对齐。
        """
        np = self._np
        encodings = self._tokenizer.encode_batch(
            [t[:12000] for t in texts])  # 超长文本粗截(防 tokenize 慢;12000 字→~9000 tokens)
        input_ids = np.zeros((len(texts), self._max_len), dtype=np.int64)
        attention = np.zeros((len(texts), self._max_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            ids = enc.ids[:self._max_len]
            input_ids[i, :len(ids)] = ids
            attention[i, :len(ids)] = 1

        out = self._session.run(None, {
            "input_ids": input_ids, "attention_mask": attention,
        })
        hidden = out[0]  # (B, L, H)
        # CLS pooling(第 0 个 token)+ L2 归一化
        cls_vec = hidden[:, 0, :]
        norm = np.linalg.norm(cls_vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-12, norm)
        return (cls_vec / norm).tolist()


# ── 进程级单例(懒加载 + 双重检查锁) ──────────────────────

_embedder = None
_embedder_lock = threading.Lock()
_embedder_error: Optional[str] = None


def get_local_embedder():
    """取本地推理器单例;初始化失败记日志并抛(调用方降级纯图谱)。

    失败只尝试一次(记 _embedder_error 后直接复用错误)——模型缺失/
    依赖未装的部署里,每次导入都重试下载会拖垮任务。
    """
    global _embedder, _embedder_error
    if _embedder is not None:
        return _embedder
    if _embedder_error is not None:
        raise RuntimeError(_embedder_error)
    with _embedder_lock:
        if _embedder is None and _embedder_error is None:
            try:
                _embedder = _LocalEmbedder(
                    os.getenv("EMBEDDING_MODEL_DIR") or None)
                logger.info("local embedder ready (bge-m3, onnx int8, dim=1024, max_len=4096)")
            except Exception as e:
                _embedder_error = f"本地 embedding 初始化失败: {e}"
                logger.warning(_embedder_error)
                raise RuntimeError(_embedder_error)
    if _embedder is not None:
        return _embedder
    raise RuntimeError(_embedder_error)


def reset_local_embedder() -> None:
    """重置单例(测试用:换模型目录/环境后重新初始化)。"""
    global _embedder, _embedder_error
    with _embedder_lock:
        _embedder = None
        _embedder_error = None