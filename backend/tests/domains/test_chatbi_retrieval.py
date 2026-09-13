"""chatbi 检索栈单测 —— 两阶段召回 / few-shot 命中 / 索引构建与增量更新。

覆盖(移植自 chat-bi retriever/indexer/indexer_update/fewshot 的核心行为):
  - 两阶段召回:向量召回(top_k + 阈值) → LLM 精筛(命中/降级/宁缺毋滥/数组容错)
    → schema_context 构建;
  - few-shot:回流索引 / 相似命中 / prompt 格式化 / 阈值与跨源隔离 / PG 持久化;
  - 索引:model+metric 分类建索引 / 批量 embed 降级 / 幂等重建(删旧建新,
    few-shot 分区不受波及) / scope 登记与多租户删除。

测试替身(单测不连 Milvus/LLM):
  - FakeLLM:chat/chat_json/embeddings 可编程;embeddings 按文本映射返回
    假向量(与 tests/domains/test_knowledge_graph_m3.MockLLM 同模式);
  - FakeSdkVector:SDK VectorStore Protocol 的内存实现,余弦相似度语义
    对齐 chat-bi MockVectorStore(同 tests FakeVector 模式 + search)。
"""
import hashlib
import json
from datetime import datetime, timezone

import pytest

from domains.chatbi.fewshot import (
    FewShotExample,
    find_fewshot_examples,
    format_fewshot_prompt,
    index_fewshot_example,
)
from domains.chatbi.indexing import (
    build_index,
    metric_to_text,
    model_to_text,
    rebuild_index,
)
from domains.chatbi.models import (
    Column,
    Metric,
    Model,
    Relationship,
    SemanticModelContent,
)
from domains.chatbi.retrieval import (
    DEFAULT_SCORE_THRESHOLD,
    DEFAULT_TOP_K,
    build_metrics_hint,
    build_schema_context,
    extract_allowed_columns,
    parse_json_response,
    retrieve,
    retrieve_context,
)
from domains.chatbi.stores import (
    DOC_FEWSHOT,
    DOC_SCHEMA,
    ChatBIEmbedder,
    ChatBIVectorStore,
    delete_fewshot_examples,
    get_db,
    get_or_create_scope,
    get_scope,
    list_fewshot_examples,
    list_scopes,
    reset_caches,
    resolve_scopes,
)
from sdk.scope_registry import is_scope_id_safe, registered_prefixes

DS1 = "ds-orders-0001"
DS2 = "ds-weather-0002"

# ── 假向量(8 维,语义方向正交;阈值 0.35 下可分)──────────────────────
V_ORDERS = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
V_USERS = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
V_GMV = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
V_WEATHER = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]
# 问"订单总金额" → 与 orders(0.67)/gmv(0.74) 同向,与 users 正交
Q_ORDER = [0.9, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
Q_WEATHER = list(V_WEATHER)
Q_NONE = [0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0]   # 与所有对象正交

Q_ORDER_TEXT = "上个月订单总金额是多少"
Q_WEATHER_TEXT = "今天天气怎么样"
Q_FEWSHOT_TEXT = "上个月的订单总数"

_VEC_BY_NAME = {"orders": V_ORDERS, "users": V_USERS, "gmv": V_GMV,
                "weather": V_WEATHER}


# ── 测试替身 ─────────────────────────────────────────────────

def _cosine_similarity(a, b):
    """余弦相似度 [-1, 1],零向量 → 0(自 chat-bi MockVectorStore 移植)。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class FakeLLM:
    """可编程 LLM 替身:embeddings 按文本映射返回假向量,chat 脚本化精筛结果。"""

    def __init__(self, default_vector=None):
        self.vector_map = {}          # 精确文本 → 向量
        self.default_vector = default_vector or list(V_ORDERS)
        self.embed_error = None       # 注入 embed 异常
        self.embed_calls = []         # [(texts, stage)]
        self.chat_error = None        # 注入 chat 异常
        self.refine_result = ""       # chat 返回文本(精筛脚本)
        self.chat_calls = []          # [{prompt, temperature, stage, conv_id}]
        self.chat_json_result = {}
        self.chat_json_calls = []

    def embeddings(self, texts, conv_id=None, stage=None):
        self.embed_calls.append((list(texts), stage))
        if self.embed_error is not None:
            raise self.embed_error
        return [list(self.vector_map.get(t, self.default_vector)) for t in texts]

    def chat(self, messages, temperature=None, max_tokens=None,
             conv_id=None, stage=None, model=None):
        self.chat_calls.append({
            "prompt": messages[0]["content"], "temperature": temperature,
            "stage": stage, "conv_id": conv_id,
        })
        if self.chat_error is not None:
            raise self.chat_error
        return self.refine_result

    def chat_json(self, messages, temperature=None, conv_id=None,
                  stage=None, model=None):
        self.chat_json_calls.append({"prompt": messages[0]["content"], "stage": stage})
        if self.chat_error is not None:
            raise self.chat_error
        return self.chat_json_result


class FakeSdkVector:
    """SDK VectorStore Protocol 内存替身(余弦相似度,同源 MockVectorStore 语义)。

    覆盖 chatbi 适配层用到的全方法集:ensure_collection / upsert_chunks /
    delete_by_doc / existing_chunk_ids / search / count / drop_collection。
    """

    def __init__(self):
        self.collections = {}   # scope -> dim
        self.rows = {}          # scope -> {chunk_id: item}
        self.fail_upsert = None
        self.fail_delete = None

    def ensure_collection(self, scope, dim):
        self.collections.setdefault(scope, dim)

    def drop_collection(self, scope):
        self.collections.pop(scope, None)
        self.rows.pop(scope, None)

    def upsert_chunks(self, scope, items):
        if self.fail_upsert is not None:
            raise RuntimeError(self.fail_upsert)
        self.rows.setdefault(scope, {}).update(
            {i["chunk_id"]: dict(i) for i in items})
        return len(items)

    def delete_by_doc(self, scope, doc_id):
        if self.fail_delete is not None:
            raise RuntimeError(self.fail_delete)
        self.rows[scope] = {k: v for k, v in self.rows.get(scope, {}).items()
                            if v["doc_id"] != doc_id}

    def existing_chunk_ids(self, scope, doc_id):
        return {k for k, v in self.rows.get(scope, {}).items()
                if v["doc_id"] == doc_id}

    def search(self, scope, query_vector, top_k=5, doc_id=None):
        # SDK 语义:doc_id 过滤 + 余弦降序 + top_k(阈值由适配层后置过滤)
        scored = []
        for item in self.rows.get(scope, {}).values():
            if doc_id and item["doc_id"] != doc_id:
                continue
            scored.append({
                "chunkId": item["chunk_id"], "docId": item["doc_id"],
                "seq": item["seq"], "text": item["text"],
                "score": _cosine_similarity(query_vector, item["vector"]),
            })
        scored.sort(key=lambda h: h["score"], reverse=True)
        return scored[:top_k]

    def count(self, scope):
        return len(self.rows.get(scope, {}))

    def close(self):
        pass


# ── fixtures 与构造辅助 ──────────────────────────────────────

@pytest.fixture()
def db():
    """pack 关系库(单例重建 + 幂等建表;数据由 conftest 每测试清表)。"""
    reset_caches()
    yield get_db()
    reset_caches()


def _make_ds(db, ds_id, name):
    """插一条数据源行(scope 登记的宿主)。"""
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_data_sources (id, name, db_type, host, port, "
            "database, username, encrypted_password, is_active, created_at, "
            "updated_at) VALUES (?, ?, 'postgresql', 'h', 5432, 'd', 'u', 'x', "
            "1, ?, ?)",
            (ds_id, name, now, now),
        )


def _make_content(with_users=True):
    """语义层样例:orders(3 列 + gmv 指标) + users。"""
    orders = Model(
        name="orders", display_name="订单表", description="订单主表",
        columns=[
            Column(name="id", display_name="主键", data_type="BIGINT"),
            Column(name="user_id", display_name="用户", data_type="BIGINT"),
            Column(name="total_amount", display_name="订单金额",
                   data_type="DECIMAL"),
        ],
        metrics=[Metric(name="gmv", display_name="成交总额",
                        formula="SUM(total_amount)",
                        condition="status IN ('paid','shipped')")],
    )
    models = [orders]
    if with_users:
        models.append(Model(
            name="users", display_name="用户表", description="用户主表",
            columns=[
                Column(name="id", display_name="主键", data_type="BIGINT"),
                Column(name="nickname", display_name="昵称",
                       data_type="VARCHAR"),
            ],
        ))
    return SemanticModelContent(models=models)


def _make_weather_content():
    """第二个数据源的语义层(跨源隔离/合并召回用)。"""
    return SemanticModelContent(models=[Model(
        name="weather", display_name="天气表", description="天气记录表",
        columns=[Column(name="city", display_name="城市",
                        data_type="VARCHAR")],
    )])


def _register_vectors(llm, content, extra=None):
    """把假向量按"精确文本"注册进 FakeLLM(键 = model/metric 的 embed 文本)。"""
    for m in content.models:
        llm.vector_map[model_to_text(m)] = list(_VEC_BY_NAME[m.name])
        for mt in m.metrics:
            llm.vector_map[metric_to_text(mt)] = list(_VEC_BY_NAME[mt.name])
    llm.vector_map.update(extra or {})


def _setup(llm=None):
    """标准三件套:适配后的向量库 + embedder + 语义层(向量已注册)。"""
    llm = llm or FakeLLM()
    store = ChatBIVectorStore(FakeSdkVector())
    embedder = ChatBIEmbedder(llm)
    content = _make_content()
    _register_vectors(llm, content)
    # 查询问题文本 → 假向量(订单问题与 orders/gmv 同向;天气问题正交)
    llm.vector_map[Q_ORDER_TEXT] = list(Q_ORDER)
    llm.vector_map[Q_WEATHER_TEXT] = list(Q_WEATHER)
    return store, embedder, llm, content


def _fewshot_id(ds_id, question):
    """index_fewshot_example 的默认 example_id(md5,与源实现同规则)。"""
    return hashlib.md5(f"{ds_id}:{question}".encode("utf-8")).hexdigest()


# ── 索引构建与增量更新 ────────────────────────────────────────

class TestIndexing:
    def test_build_index_indexes_models_and_metrics(self, db):
        """表/指标分类建索引:3 条记录、chunk_id 分类编码、scope 回写数据源行。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        result = build_index(content, DS1, store, embedder, db=db)
        assert result.indexed_count == 3   # 2 model + 1 metric
        assert result.error is None

        scope = get_scope(db, DS1)
        assert scope is not None and is_scope_id_safe(scope)
        rows = store._sdk.rows[scope]
        assert set(rows) == {"model:orders", "model:users", "metric:gmv"}
        assert rows["model:orders"]["doc_id"] == DOC_SCHEMA
        # 表文本/指标文本分类正确(维度/表名/列名/指标的文本化处理)
        assert "表名orders" in rows["model:orders"]["text"]
        assert "SUM(total_amount)" in rows["metric:gmv"]["text"]
        # 建库维度探测与 embedder 维度缓存一致
        assert store._sdk.collections[scope] == 8
        assert embedder.dim == 8

    def test_build_index_scope_explicit_without_db(self, db):
        """显式 scope 直连(不需要 db):绕过数据源行登记。"""
        scope = "11111111-1111-1111-1111-111111111111"
        store, embedder, llm, content = _setup()
        result = build_index(content, DS1, store, embedder, scope=scope)
        assert result.indexed_count == 3
        assert store._sdk.rows[scope]

    def test_build_index_empty_content(self, db):
        store, embedder, llm, content = _setup()
        empty = SemanticModelContent(models=[])
        assert build_index(empty, DS1, store, embedder, scope="x").indexed_count == 0

    def test_build_index_scope_missing_fails(self, db):
        """既无 db 又无显式 scope → 降级返回 error(不抛)。"""
        store, embedder, llm, content = _setup()
        result = build_index(content, DS1, store, embedder)
        assert result.indexed_count == 0
        assert "scope" in result.error

    def test_build_index_unknown_data_source_degrades(self, db):
        """数据源行不存在 → scope 签发 fail-fast,build_index 降级不抛。"""
        store, embedder, llm, content = _setup()
        result = build_index(content, "ds-ghost", store, embedder, db=db)
        assert result.indexed_count == 0
        assert "数据源不存在" in result.error

    def test_build_index_embed_failure_degrades(self, db):
        """批量 embed 失败 → indexed_count=0 + error(不阻塞调用方)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        llm.embed_error = RuntimeError("embedding backend down")
        result = build_index(content, DS1, store, embedder, db=db)
        assert result.indexed_count == 0
        assert "embedding backend down" in result.error

    def test_build_index_upsert_failure_degrades(self, db):
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        store._sdk.fail_upsert = "milvus insert boom"
        result = build_index(content, DS1, store, embedder, db=db)
        assert result.indexed_count == 0
        assert "milvus insert boom" in result.error

    def test_build_index_batches_embed_in_one_call(self, db):
        """批量 embed:一次调用处理全部文本(源 build_index 效率口径)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, db=db)
        assert len(llm.embed_calls) == 1
        texts, stage = llm.embed_calls[0]
        assert len(texts) == 3
        assert stage == "chatbi.embed"

    def test_model_to_text_and_metric_to_text(self):
        """文本序列化规则:中文描述重复 2 遍 / 表名前缀 / 中文列名 / 指标公式。"""
        content = _make_content()
        orders = content.models[0]
        text = model_to_text(orders)
        assert text.count("订单主表") == 2          # 核心语义重复 2 遍
        assert "订单表" in text                      # display_name
        assert "表名orders" in text                  # 英文表名放最后
        assert "字段:主键,用户,订单金额" in text      # 只取中文 display_name
        mtext = metric_to_text(orders.metrics[0])
        assert "成交总额" in mtext
        assert "指标gmv" in mtext
        assert "SUM(total_amount)" in mtext
        assert "status IN ('paid','shipped')" in mtext  # condition 进文本

    def test_rebuild_index_deletes_old_and_rebuilds(self, db):
        """增量更新(删旧建新):旧索引清空、few-shot 分区不受波及。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        assert build_index(content, DS1, store, embedder, db=db).indexed_count == 3
        # few-shot 先回流(同 scope 的另一 doc 分区)
        index_fewshot_example(Q_FEWSHOT_TEXT, "SELECT COUNT(*) FROM orders",
                              embedder, DS1, store, db)

        content2 = SemanticModelContent(models=[content.models[1]])  # 只剩 users
        rr = rebuild_index(content2, DS1, store, embedder, db=db)
        assert rr.deleted_count == 3
        assert rr.indexed_count == 1
        assert rr.error is None

        scope = get_scope(db, DS1)
        rows = store._sdk.rows[scope]
        schema_rows = {k for k, v in rows.items() if v["doc_id"] == DOC_SCHEMA}
        fewshot_rows = [v for v in rows.values() if v["doc_id"] == DOC_FEWSHOT]
        assert schema_rows == {"model:users"}        # 删旧建新后只剩新内容
        assert len(fewshot_rows) == 1                # few-shot 分区幸存

    def test_rebuild_index_delete_failure_returns_error(self, db):
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, db=db)
        store._sdk.fail_delete = "milvus delete boom"
        rr = rebuild_index(content, DS1, store, embedder, db=db)
        assert rr.deleted_count == 0
        assert rr.indexed_count == 0
        assert "milvus delete boom" in rr.error


# ── 两阶段召回 ───────────────────────────────────────────────

class TestRetrieve:
    def _indexed(self, db):
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, db=db)
        return store, embedder, llm, content

    def test_two_stage_refine_selects_matching_tables(self, db):
        """召回 → 精筛:LLM 选中的表带 llm_selected,prompt 含候选清单。"""
        store, embedder, llm, content = self._indexed(db)
        llm.refine_result = json.dumps({"models": ["orders"], "reason": "订单表命中"},
                                       ensure_ascii=False)
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db, conv_id="conv-1")
        assert result.degraded is False
        assert result.no_match_reason is None
        assert [m["name"] for m in result.models] == ["orders"]
        assert result.models[0]["type"] == "model"
        assert result.models[0]["llm_selected"] is True
        assert result.models[0]["id"] == "model:orders"

        # 精筛调用参数:temperature=0、stage、conv_id 透传;prompt 含候选与规则
        call = llm.chat_calls[0]
        assert call["temperature"] == 0.0
        assert call["stage"] == "chatbi.retrieve.refine"
        assert call["conv_id"] == "conv-1"
        assert "name=orders" in call["prompt"] and "type=model" in call["prompt"]
        assert "指标" in call["prompt"] or "gmv" in call["prompt"]
        assert "宁缺毋滥" in call["prompt"]

    def test_refine_multi_table_selection(self, db):
        """问题涉及多对象时 LLM 全选(订单表 + 指标)。"""
        store, embedder, llm, content = self._indexed(db)
        llm.refine_result = json.dumps({"models": ["orders", "gmv"], "reason": "r"})
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        # 候选按分数降序(gmv 0.74 > orders 0.67),精筛保留召回顺序
        assert sorted(m["name"] for m in result.models) == ["gmv", "orders"]

    def test_refine_degrades_on_llm_failure(self, db):
        """LLM 失败 → 降级返回原始召回(向量已过滤, degraded 标记)。"""
        store, embedder, llm, content = self._indexed(db)
        llm.chat_error = RuntimeError("rate limited")
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.degraded is True
        # 召回阶段已按阈值过滤:orders + gmv 命中,users 正交被滤掉
        assert sorted(m["name"] for m in result.models) == ["gmv", "orders"]
        assert all("llm_selected" not in m for m in result.models)

    def test_refine_degrades_on_invalid_json(self, db):
        store, embedder, llm, content = self._indexed(db)
        llm.refine_result = "抱歉,我无法判断。"
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.degraded is True
        assert len(result.models) == 2

    def test_refine_accepts_list_payload(self, db):
        """LLM 返回数组而非对象 → 统一规整(防御性,源实现同款)。"""
        store, embedder, llm, content = self._indexed(db)
        llm.refine_result = '["orders"]'
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.degraded is False
        assert [m["name"] for m in result.models] == ["orders"]

    def test_refine_empty_selection_is_no_match(self, db):
        """宁缺毋滥:LLM 判断无真匹配 → 空结果 + 理由。"""
        store, embedder, llm, content = self._indexed(db)
        llm.refine_result = json.dumps({"models": [], "reason": "候选均不相关"},
                                       ensure_ascii=False)
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.models == []
        assert result.no_match_reason == "候选均不相关"

    def test_refine_without_llm_degrades(self, db):
        """未注入 llm → 与 LLM 失败同款降级(不抛)。"""
        store, embedder, llm, content = self._indexed(db)
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          db=db)
        assert result.degraded is True
        assert len(result.models) == 2

    def test_no_recall_returns_friendly_reason(self, db):
        """无召回不 fallback 不随机选表 → 友好提示(v1 教训)。"""
        store, embedder, llm, content = self._indexed(db)
        llm.vector_map[Q_WEATHER_TEXT] = Q_NONE
        result = retrieve(Q_WEATHER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.models == []
        assert result.no_match_reason == "无法匹配到相关表，请换一种问法或检查数据源"
        assert llm.chat_calls == []   # 无召回不烧精筛

    def test_embed_failure_reason(self, db):
        store, embedder, llm, content = self._indexed(db)
        llm.embed_error = RuntimeError("embed down")
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.models == []
        assert result.no_match_reason == "问题向量化失败, 无法检索"

    def test_skip_llm_refine_returns_candidates(self, db):
        """skip_llm_refine:阶段1 即终态,不调用 LLM。"""
        store, embedder, llm, content = self._indexed(db)
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          skip_llm_refine=True, llm=llm, db=db)
        assert llm.chat_calls == []
        assert result.degraded is False
        assert sorted(m["name"] for m in result.models) == ["gmv", "orders"]
        # 召回默认参数(源 config 原值):top_k=20 / 阈值 0.35
        assert DEFAULT_TOP_K == 20 and DEFAULT_SCORE_THRESHOLD == 0.35

    def test_retrieve_with_explicit_scope_no_db(self, db):
        """显式 scope 直连(测试/无关系库场景)。"""
        scope = "22222222-2222-2222-2222-222222222222"
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, scope=scope)
        llm.vector_map["用户有哪些"] = list(V_USERS)
        llm.refine_result = json.dumps({"models": ["users"], "reason": "r"})
        result = retrieve("用户有哪些", store, embedder, scope=scope, llm=llm)
        assert [m["name"] for m in result.models] == ["users"]

    def test_retrieve_without_scope_resolution_raises(self, db):
        """既无 db 又无 scope 又无 ds → 接线缺陷,fail-fast。"""
        store, embedder, llm, content = _setup()
        with pytest.raises(ValueError):
            retrieve(Q_ORDER_TEXT, store, embedder, llm=llm)

    def test_retrieve_unindexed_data_source_is_empty(self, db):
        """数据源存在但未建索引(未登记 scope)→ 召回为空(同源语义)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        result = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                          llm=llm, db=db)
        assert result.no_match_reason is not None
        assert result.models == []

    def test_cross_source_isolation_and_merge(self, db):
        """多源隔离:限定 ds 只召回该源;不限 ds 时跨源合并按分数排序。"""
        _make_ds(db, DS1, "订单库")
        _make_ds(db, DS2, "天气库")
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, db=db)
        weather_content = _make_weather_content()
        llm.vector_map[model_to_text(weather_content.models[0])] = list(V_WEATHER)
        build_index(weather_content, DS2, store, embedder, db=db)
        assert len(list_scopes(db)) == 2

        # 限定 DS1:天气表(在 DS2)不可见
        llm.refine_result = json.dumps({"models": ["orders"], "reason": "r"})
        r1 = retrieve(Q_ORDER_TEXT, store, embedder, data_source_id=DS1,
                      llm=llm, db=db)
        assert [m["name"] for m in r1.models] == ["orders"]

        # 不限数据源:跨源合并召回,天气命中
        llm.refine_result = json.dumps({"models": ["weather"], "reason": "r"})
        r2 = retrieve(Q_WEATHER_TEXT, store, embedder, llm=llm, db=db)
        assert [m["name"] for m in r2.models] == ["weather"]
        assert r2.models[0]["id"] == "model:weather"


# ── schema_context 构建 ──────────────────────────────────────

class TestSchemaContext:
    def test_build_schema_context_with_names(self):
        content = _make_content()
        ctx = build_schema_context(content, ["orders"])
        # 列带中文名与 data_type;指标行含公式与 condition
        assert "orders(订单表):" in ctx
        assert "user_id(中文: 用户)[BIGINT]" in ctx
        assert "total_amount(中文: 订单金额)[DECIMAL]" in ctx
        assert ("指标: gmv(成交总额) = SUM(total_amount) "
                "WHERE status IN ('paid','shipped')") in ctx
        assert "users" not in ctx                  # 未选中表被过滤

    def test_build_schema_context_relationships_and_composite(self):
        content = _make_content()
        content.models[1].relationships.append(Relationship(
            name="orders_user", target_model="orders", join_type="LEFT",
            on="users.id = orders.user_id", type="N:1"))
        content.models[0].metrics.append(Metric(
            name="aov", display_name="客单价", formula="gmv / order_count",
            type="composite", factor_metric_names=["gmv"]))
        ctx = build_schema_context(content, ["users", "orders"])
        assert "→orders(users.id = orders.user_id)" in ctx   # JOIN 依据
        assert "[子指标: gmv]" in ctx                        # composite 标注

    def test_build_metrics_hint_and_allowed_columns(self):
        content = _make_content()
        hint = build_metrics_hint(content, ["orders"])
        assert hint == ("orders: gmv(成交总额) = SUM(total_amount) "
                        "WHERE status IN ('paid','shipped')")
        assert extract_allowed_columns(content, ["orders"]) == {
            "id", "user_id", "total_amount"}
        assert extract_allowed_columns(None) == set()
        assert build_schema_context(None) == ""

    def test_retrieve_context_one_stop(self, db):
        """一站式:召回精筛 → schema_context / 白名单列 / 指标提示。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, content = _setup()
        build_index(content, DS1, store, embedder, db=db)
        llm.refine_result = json.dumps({"models": ["orders"], "reason": "r"})
        out = retrieve_context(Q_ORDER_TEXT, content, store, embedder,
                               data_source_id=DS1, llm=llm, db=db)
        assert out["model_names"] == ["orders"]
        assert "orders(订单表):" in out["schema_context"]
        assert out["allowed_columns"] == {"id", "user_id", "total_amount"}
        assert "gmv(成交总额)" in out["metrics_hint"]
        assert out["retrieval"].degraded is False


# ── few-shot 命中 / 格式化 / 回流 ────────────────────────────

class TestFewshot:
    def test_index_and_find_fewshot(self, db):
        """回流 → 命中:同问题召回示例,格式化输出与 PG 权威行齐备。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, _ = _setup()
        llm.vector_map[Q_FEWSHOT_TEXT] = list(V_ORDERS)
        index_fewshot_example(Q_FEWSHOT_TEXT, "SELECT COUNT(*) FROM orders",
                              embedder, DS1, store, db)

        examples = find_fewshot_examples(Q_FEWSHOT_TEXT, store, embedder,
                                         data_source_id=DS1, db=db)
        assert len(examples) == 1
        assert examples[0].question == Q_FEWSHOT_TEXT
        assert examples[0].sql == "SELECT COUNT(*) FROM orders"
        assert examples[0].score == pytest.approx(1.0)

        assert format_fewshot_prompt(examples) == (
            "以下是相似问题的参考 SQL (已审核, 可借鉴写法):\n"
            "1. 问题: 上个月的订单总数\n"
            "   SQL: SELECT COUNT(*) FROM orders")
        assert format_fewshot_prompt([]) == ""

        rows = list_fewshot_examples(db, DS1)
        assert len(rows) == 1
        assert rows[0]["id"] == _fewshot_id(DS1, Q_FEWSHOT_TEXT)
        assert rows[0]["sql"] == "SELECT COUNT(*) FROM orders"

    def test_fewshot_threshold_filters_weak_match(self, db):
        """score 阈值 0.5:正交问题不召回(宁缺毋滥)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, _ = _setup()
        llm.vector_map[Q_FEWSHOT_TEXT] = list(V_ORDERS)
        index_fewshot_example(Q_FEWSHOT_TEXT, "SELECT COUNT(*) FROM orders",
                              embedder, DS1, store, db)
        llm.vector_map[Q_WEATHER_TEXT] = list(Q_NONE)
        assert find_fewshot_examples(Q_WEATHER_TEXT, store, embedder,
                                     data_source_id=DS1, db=db) == []

    def test_fewshot_cross_source_isolation(self, db):
        """防跨数据源召回:DS2 的分区查不到 DS1 的示例。"""
        _make_ds(db, DS1, "订单库")
        _make_ds(db, DS2, "天气库")
        store, embedder, llm, _ = _setup()
        llm.vector_map[Q_FEWSHOT_TEXT] = list(V_ORDERS)
        index_fewshot_example(Q_FEWSHOT_TEXT, "SELECT COUNT(*) FROM orders",
                              embedder, DS1, store, db)
        assert find_fewshot_examples(Q_FEWSHOT_TEXT, store, embedder,
                                     scope=get_or_create_scope(db, DS2),
                                     db=db) == []

    def test_fewshot_missing_sql_skipped(self, db):
        """text 无有效 sql(非 JSON/空)→ 跳过该候选(源实现同款防御)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, _ = _setup()
        llm.vector_map[Q_FEWSHOT_TEXT] = list(V_ORDERS)
        scope = get_or_create_scope(db, DS1)
        # 直接塞一条"无 sql"的 few-shot 记录
        from domains.chatbi.stores import VectorRecord
        store.upsert_records(scope, [VectorRecord(
            id="manual-no-sql", vector=list(V_ORDERS), metadata={},
            text="纯文本,不是 JSON")], doc_id=DOC_FEWSHOT)
        assert find_fewshot_examples(Q_FEWSHOT_TEXT, store, embedder,
                                     data_source_id=DS1, db=db) == []

    def test_fewshot_embed_failure_degrades(self, db):
        """embed 失败 → 不写向量不写 PG(不阻塞审核流程,源实现同款)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, _ = _setup()
        llm.embed_error = RuntimeError("embed down")
        index_fewshot_example(Q_FEWSHOT_TEXT, "SELECT 1", embedder, DS1,
                              store, db)
        assert list_fewshot_examples(db, DS1) == []
        assert store._sdk.rows == {}

    def test_fewshot_tenant_delete(self, db):
        """多租户删除:按数据源删示例,不影响其他数据源。"""
        _make_ds(db, DS1, "订单库")
        _make_ds(db, DS2, "天气库")
        store, embedder, llm, _ = _setup()
        index_fewshot_example("q1", "SELECT 1", embedder, DS1, store, db)
        index_fewshot_example("q2", "SELECT 2", embedder, DS2, store, db)
        assert delete_fewshot_examples(db, DS1) == 1
        assert list_fewshot_examples(db, DS1) == []
        assert len(list_fewshot_examples(db, DS2)) == 1

    def test_find_fewshot_search_failure_returns_empty(self, db):
        """检索异常 → 返回空列表不抛(源实现降级语义)。"""
        _make_ds(db, DS1, "订单库")
        store, embedder, llm, _ = _setup()

        class BoomStore(ChatBIVectorStore):
            def search_records(self, *a, **kw):
                raise RuntimeError("milvus down")

        boom = BoomStore(FakeSdkVector())
        assert find_fewshot_examples(Q_FEWSHOT_TEXT, boom, embedder,
                                     scope="33333333-3333-3333-3333-333333333333",
                                     db=db) == []


# ── 存储设施(scope 登记 / 缓存 / 前缀)──────────────────────────

class TestStores:
    def test_get_or_create_scope_idempotent(self, db):
        _make_ds(db, DS1, "订单库")
        scope1 = get_or_create_scope(db, DS1)
        scope2 = get_or_create_scope(db, DS1)
        assert scope1 == scope2
        assert is_scope_id_safe(scope1)          # 服务端签发的 UUID

    def test_get_scope_unregistered(self, db):
        _make_ds(db, DS1, "订单库")
        assert get_scope(db, DS1) is None
        assert get_scope(db, "ds-ghost") is None

    def test_resolve_scopes_semantics(self, db):
        """解析规则:显式 scope > 数据源登记 > 全部活跃源;无 db 时 fail-fast。"""
        _make_ds(db, DS1, "订单库")
        with pytest.raises(ValueError):
            resolve_scopes(None)
        assert resolve_scopes(None, scope="s") == ["s"]
        assert resolve_scopes(db, DS1) == []            # 未登记 → 空(召回为空)
        scope = get_or_create_scope(db, DS1)
        assert resolve_scopes(db, DS1) == [scope]
        assert scope in resolve_scopes(db)              # 全量召回
        assert resolve_scopes(db, "ds-ghost") == []

    def test_chatbi_prefix_registered(self, db):
        from domains.chatbi import stores
        stores._ensure_prefix_registered()
        assert registered_prefixes().get("chatbi") == "chatbi"

    def test_embedder_cache_and_dim(self):
        """维度缓存(首次 embed 探测)+ 文本级缓存(相同文本不出站)。"""
        llm = FakeLLM()
        emb = ChatBIEmbedder(llm)
        assert emb.dim is None
        vecs = emb.embed(["a", "a", "b"])
        assert len(vecs) == 3 and len(vecs[0]) == 8
        assert emb.dim == 8
        assert len(llm.embed_calls) == 1
        # 批内不去重(与源 LocalEmbedder 同语义:缓存只跨调用生效),
        # 未缓存的 3 条一次性出站
        assert llm.embed_calls[0][0] == ["a", "a", "b"]
        assert emb.embed(["a", "b"]) == [vecs[0], vecs[2]]   # 跨调用缓存命中
        assert len(llm.embed_calls) == 1                # 全缓存命中,零出站
        assert emb.embed([]) == []

    def test_parse_json_response_tolerant(self):
        """剥洋葱式解析:纯 JSON / markdown 包裹 / 前后文字 / 数组 / 失败。"""
        assert parse_json_response('{"a": 1}') == {"a": 1}
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}
        assert parse_json_response('结果是 {"a": 1} 请查收') == {"a": 1}
        assert parse_json_response('["orders"]') == ["orders"]
        assert parse_json_response("无法判断") is None
        assert parse_json_response("") is None
        assert parse_json_response(None) is None
