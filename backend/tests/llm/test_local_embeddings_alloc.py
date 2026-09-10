"""本地 embedding 推理的内存分配回归——17GB 注意力张量事故固化。

事故:encode() 按 _max_len=4096 定长分配输入矩阵,transformer 注意力
内存 O(B·heads·L²) —— B=16/L=4096 时仅分数张量 ~17GB,32G 主机 OOM,
整机卡死,任务线程阻塞在分配上(cancel 检查点都到不了)。
修复:按本批实际最大 token 数动态分配;向量语义不变(dim/归一化)。
"""
import unittest
from unittest.mock import MagicMock, patch

from llm import local_embeddings as le


class _FakeEnc:
    def __init__(self, n):
        self.ids = list(range(n))


class TestDynamicAllocation(unittest.TestCase):

    def _run_encode(self, id_counts, max_len=4096):
        """用假 session/onnx 跑 encode,返回 (输入矩阵形状, 向量)。"""
        emb = object.__new__(le._LocalEmbedder)
        emb._max_len = max_len

        import numpy as np
        emb._np = np
        emb._tokenizer = MagicMock()
        emb._tokenizer.encode_batch.return_value = [_FakeEnc(n) for n in id_counts]

        seen = {}

        def fake_run(_, feeds):
            seen["input_shape"] = feeds["input_ids"].shape
            B, L = feeds["input_ids"].shape
            # 模拟 hidden (B, L, 1024);CLS 位取前 4 维做可辨识向量
            out = np.zeros((B, L, 1024), dtype=np.float32)
            for b in range(B):
                out[b, 0, :4] = [b, 1.0, 0.0, 0.0]
            return [out]

        session = MagicMock()
        session.run = fake_run
        emb._session = session
        vectors = emb.encode([f"文本{i}" for i in id_counts])
        return seen["input_shape"], vectors, np

    def test_matrix_shrinks_to_actual_tokens(self):
        """16 段各 ~100 token:矩阵长度应是 100 级,不是 4096(17GB 事故)。"""
        shape, vectors, np = self._run_encode([100] * 16)
        self.assertEqual(shape, (16, 100))          # 动态长度,非 4096
        self.assertLess(shape[1], 4096)

    def test_caps_at_max_len(self):
        """超长段仍被截断到 max_len,矩阵不超界。"""
        shape, _, _ = self._run_encode([9000, 50])
        self.assertEqual(shape, (2, 4096))

    def test_vectors_normalized_and_dim_kept(self):
        """修复不改变输出语义:维度=1024、L2 归一化。"""
        _, vectors, np = self._run_encode([10, 20, 30])
        self.assertEqual(len(vectors), 3)
        for v in vectors:
            self.assertEqual(len(v), 1024)
            self.assertAlmostEqual(np.linalg.norm(v), 1.0, places=5)

    def test_empty_batch_safe(self):
        shape, vectors, _ = self._run_encode([])
        self.assertEqual(shape[0] if shape else 0, 0)
        self.assertEqual(vectors, [])


if __name__ == "__main__":
    unittest.main()
