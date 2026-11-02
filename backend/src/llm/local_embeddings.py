"""进程内 bge-m3 embedding：ONNX + tokenizers 推理，无外部 API。

模型固定 Xenova/bge-m3 ONNX 量化版，输出 1024 维向量，编码长度
上限 4096 token；查询与文档均不添加 instruction 前缀。

部署脚本准备 data/models/embedding/，Compose 挂载到
/app/models/embedding/。裸机通过 EMBEDDING_MODEL_DIR 指定模型目录，
或从 HF_ENDPOINT 下载固定模型并缓存到 EMBEDDING_CACHE_DIR。

进程级懒加载单例由锁保护；ONNX session 可在多线程间共享。
"""
import logging
import os
import threading
from typing import List, Optional

from sdk.embedding import LOCAL_EMBED_DIM as _DIM

logger = logging.getLogger(__name__)

# 模型仓库(ONNX 转换版;官方 BAAI 仓库无 onnx 文件)
_MODEL_REPO = "Xenova/bge-m3"
# 量化版优先(~543MB,CPU 上比 fp32 快 2-3 倍,精度损失 <1%)
_ONNX_FILES = ["onnx/model_quantized.onnx", "onnx/model.onnx"]
_TOKENIZER_FILES = ["tokenizer.json", "config.json", "special_tokens_map.json", "tokenizer_config.json"]


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
        """从 HF 镜像拉模型(仅首次;构建期预下载的部署不会走到这)。

        带超时与进度日志:urllib 默认无超时,镜像限速时 543MB 能挂几十
        分钟且无任何输出——表现即"导入批次收尾卡死"(线上事故)。
        """
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

            def _progress(blocks, bs, total):
                if blocks % 200 == 0 and total > 0:
                    logger.info(f"  {rel}: {blocks * bs // 1024 // 1024}MB / {total // 1024 // 1024}MB")

            tmp = dest + ".part"
            urllib.request.urlretrieve(url, tmp, reporthook=_progress)
            os.replace(tmp, dest)
        return cache

    # ── 推理 ─────────────────────────────────────────────

    def encode(self, texts: List[str]) -> List[List[float]]:
        """批量编码:tokenize → onnx 前向 → CLS pooling → L2 归一化。

        bge-m3 是 XLM-RoBERTa 架构:仅需 input_ids + attention_mask,
        不需要 token_type_ids(与 bge-large-zh 的 BERT 架构不同)。
        官方 sentence-transformers 用 CLS pooling;这里按官方对齐。
        """
        np = self._np
        import time as _time
        _t0 = _time.monotonic()
        encodings = self._tokenizer.encode_batch(
            [t[:12000] for t in texts])  # 超长文本粗截(防 tokenize 慢;12000 字→~9000 tokens)
        _t_tok = _time.monotonic()
        # 【动态长度分配】按本批实际最大 token 数分配矩阵,而不是固定
        # _max_len——transformer 注意力内存是 O(B·heads·L²):B=16、L=4096
        # 时仅分数张量就 ~17GB,32G 主机直接 OOM/整机卡死(线上事故,
        # 表现为"批次收尾假卡死 + cancel 无响应")。1200 字段 ≈1500 token,
        # 动态分配后单批峰值降到 ~2.7GB(16 段)乃至百 MB 级(8 段)。
        batch_len = min(self._max_len,
                        max((min(len(e.ids), self._max_len) for e in encodings), default=1))
        input_ids = np.zeros((len(texts), batch_len), dtype=np.int64)
        attention = np.zeros((len(texts), batch_len), dtype=np.int64)
        for i, enc in enumerate(encodings):
            ids = enc.ids[:batch_len]
            input_ids[i, :len(ids)] = ids
            attention[i, :len(ids)] = 1

        out = self._session.run(None, {
            "input_ids": input_ids, "attention_mask": attention,
        })
        # 慢推理可观测:批耗时/条数/tokenize 占比——CPU 推理是向量写入
        # 的主要耗时,无日志时表现为"批次收尾长时间无输出"(线上误判卡死)
        _t_all = _time.monotonic() - _t0
        if _t_all > 3:
            logger.info(f"embed slow: {len(texts)} 段 {_t_all:.1f}s"
                        f"(tokenize {_t_tok - _t0:.1f}s, infer {_t_all - (_t_tok - _t0):.1f}s,"
                        f"batch_len={batch_len})")
        hidden = out[0]  # (B, L, H)
        # CLS pooling(第 0 个 token)+ L2 归一化
        cls_vec = hidden[:, 0, :]
        norm = np.linalg.norm(cls_vec, axis=1, keepdims=True)
        norm = np.where(norm == 0, 1e-12, norm)
        return (cls_vec / norm).tolist()

    def encode_queries(self, texts: List[str]) -> List[List[float]]:
        """bge-m3 查询侧直接编码原文,不添加 instruction。"""
        return self.encode(texts)


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