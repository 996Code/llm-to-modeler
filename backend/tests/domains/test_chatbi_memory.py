"""chatbi 记忆/指标反哺栈单测 —— CRUD/召回/提炼/整理/链路沉淀/指标反哺闭环。

覆盖(移植自 chat-bi test_recall.py / test_metric_inference.py 的核心行为,
存储层由文件换 PG 后的等价断言):
  - 记忆 CRUD: 写入/更新(created_at 保留 + consolidated 重置)/删除/
    linkage 同名 upsert/extra_metadata 防御与合入;
  - 管理端列表: user 过滤 / limit / newest-first / 来源对话 conversation_id;
  - 召回: 关键词相关性排序 / max_count 上限 / 宁缺毋滥 / consolidated 与
    linkage 排除 / recall_text 格式化与 fail-open 降级(存储挂→"");
  - 提炼: should_save 判定 / name 白名单 / LLM 挂降级 None / 落库含
    conversation_id / prompt 含已有记忆摘要(第一道去重防线);
  - 整理: 合并写入 + 原记忆标记隐藏 / 进度回调 / dict 包裹兼容 / 失败降级;
  - 查询流水与链路沉淀: recent_queries 追加+容量控制 / linkage 共现递增、
    场景去重、JOIN 路径提取(单跳/多跳/间接标注);
  - 指标反哺: 命中 → co_occurrence 原子递增(语义层 content JSON, pydantic
    对象不变) / 新聚合单表 → metric_suggestion(按名去重) / 多表 JOIN 拒绝 /
    无聚合不反哺 / metric_hits 组装 / 语义层行缺失回退 / suggestion 失败
    不阻断命中 / 存储异常上抛(调用方 ask_data 兜底, fail-open 按源码)。

测试库: TEST_DATABASE_URL 指定的专属 PG(conftest 每测试清表);
LLM 用可编程替身, 不连真实模型。
"""
import json
import re
import uuid
from datetime import datetime, timezone

import pytest

from domains.chatbi.feedback import (
    _extract_sql_tables,
    persist_metric_feedback,
)
from domains.chatbi.memory import (
    CHATBI_MEMORY_DDL,
    ChatBIMemoryStore,
    consolidate_memories,
    delete_memory,
    extract_and_save_memory,
    extract_memory_from_turn,
    format_memories_for_prompt,
    get_memory_store,
    list_memories,
    persist_linkage_memory,
    recall_memories,
    recall_text,
    save_query_memory,
    _extract_join_on_conditions,
    _extract_join_pairs,
)
from domains.chatbi.models import (
    CHATBI_DDL,
    Column,
    Metric,
    Model,
    SemanticModelContent,
)
from sdk.relational_store import PackRelationalDB

DS1 = "ds-mem-0001"


# ── 测试替身 ──────────────────────────────────────────────────

class FakeLLM:
    """chat/chat_json 可编程替身(与 test_chatbi_retrieval.FakeLLM 同模式)。"""

    def __init__(self, chat_text: str = "", chat_json_obj: dict | None = None,
                 chat_error: Exception | None = None,
                 json_error: Exception | None = None):
        self.chat_text = chat_text
        self.chat_json_obj = chat_json_obj
        self.chat_error = chat_error
        self.json_error = json_error
        self.chat_calls: list[dict] = []
        self.json_calls: list[dict] = []

    def chat(self, messages, temperature=None, stage=None, conv_id=None, **kw):
        self.chat_calls.append({"messages": messages, "temperature": temperature,
                                "stage": stage, "conv_id": conv_id})
        if self.chat_error is not None:
            raise self.chat_error
        return (self.chat_text, {"stage": stage})

    def chat_json(self, messages, temperature=None, stage=None, conv_id=None, **kw):
        self.json_calls.append({"messages": messages, "temperature": temperature,
                                "stage": stage, "conv_id": conv_id})
        if self.json_error is not None:
            raise self.json_error
        return dict(self.chat_json_obj or {})


class BrokenDB:
    """存储故障替身: 一切操作即抛(降级语义验证用)。"""

    def connect(self):
        raise RuntimeError("db down")

    def init_schema(self, ddl):
        raise RuntimeError("db down")


# ── fixtures 与构造辅助 ──────────────────────────────────────

@pytest.fixture()
def db():
    """pack 关系库(幂等建表;数据由 conftest 每测试清表)。"""
    _db = PackRelationalDB("chatbi")
    _db.init_schema(list(CHATBI_DDL) + CHATBI_MEMORY_DDL)
    yield _db


def _make_content(co_occurrence: int = 0) -> SemanticModelContent:
    """语义层样例: orders(gmv=SUM(total_amount)) + users。"""
    orders = Model(
        name="orders", display_name="订单表", description="订单主表",
        columns=[
            Column(name="id", display_name="主键", data_type="BIGINT"),
            Column(name="total_amount", display_name="订单金额",
                   data_type="DECIMAL"),
            Column(name="user_id", display_name="用户", data_type="BIGINT"),
        ],
        metrics=[Metric(name="gmv", display_name="成交总额",
                        formula="SUM(total_amount)",
                        co_occurrence=co_occurrence)],
    )
    users = Model(
        name="users", display_name="用户表", description="用户主表",
        columns=[Column(name="id", display_name="主键", data_type="BIGINT")],
    )
    return SemanticModelContent(models=[orders, users])


def _insert_semantic_row(db, content: SemanticModelContent, ds_id: str = DS1):
    """写一行 is_current 语义层(指标反哺的持久化目标)。"""
    now = datetime.now(timezone.utc).isoformat()
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO chatbi_semantic_models "
            "(id, data_source_id, version, content, is_current, created_at) "
            "VALUES (?, ?, 1, ?, 1, ?)",
            (str(uuid.uuid4()), ds_id, content.model_dump_json(), now),
        )


def _semantic_json_in_db(db, ds_id: str = DS1) -> dict:
    """读回语义层 is_current 行的 content JSON。"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT content FROM chatbi_semantic_models "
            "WHERE data_source_id = ? AND is_current = 1", (ds_id,)).fetchone()
    assert row is not None
    return json.loads(row["content"])


def _entry_by_name(entries: list[dict], name: str) -> dict:
    matched = [e for e in entries if e["name"] == name]
    assert matched, f"记忆 {name!r} 不存在: {[e['name'] for e in entries]}"
    return matched[0]


def _linkage_state(tables, join_section="", question="本月各品类销售额",
                   aggregation=None) -> dict:
    """管线 dict 形态的查询状态(ask_data state 同构)。"""
    state = {"current_tables": list(tables),
             "join_path_section": join_section,
             "question": question}
    if aggregation:
        state["thinking"] = {"aggregation": aggregation}
    return state


# ── 记忆 CRUD ────────────────────────────────────────────────

class TestMemoryCrud:
    """ChatBIMemoryStore 写读删与源 AgentMemoryStore 行为对齐。"""

    def test_save_list_read_roundtrip(self, db):
        store = get_memory_store(db)
        mem_id = store.save_memory(
            name="gmv 口径", description="GMV 计算口径",
            content="GMV = SUM(total_amount)", memory_type="project")
        assert mem_id
        entries = store.list_memories()
        entry = _entry_by_name(entries, "gmv 口径")
        assert entry["id"] == mem_id
        assert entry["description"] == "GMV 计算口径"
        assert entry["type"] == "project"
        assert entry["consolidated"] is False
        assert entry["created_at"]
        assert store.read_memory(mem_id) == "GMV = SUM(total_amount)"

    def test_update_preserves_created_at(self, db):
        store = get_memory_store(db)
        mem_id = store.save_memory(name="m", description="d1", content="c1")
        before = _entry_by_name(store.list_memories(), "m")
        store.save_memory(name="m", description="d2", content="c2", mem_id=mem_id)
        after = _entry_by_name(store.list_memories(), "m")
        assert after["created_at"] == before["created_at"]  # 更新保留原值
        assert after["description"] == "d2"
        assert store.read_memory(mem_id) == "c2"

    def test_update_resets_consolidated(self, db):
        # 源: save_memory 重写 frontmatter → consolidated 复位 false
        store = get_memory_store(db)
        mem_id = store.save_memory(name="m", description="d", content="c")
        assert store.mark_consolidated(mem_id) is True
        assert _entry_by_name(store.list_memories(), "m")["consolidated"] is True
        store.save_memory(name="m", description="d", content="c", mem_id=mem_id)
        assert _entry_by_name(store.list_memories(), "m")["consolidated"] is False

    def test_mark_consolidated_missing(self, db):
        store = get_memory_store(db)
        assert store.mark_consolidated("no-such-id") is False

    def test_delete_memory(self, db):
        store = get_memory_store(db)
        mem_id = store.save_memory(name="m", description="d", content="c")
        assert store.delete_memory(mem_id) is True
        assert store.read_memory(mem_id) is None
        assert store.delete_memory(mem_id) is False  # 幂等返回 False
        # 管理端模块级入口
        other = store.save_memory(name="m2", description="d", content="c")
        assert delete_memory(db, other) is True
        assert delete_memory(db, other) is False

    def test_linkage_name_upsert_dedup(self, db):
        # 源索引去重语义: 同名 linkage(同表对)只保留最新一条
        store = get_memory_store(db)
        store.save_memory(name="linkage-a-b", description="表 a 和 b 的共现经验",
                          content="x", memory_type="linkage",
                          extra_metadata={"co_occurrence": 1, "tables": ["a", "b"]})
        store.save_memory(name="linkage-a-b", description="表 a 和 b 的共现经验",
                          content="x2", memory_type="linkage",
                          extra_metadata={"co_occurrence": 2, "tables": ["a", "b"]})
        linkage = [e for e in store.list_memories() if e["type"] == "linkage"]
        assert len(linkage) == 1
        assert linkage[0]["co_occurrence"] == 2
        # 乱序表对查询 (源 get_linkage_memory 契约)
        found = store.get_linkage_memory("b", "a")
        assert found is not None and found["co_occurrence"] == 2
        assert store.get_linkage_memory("a", "c") is None

    def test_extra_metadata_known_and_extra_keys(self, db):
        store = get_memory_store(db)
        mem_id = store.save_memory(
            name="metric-suggestion-orders-count", description="d", content="c",
            memory_type="metric_suggestion",
            extra_metadata={"table": "orders", "formula": "COUNT(*)",
                            "conversation_id": "conv-1"})
        entry = next(e for e in store.list_memories() if e["id"] == mem_id)
        assert entry["table"] == "orders"        # 非枚举键 → extra_json 合入
        assert entry["formula"] == "COUNT(*)"
        assert entry["conversation_id"] == "conv-1"
        assert entry["conv_id"] == "conv-1"      # 管理端别名

    def test_extra_metadata_rejects_bad_key(self, db):
        store = get_memory_store(db)
        with pytest.raises(ValueError):
            store.save_memory(name="x", description="d", content="c",
                              extra_metadata={"bad-key": 1})

    def test_read_index_and_reconcile(self, db):
        store = get_memory_store(db)
        mem_id = store.save_memory(name="m", description="一句话", content="c")
        idx = store.read_index()
        assert f"- [{mem_id}]({mem_id}) — 一句话" in idx
        # 构造同名 linkage 重复行 → reconcile 去重保留最新
        store.save_memory(name="linkage-a-b", description="d", content="c",
                          memory_type="linkage", mem_id="dup-1",
                          extra_metadata={"tables": ["a", "b"]})
        store.save_memory(name="linkage-a-b", description="d", content="c",
                          memory_type="linkage", mem_id="dup-2",
                          extra_metadata={"tables": ["a", "b"]})
        removed = store.reconcile_index()
        assert removed == 1
        linkage = [e for e in store.list_memories() if e["type"] == "linkage"]
        assert len(linkage) == 1 and linkage[0]["id"] == "dup-2"


# ── 管理端列表 ───────────────────────────────────────────────

class TestAdminList:
    """list_memories 模块级入口 (api.py /memories 契约)。"""

    def test_order_limit_and_fields(self, db):
        store = get_memory_store(db)
        store.save_memory(name="m1", description="d", content="c1")
        store.save_memory(name="m2", description="d", content="c2")
        store.save_memory(name="m3", description="d", content="c3")
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:  # 人为错开 created_at 验证 newest-first
            conn.execute("UPDATE chatbi_agent_memories SET created_at = ? WHERE name = 'm1'",
                         ("2024-01-01T00:00:00+00:00",))
            conn.execute("UPDATE chatbi_agent_memories SET created_at = ? WHERE name = 'm2'",
                         ("2024-01-02T00:00:00+00:00",))
            conn.execute("UPDATE chatbi_agent_memories SET created_at = ? WHERE name = 'm3'",
                         ("2024-01-03T00:00:00+00:00",))
        items = list_memories(db)
        assert [i["name"] for i in items] == ["m3", "m2", "m1"]
        assert items[0]["content"] == "c3"       # MemoryOut 契约含正文
        assert items[0]["created_at"] == "2024-01-03T00:00:00+00:00"
        assert len(list_memories(db, limit=2)) == 2
        assert [i["name"] for i in list_memories(db, limit=2)] == ["m3", "m2"]

    def test_user_filter(self, db):
        store = get_memory_store(db)
        store.save_memory(name="u1-mem", description="d", content="c", user_id="u1")
        store.save_memory(name="u2-mem", description="d", content="c", user_id="u2")
        names = [e["name"] for e in list_memories(db, user_id="u1")]
        assert names == ["u1-mem"]
        assert len(list_memories(db)) == 2       # 不过滤 = 全量

    def test_conversation_and_include_flag(self, db):
        store = get_memory_store(db)
        store.save_memory(name="conv-mem", description="d", content="c",
                          extra_metadata={"conversation_id": "conv-9"})
        store.save_memory(name="old-mem", description="d", content="c")
        store.mark_consolidated(
            _entry_by_name(store.list_memories(), "old-mem")["id"])
        items = list_memories(db, include_consolidated=False)
        assert [i["name"] for i in items] == ["conv-mem"]  # 源 API 默认隐藏语义
        entry = items[0]
        assert entry["conversation_id"] == "conv-9"
        assert entry["conv_id"] == "conv-9"
        assert len(list_memories(db)) == 2


# ── 召回 ─────────────────────────────────────────────────────

class TestRecall:
    """recall_memories / recall_text (源 test_recall.py 行为对齐)。"""

    def _seed(self, db):
        store = get_memory_store(db)
        store.save_memory(name="gmv", description="GMV 计算口径",
                          content="GMV content")
        store.save_memory(name="naming", description="命名规范",
                          content="naming content")

    def test_relevance_ordering(self, db):
        self._seed(db)
        results = recall_memories("GMV", db)
        assert results and results[0]["name"] == "gmv"   # 相关记忆排第一

    def test_no_match_returns_empty(self, db):
        self._seed(db)
        # 宁缺毋滥: 问题与所有记忆不相关 → 空
        assert recall_memories("完全无关的天气问题xyz", db) == []

    def test_max_count(self, db):
        store = get_memory_store(db)
        for i in range(10):
            store.save_memory(name=f"m{i}", description=f"记忆{i} 相关",
                              content=f"content {i}")
        assert len(recall_memories("相关", db, max_count=3)) == 3
        # 默认上限 = memory_max_recall_count = 5
        assert len(recall_memories("相关", db)) == 5

    def test_excludes_consolidated_and_linkage(self, db):
        store = get_memory_store(db)
        store.save_memory(name="gmv", description="GMV 计算口径", content="c")
        mem_id = store.save_memory(name="old", description="GMV 旧知识", content="c")
        store.mark_consolidated(mem_id)
        store.save_memory(name="linkage-orders-users",
                          description="表 orders 和 users 的共现经验",
                          content="c", memory_type="linkage",
                          extra_metadata={"co_occurrence": 3,
                                          "tables": ["orders", "users"]})
        names = [m["name"] for m in recall_memories("GMV orders users", db)]
        assert names == ["gmv"]  # consolidated 与 linkage 均不进 prompt

    def test_user_filter(self, db):
        store = get_memory_store(db)
        store.save_memory(name="gmv", description="GMV 计算口径", content="c",
                          user_id="u1")
        assert len(recall_memories("GMV", db, user_id="u1")) == 1
        assert recall_memories("GMV", db, user_id="u2") == []
        assert len(recall_memories("GMV", db)) == 1  # 不过滤 = 全量

    def test_format_for_prompt(self, db):
        assert format_memories_for_prompt([]) == ""
        text = format_memories_for_prompt([
            {"name": "gmv", "description": "GMV 口径",
             "content": "GMV = SUM(total_amount)"}])
        assert text.startswith("【Agent 记忆 (相关业务知识)】")
        assert "[gmv] GMV 口径" in text
        assert "GMV = SUM(total_amount)" in text

    def test_format_truncates_body(self, db):
        text = format_memories_for_prompt([
            {"name": "m", "description": "d", "content": "x" * 500}])
        assert "x" * 201 not in text
        assert ("x" * 200) in text

    def test_recall_text_formats(self, db):
        self._seed(db)
        text = recall_text(None, db, "GMV 是多少")
        assert "【Agent 记忆 (相关业务知识)】" in text
        assert "[gmv] GMV 计算口径" in text
        assert "GMV content" in text

    def test_recall_text_no_match_returns_empty(self, db):
        self._seed(db)
        assert recall_text(None, db, "量子物理") == ""
        # 问题与所有记忆都不重叠 → "" (宁缺毋滥, 不灌无关内容)
        assert recall_text(None, db, "任何问题") == ""
        # 空库 → ""
        assert recall_text(None, PackRelationalDB("chatbi"), "任何问题") == ""

    def test_recall_text_top_k(self, db):
        store = get_memory_store(db)
        for i in range(4):
            store.save_memory(name=f"m{i}", description=f"记忆{i} 相关",
                              content=f"c{i}")
        text = recall_text(None, db, "相关", top_k=1)
        assert text.count("[m") == 1

    def test_recall_text_fail_open_on_broken_db(self):
        # 存储挂 → "" (源调用侧 try/except 降级语义内化, 不阻断主流程)
        assert recall_text(None, BrokenDB(), "GMV") == ""

    def test_recall_text_needs_no_llm(self, db):
        # llm=None 也可用: 召回路径零 LLM 调用 (源设计"省调用")
        self._seed(db)
        assert "gmv" in recall_text(None, db, "GMV")


# ── LLM 自主提炼 ─────────────────────────────────────────────

class TestExtractMemory:
    """extract_memory_from_turn / extract_and_save_memory。"""

    _GOOD = {"should_save": True, "name": "status 约定",
             "description": "status=2 表示审核中",
             "type": "project", "content": "status=2 表示审核中"}

    def test_extract_returns_judgement(self, db):
        llm = FakeLLM(chat_json_obj=dict(self._GOOD))
        result = extract_memory_from_turn(
            llm, "有多少审核中订单", "SELECT COUNT(*) FROM orders",
            ["orders"], "共 10 单", db, conv_id="conv-1")
        assert result == {"name": "status 约定", "description": "status=2 表示审核中",
                          "type": "project", "content": "status=2 表示审核中"}
        call = llm.json_calls[0]
        assert call["stage"] == "chatbi.memory.extract"   # node= → stage 契约
        assert call["temperature"] == 0.0
        assert call["conv_id"] == "conv-1"

    def test_extract_prompt_includes_existing_summaries(self, db):
        # 第一道去重防线: 已有记忆摘要进 prompt (源 _get_existing_memory_summaries)
        store = get_memory_store(db)
        store.save_memory(name="gmv 口径", description="GMV 计算口径",
                          content="c")
        llm = FakeLLM(chat_json_obj={"should_save": False})
        extract_memory_from_turn(llm, "GMV 是多少", "SELECT 1", ["orders"],
                                 "回复", db)
        prompt = llm.json_calls[0]["messages"][0]["content"]
        assert "【已有记忆 (避免重复)】" in prompt
        assert "- gmv 口径: GMV 计算口径" in prompt

    def test_extract_not_worth_saving(self, db):
        llm = FakeLLM(chat_json_obj={"should_save": False})
        assert extract_memory_from_turn(llm, "q", "s", ["t"], "r", db) is None

    def test_extract_llm_down_returns_none(self, db):
        # LLM 挂 → None (fail-open, 不阻断主流程)
        llm = FakeLLM(json_error=RuntimeError("llm down"))
        assert extract_memory_from_turn(llm, "q", "s", ["t"], "r", db) is None

    def test_extract_invalid_payload(self, db):
        # 缺 name/content / name 不合法 → None
        base = dict(self._GOOD)
        base["name"] = ""
        assert extract_memory_from_turn(
            FakeLLM(chat_json_obj=base), "q", "s", ["t"], "r", db) is None
        base = dict(self._GOOD)
        base["content"] = ""
        assert extract_memory_from_turn(
            FakeLLM(chat_json_obj=base), "q", "s", ["t"], "r", db) is None
        base = dict(self._GOOD)
        base["name"] = "bad..name"  # 白名单外字符
        assert extract_memory_from_turn(
            FakeLLM(chat_json_obj=base), "q", "s", ["t"], "r", db) is None

    def test_extract_and_save_with_conversation(self, db):
        # 源 chat_stream.py 调用侧组合: 提炼 → 落库(来源对话关联)
        llm = FakeLLM(chat_json_obj=dict(self._GOOD))
        result = extract_and_save_memory(
            llm, db, "有多少审核中订单", "SELECT 1", ["orders"], "回复",
            conv_id="conv-7", user_id="u1")
        assert result is not None and result["id"]
        entry = _entry_by_name(get_memory_store(db).list_memories(), "status 约定")
        assert entry["conversation_id"] == "conv-7"
        assert entry["user_id"] == "u1"
        assert entry["type"] == "project"

    def test_extract_and_save_noop_when_nothing_worth(self, db):
        llm = FakeLLM(chat_json_obj={"should_save": False})
        assert extract_and_save_memory(llm, db, "q", "s", ["t"], "r") is None
        assert get_memory_store(db).list_memories() == []


# ── 记忆整理 ─────────────────────────────────────────────────

class TestConsolidate:
    """consolidate_memories (async → sync, llm 注入)。"""

    def _seed_two(self, db):
        store = get_memory_store(db)
        store.save_memory(name="m1", description="d1", content="body1")
        store.save_memory(name="m2", description="d2", content="body2")

    def test_consolidate_merges_and_marks(self, db):
        self._seed_two(db)
        llm = FakeLLM(chat_text=json.dumps([
            {"name": "合并记忆", "description": "合并", "type": "project",
             "content": "合并正文"}]))
        progress: list[tuple[int, str]] = []
        result = consolidate_memories(llm, db,
                                      on_progress=lambda p, s: progress.append((p, s)))
        assert result["consolidated"] == 1
        assert result["total"] == 2
        assert result["detail"].startswith("整理为 1 条精炼记忆")
        assert progress[0] == (5, "准备中...")
        assert progress[-1] == (100, "完成")
        assert llm.chat_calls[0]["stage"] == "chatbi.memory.consolidate"
        by_name = {e["name"]: e for e in get_memory_store(db).list_memories()}
        assert by_name["m1"]["consolidated"] is True    # 原记忆标记隐藏
        assert by_name["m2"]["consolidated"] is True
        merged = by_name["合并记忆"]
        assert merged["type"] == "consolidated"
        assert merged["consolidated"] is False
        # 已整理的不进召回
        assert recall_memories("m1", db) == []

    def test_consolidate_dict_wrapped_response(self, db):
        # 兼容: LLM 可能返回 dict 而非 list (源同款容错)
        self._seed_two(db)
        llm = FakeLLM(chat_text=json.dumps(
            {"name": "单条", "description": "d", "type": "project",
             "content": "c"}))
        result = consolidate_memories(llm, db)
        assert result["consolidated"] == 1

    def test_consolidate_too_few(self, db):
        store = get_memory_store(db)
        only = store.save_memory(name="only", description="d", content="c")
        llm = FakeLLM()
        result = consolidate_memories(llm, db)
        assert result == {"consolidated": 0, "total": 1,
                          "detail": "记忆条目较少, 无需整理"}
        assert llm.chat_calls == []                     # 不值得调 LLM
        # ids 过滤到 1 条 → 同款早退
        result = consolidate_memories(llm, db, ids=[only])
        assert result["total"] == 1

    def test_consolidate_skips_linkage(self, db):
        store = get_memory_store(db)
        store.save_memory(name="m1", description="d", content="c")
        store.save_memory(name="linkage-a-b", description="d", content="c",
                          memory_type="linkage",
                          extra_metadata={"tables": ["a", "b"]})
        llm = FakeLLM()
        result = consolidate_memories(llm, db)
        assert result["total"] == 1                     # linkage 不参与整理
        assert llm.chat_calls == []

    def test_consolidate_llm_down_degrades(self, db):
        self._seed_two(db)
        llm = FakeLLM(chat_error=RuntimeError("llm down"))
        result = consolidate_memories(llm, db)
        assert result["consolidated"] == 0
        assert result["detail"].startswith("整理失败")
        # 原记忆未被误标记
        assert all(not e["consolidated"]
                   for e in get_memory_store(db).list_memories())

    def test_consolidate_invalid_llm_output(self, db):
        self._seed_two(db)
        llm = FakeLLM(chat_text="不是 JSON")
        result = consolidate_memories(llm, db)
        assert result["detail"] == "LLM 未返回有效结果"


# ── 查询流水 ─────────────────────────────────────────────────

class TestSaveQueryMemory:
    """save_query_memory (recent_queries 行 + 容量控制)。"""

    def _recent(self, db):
        matched = [e for e in get_memory_store(db).list_memories()
                   if e["name"] == "recent_queries"]
        return matched[0] if matched else None

    def test_append_and_capacity(self, db):
        save_query_memory("订单数是多少", ["orders"], db)
        entry = self._recent(db)
        assert entry is not None
        body = get_memory_store(db).read_memory(entry["id"])
        assert "订单数是多少" in body and "orders" in body
        save_query_memory("GMV 多少", ["orders", "users"], db)
        body = get_memory_store(db).read_memory(entry["id"])
        assert "GMV 多少" in body and "订单数是多少" in body
        # 容量控制: >200 行 → 保留最近 100 行 (源 frontmatter+后 100 行的行版)
        store = get_memory_store(db)
        store.save_memory(name="recent_queries", description="d",
                          content="\n".join(f"- 行{i}" for i in range(200)),
                          memory_type="project", mem_id=entry["id"])
        save_query_memory("最新问题", ["orders"], db)
        body = store.read_memory(entry["id"])
        assert len(body.split("\n")) == 100
        assert "最新问题" in body
        assert "- 行0" not in body

    def test_empty_question_noop(self, db):
        save_query_memory("", ["orders"], db)
        assert self._recent(db) is None

    def test_fail_open_on_broken_db(self):
        # 存储挂 → 不抛 (logger.debug, 源同款不阻塞)
        save_query_memory("q", ["t"], BrokenDB())


# ── 链路经验沉淀 ─────────────────────────────────────────────

class TestLinkageMemory:
    """persist_linkage_memory + JOIN 提取辅助 (源 test_graph_feedback_e2e 行为)。"""

    def test_extract_join_pairs_simple(self):
        pairs = _extract_join_pairs("biz_orders.user_id = biz_users.id")
        assert pairs == {("biz_orders", "biz_users")}

    def test_extract_join_pairs_multihop(self):
        section = ("biz_orders LEFT JOIN st_shops ON biz_orders.shop_id = st_shops.id (confidence=1.0)\n"
                   "st_shops LEFT JOIN pd_categories ON st_shops.category_id = pd_categories.id (confidence=1.0)")
        assert _extract_join_pairs(section) == {
            ("biz_orders", "st_shops"), ("pd_categories", "st_shops")}

    def test_extract_join_on_conditions(self):
        section = ("biz_orders LEFT JOIN st_shops ON biz_orders.shop_id = st_shops.id (confidence=1.0)\n"
                   "st_shops LEFT JOIN pd_categories ON st_shops.category_id = pd_categories.id (confidence=1.0)")
        # 直接关联: 提取纯 ON 条件 (去 confidence 标注)
        assert _extract_join_on_conditions(section, "biz_orders", "st_shops") == [
            {"on": "biz_orders.shop_id = st_shops.id", "join_type": "LEFT"}]
        # 间接关联: 无直接 ON
        assert _extract_join_on_conditions(section, "biz_orders", "pd_categories") == []

    def test_create_and_update_linkage(self, db):
        state = _linkage_state(["biz_orders", "biz_users"],
                               "biz_orders.user_id = biz_users.id",
                               question="本月各品类销售额")
        persist_linkage_memory(db, state, conv_id="conv-1")
        entry = get_memory_store(db).get_linkage_memory("biz_users", "biz_orders")
        assert entry is not None
        assert entry["name"] == "linkage-biz_orders-biz_users"
        assert entry["co_occurrence"] == 1
        assert entry["tables"] == ["biz_orders", "biz_users"]
        assert entry["join_paths"] == [{"on": "biz_orders.user_id = biz_users.id",
                                        "join_type": "LEFT"}]
        assert entry["scenes"] == ["本月各品类销售额"]
        assert entry["conversation_id"] == "conv-1"
        body = get_memory_store(db).read_memory(entry["id"])
        assert "## JOIN 路径" in body
        assert "## 典型场景" in body
        # 第二次共现: co+1, 场景去重不重复追加
        persist_linkage_memory(db, state, conv_id="conv-2")
        entry2 = get_memory_store(db).get_linkage_memory("biz_orders", "biz_users")
        assert entry2["co_occurrence"] == 2
        assert entry2["scenes"] == ["本月各品类销售额"]
        assert entry2["conversation_id"] == "conv-2"

    def test_aggregation_extracted_and_cleaned(self, db):
        state = _linkage_state(["a", "b"], "a.id = b.aid",
                               aggregation="SUM(actual_amount) 按 category 分组")
        persist_linkage_memory(db, state)
        entry = get_memory_store(db).get_linkage_memory("a", "b")
        assert entry["aggregation"] == "SUM"   # 只存聚合关键字
        assert "## 聚合方式\nSUM" in get_memory_store(db).read_memory(entry["id"])

    def test_single_table_skipped(self, db):
        persist_linkage_memory(db, _linkage_state(["orders"]))
        assert get_memory_store(db).list_memories() == []

    def test_indirect_annotation(self, db):
        # 无直接 JOIN 段 → 间接关联标注
        state = _linkage_state(["orders", "users", "regions"],
                               question="各区域用户数")
        persist_linkage_memory(db, state)
        entry = get_memory_store(db).get_linkage_memory("orders", "users")
        assert "间接关联（经由 其他表）" in \
            get_memory_store(db).read_memory(entry["id"])

    def test_multihop_state_via_intermediary(self, db):
        section = ("orders LEFT JOIN st_shops ON orders.shop_id = st_shops.id (confidence=1.0)\n"
                   "st_shops LEFT JOIN pd_categories ON st_shops.category_id = pd_categories.id (confidence=1.0)")
        state = _linkage_state(["orders", "st_shops", "pd_categories"],
                               join_section=section, question="各品类订单额")
        persist_linkage_memory(db, state)
        store = get_memory_store(db)
        direct = store.get_linkage_memory("orders", "st_shops")
        assert direct["co_occurrence"] == 1
        # 间接表对: 标注经由中间表
        indirect = store.get_linkage_memory("orders", "pd_categories")
        assert "间接关联（经由 st_shops）" in \
            store.read_memory(indirect["id"])
        # 三对表 → 三条 linkage 记忆
        assert len([e for e in store.list_memories()
                    if e["type"] == "linkage"]) == 3

    def test_accepts_store_instance(self, db):
        # 源签名 mem_store 在前: 传 store 实例与传 db 等价 (graph_infer 消费方)
        store = get_memory_store(db)
        persist_linkage_memory(store, _linkage_state(["a", "b"], "a.id = b.aid"))
        assert store.get_linkage_memory("a", "b") is not None


# ── 指标反哺 ─────────────────────────────────────────────────

class TestMetricFeedback:
    """persist_metric_feedback 完整闭环 (源 TestMetricFeedback 行为对齐)。"""

    def test_hit_known_metric_increments_in_semantic_json(self, db):
        _insert_semantic_row(db, _make_content(co_occurrence=2))
        content = _make_content(co_occurrence=2)
        hits = persist_metric_feedback(
            db, None, "SELECT SUM(total_amount) FROM orders", content, DS1,
            conv_id="conv-1")
        assert len(hits) == 1
        hit = hits[0]
        assert hit["table"] == "orders"
        assert hit["metric"] == "gmv"
        assert hit["display_name"] == "成交总额"
        assert hit["co_occurrence"] == 3          # 递增后累计值
        assert hit["source"] == "auto_inferred"
        assert hit["type"] == "single"
        # 源语义: 内存中对象不变, DB 层原子递增 (F2 防竞态)
        assert content.models[0].metrics[0].co_occurrence == 2
        data = _semantic_json_in_db(db)
        assert data["models"][0]["metrics"][0]["co_occurrence"] == 3
        # 再次命中 → 继续递增 (越用越可信)
        hits = persist_metric_feedback(
            db, None, "SELECT SUM(total_amount) FROM orders",
            _make_content(co_occurrence=3), DS1)
        assert hits[0]["co_occurrence"] == 4
        assert _semantic_json_in_db(db)["models"][0]["metrics"][0]["co_occurrence"] == 4

    def test_no_aggregation_no_feedback(self, db):
        _insert_semantic_row(db, _make_content())
        hits = persist_metric_feedback(
            db, None, "SELECT * FROM orders WHERE id = 1", _make_content(), DS1)
        assert hits == []
        assert get_memory_store(db).list_memories() == []

    def test_unknown_table_no_feedback(self, db):
        # 源: not state.current_tables → []
        _insert_semantic_row(db, _make_content())
        hits = persist_metric_feedback(
            db, None, "SELECT SUM(x) FROM unknown_t", _make_content(), DS1)
        assert hits == []

    def test_empty_sql_no_feedback(self, db):
        assert persist_metric_feedback(db, None, "", _make_content(), DS1) == []

    def test_new_pattern_single_table_writes_suggestion(self, db):
        _insert_semantic_row(db, _make_content())
        hits = persist_metric_feedback(
            db, None, "SELECT COUNT(*) FROM orders", _make_content(), DS1,
            conv_id="conv-9", question="订单量是多少")
        assert hits == []                          # 无已知指标命中
        entries = get_memory_store(db).list_memories()
        sug = _entry_by_name(entries, "metric-suggestion-orders-count-*")
        assert sug["type"] == "metric_suggestion"
        assert sug["table"] == "orders"
        assert sug["formula"] == "COUNT(*)"
        assert sug["conversation_id"] == "conv-9"
        body = get_memory_store(db).read_memory(sug["id"])
        assert "## 新指标建议" in body
        assert "表: orders" in body
        assert "聚合: COUNT(*)" in body
        assert "来源 SQL: SELECT COUNT(*) FROM orders" in body
        assert "来源问题: 订单量是多少" in body
        assert "建议在语义层页面添加此指标定义。" in body

    def test_suggestion_dedup_by_name(self, db):
        _insert_semantic_row(db, _make_content())
        persist_metric_feedback(db, None, "SELECT COUNT(*) FROM orders",
                                _make_content(), DS1)
        persist_metric_feedback(db, None, "SELECT COUNT(*) FROM orders",
                                _make_content(), DS1)
        names = [e["name"] for e in get_memory_store(db).list_memories()]
        assert names.count("metric-suggestion-orders-count-*") == 1

    def test_covered_aggregation_no_suggestion(self, db):
        # 命中已知指标 → 已覆盖, 不写 suggestion
        _insert_semantic_row(db, _make_content())
        persist_metric_feedback(db, None, "SELECT SUM(total_amount) FROM orders",
                                _make_content(), DS1)
        assert get_memory_store(db).list_memories() == []

    def test_multi_table_join_no_suggestion(self, db):
        # 源 test_multi_table_query_no_new_suggestion: 多表 JOIN 不建议
        _insert_semantic_row(db, _make_content())
        persist_metric_feedback(
            db, None,
            "SELECT SUM(o.total_amount) FROM orders o JOIN users u "
            "ON o.user_id = u.id",
            _make_content(), DS1, question="q")
        assert get_memory_store(db).list_memories() == []
        assert persist_metric_feedback(
            db, None,
            "SELECT SUM(o.total_amount) FROM orders o JOIN users u "
            "ON o.user_id = u.id",
            _make_content(), DS1) == []

    def test_no_known_metrics_no_suggestion(self, db):
        # 源条件 `and known_metrics`: 表无已知指标 → 宁缺毋滥不猜
        _insert_semantic_row(db, SemanticModelContent(models=[
            Model(name="orders", display_name="订单表",
                  columns=[Column(name="id", display_name="主键",
                                  data_type="BIGINT")])]))
        persist_metric_feedback(db, None, "SELECT COUNT(*) FROM orders",
                                SemanticModelContent(models=[
                                    Model(name="orders", display_name="订单表",
                                          columns=[Column(name="id",
                                                          display_name="主键",
                                                          data_type="BIGINT")])]),
                                DS1)
        assert get_memory_store(db).list_memories() == []

    def test_missing_semantic_row_hits_fallback(self, db):
        # 语义层行缺失: 不持久化 (源 `if not sm: return`), hits 回退 旧值+delta
        content = _make_content(co_occurrence=5)
        hits = persist_metric_feedback(
            db, None, "SELECT SUM(total_amount) FROM orders", content, DS1)
        assert len(hits) == 1
        assert hits[0]["co_occurrence"] == 6

    def test_suggestion_failure_does_not_block_hits(self, db, monkeypatch):
        # suggestion 写失败 → 逐条捕获 (源同款), 命中统计不受影响
        _insert_semantic_row(db, _make_content())

        class BoomStore:
            def list_memories(self):
                return []

            def save_memory(self, *a, **k):
                raise RuntimeError("boom")

        monkeypatch.setattr("domains.chatbi.memory.get_memory_store",
                            lambda _db: BoomStore())
        hits = persist_metric_feedback(
            db, None,
            "SELECT SUM(total_amount), COUNT(id) FROM orders",
            _make_content(), DS1)
        assert len(hits) == 1 and hits[0]["metric"] == "gmv"

    def test_storage_error_propagates_to_caller(self):
        # co_occurrence 持久化失败 → 上抛, 由调用方 (ask_data try/except)
        # 兜底为 metric_hits=[] —— 与源"调用侧 except → 无命中"一致的 fail-open
        with pytest.raises(RuntimeError):
            persist_metric_feedback(
                BrokenDB(), None, "SELECT SUM(total_amount) FROM orders",
                _make_content(), DS1)


# ── SQL 表名解析辅助 ─────────────────────────────────────────

class TestExtractSqlTables:
    """_extract_sql_tables (表集来源适配的规则锚点)。"""

    def test_simple(self):
        assert _extract_sql_tables("SELECT SUM(total_amount) FROM orders") == ["orders"]

    def test_comma_from_and_alias(self):
        sql = "SELECT * FROM orders o, users u WHERE o.user_id = u.id"
        assert _extract_sql_tables(sql) == ["orders", "users"]

    def test_join_and_qualified(self):
        sql = ("SELECT SUM(o.total_amount) FROM orders o "
               "LEFT JOIN users u ON o.user_id = u.id GROUP BY u.id")
        assert _extract_sql_tables(sql) == ["orders", "users"]

    def test_quoted_and_schema_qualified(self):
        sql = 'SELECT * FROM "sch"."orders" INNER JOIN "users" ON 1=1'
        assert _extract_sql_tables(sql) == ["orders", "users"]

    def test_subquery_noise_filtered_by_intersection(self):
        sql = "SELECT SUM(x) FROM (SELECT x FROM orders) t"
        assert _extract_sql_tables(sql) == ["orders"]

    def test_empty(self):
        assert _extract_sql_tables("") == []


# ── DDL 契约 ─────────────────────────────────────────────────

def test_memory_ddl_only_own_tables():
    """本栈 DDL 只含自己的表 (chatbi_ 前缀;不越界建他栈的表)。"""
    ddl_text = "\n".join(CHATBI_MEMORY_DDL)
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", ddl_text)
    assert tables == ["chatbi_agent_memories"]
    assert all(t.startswith("chatbi_") for t in tables)


def test_store_accepts_db_and_is_duck_compatible(db):
    """graph_infer 鸭子契约: list_memories() -> list[dict] 且含 type/tables/
    co_occurrence 字段 (linkage_memories_to_cooccurrence 的消费形态)。"""
    store = ChatBIMemoryStore(db)
    store.save_memory(name="linkage-a-b", description="d", content="c",
                      memory_type="linkage",
                      extra_metadata={"co_occurrence": 4, "tables": ["a", "b"]})
    entries = store.list_memories()
    assert isinstance(entries, list) and all(isinstance(e, dict) for e in entries)
    linkage = entries[0]
    assert linkage["type"] == "linkage"
    assert linkage["co_occurrence"] == 4
    assert linkage["tables"] == ["a", "b"]


class TestWalkthroughFixes:
    """走查修复回归锚(B1/B2/B3)——这些路径此前从未成功执行过,
    815 全绿也漏掉,必须显式守护。"""

    def test_b1_memory_extraction_executes(self, db):
        """B1: result_summary 未定义曾致 NameError 被 except 吞→记忆抽取全死。
        修复后 extract_and_save_memory 必须真实返回记忆(而非静默 None)。"""
        from domains.chatbi.memory import extract_and_save_memory
        from tests.domains.test_chatbi_pipeline import FakeLLM
        llm = FakeLLM({"chatbi.memory.extract": {
            "should_save": True, "name": "b1_anchor",
            "description": "回归锚", "content": "B1 修复后的记忆"}})
        result = extract_and_save_memory(
            llm, db, "问题", "SELECT 1", ["t"],
            "查询完成, 返回 1 行", conv_id="b1-anchor")
        assert result is not None and result.get("name") == "b1_anchor"

    def test_b2_sync_linkage_signature(self, db):
        """B2: sync_linkage_to_graph 参数错位曾致 AttributeError。
        修复后 (db, mem_store, ds_id) 签名必须可执行不抛。"""
        from domains.chatbi.memory import get_memory_store
        from domains.chatbi.models import SemanticModelContent, Model
        from domains.chatbi.graph_infer import sync_linkage_to_graph
        content = SemanticModelContent(models=[Model(name="t1", display_name="T1")])
        mem_store = get_memory_store(db)
        # 只验证签名可执行(空记忆/空关系→空结果, 不炸即过)
        r = sync_linkage_to_graph(db, mem_store, "b2-anchor-ds")
        assert isinstance(r, dict)

    def test_b3_linkage_scenes_populated(self, db):
        """B3: state['question'] 键不存在曾致 scenes 恒空。
        修复后 user_input 键必须写入 linkage 记忆的典型场景。"""
        from domains.chatbi.memory import persist_linkage_memory, list_memories, delete_memory
        state = {"user_input": "b3场景锚定问题", "current_tables": ["ta", "tb"],
                 "join_path_section": "ta LEFT JOIN tb ON ta.x = tb.x",
                 "sql": "SELECT 1", "thinking": {}}
        persist_linkage_memory(db, state, conv_id="b3-anchor")
        links = [m for m in list_memories(db, limit=50)
                 if m.get("type") == "linkage" and "b3场景锚定问题" in (m.get("scenes") or [])]
        assert links, "scenes 应含 user_input 文本(B3 修复前恒空)"
        for m in links:
            delete_memory(db, m["id"])
