"""chatbi 管线集成测试 —— ask_data / switch_chart 端到端(FakeLLM + 真 PG)。

对标 ChatBI agent.py 的主循环行为;覆盖:
  完整查询管线(检索降级路径)/自愈回环(执行失败→heal→成功)/
  澄清场景②(表不确定)/澄清场景③(结果异常→自动修正成功不 ask→ask 兜底)/
  多轮继承(prev_tables)/switch_chart(含 S7 重校验)。

跑法: TEST_DATABASE_URL=...chatbi_test_pipeline ./venv/bin/python -m pytest
    tests/domains/test_chatbi_pipeline.py -q
"""
import json
import uuid

import pytest

from domains.chatbi import datasources
from domains.chatbi.tools.ask_data import AskDataTool
from domains.chatbi.tools.switch_chart import SwitchChartTool


# ── FakeLLM: 按 stage 可编程(chat 返回 (content, meta) 元组) ──

class FakeLLM:
    def __init__(self, script: dict):
        self.script = script   # {stage 或 "*": content 或 callable(**kw)->str}
        self.calls = []

    def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
        self.calls.append({"stage": stage, "user": messages[-1]["content"]})
        for key in (stage, "*"):
            if key in self.script:
                v = self.script[key]
                if callable(v):
                    v = v(messages=messages)
                # dict 编排项自动序列化(对齐真 LLMClient.chat 的 str 返回)
                return (json.dumps(v, ensure_ascii=False)
                        if isinstance(v, dict) else v), {}
        raise RuntimeError(f"FakeLLM: stage {stage} 未编排")

    def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None):
        self.calls.append({"stage": stage})
        for key in (stage, "*"):
            if key in self.script:
                v = self.script[key]
                return v() if callable(v) else v
        raise RuntimeError(f"FakeLLM.chat_json: {stage} 未编排")


# ── 测试环境: 真实 biz 表 + 数据源行 + 语义层(FakeLLM 扫描) ────

@pytest.fixture()
def env(test_url=""):
    import psycopg
    import os
    url = os.environ["DATABASE_URL"]
    dbname = url.rsplit("/", 1)[-1]
    with psycopg.connect(url) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS public")
        conn.execute("DROP TABLE IF EXISTS public.biz_orders CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.biz_users CASCADE")
        conn.execute("""CREATE TABLE public.biz_users (
            id BIGSERIAL PRIMARY KEY, username VARCHAR(128), city VARCHAR(64),
            vip_level SMALLINT DEFAULT 0, created_at TIMESTAMP DEFAULT NOW())""")
        conn.execute("COMMENT ON TABLE public.biz_users IS '用户表'")
        conn.execute("COMMENT ON COLUMN public.biz_users.city IS '所在城市'")
        conn.execute("""CREATE TABLE public.biz_orders (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT REFERENCES public.biz_users(id),
            total_amount NUMERIC(10,2), status VARCHAR(32),
            created_at TIMESTAMP DEFAULT NOW())""")
        conn.execute("COMMENT ON TABLE public.biz_orders IS '订单表'")
        conn.execute("COMMENT ON COLUMN public.biz_orders.total_amount IS '订单金额'")
        conn.execute("COMMENT ON COLUMN public.biz_orders.status IS '订单状态'")
        conn.execute("INSERT INTO public.biz_users (username, city) VALUES ('张三','北京'),('李四','上海')")
        conn.execute("INSERT INTO public.biz_orders (user_id, total_amount, status) "
                     "VALUES (1, 100.50, 'paid'), (1, 200.00, 'shipped'), (2, 50.00, 'paid')")
        conn.commit()

    from sdk.relational_store import PackRelationalDB
    db = PackRelationalDB("chatbi")
    from domains.chatbi.datasources import init_store, configure_encryption
    from cryptography.fernet import Fernet
    configure_encryption(Fernet.generate_key().decode())
    init_store(db)
    ds = datasources.create_datasource(
        db, "演示库", "postgresql", "127.0.0.1", 5432,
        dbname, "root", "root")

    # 语义层: FakeLLM 扫描(LLM 富化/指标/示例问题全降级也能落库)
    from domains.chatbi import semantic
    llm = FakeLLM({})  # 全部抛错 → 走规则降级链
    content = semantic.scan_datasource(
        llm=llm, db=db, connect_info={"host": "127.0.0.1", "port": 5432,
                                      "user": "root", "password": "root",
                                      "database": dbname, "db_type": "postgresql"})
    assert content is not None and content.models, "扫描应产出语义层"

    # 熔断器重置:进程级单例(跨查询连续失败熔断是 ChatBI 原语义),
    # 测试用例间的失败会互相污染自愈配额,每个用例从 closed 态开始
    from domains.chatbi.healer import reset_circuit_breaker
    reset_circuit_breaker()

    # 会话记忆(真 ConversationStore, conftest 已清表)
    from services.conversation_store import ConversationStore
    from engine.conversation import ConversationManager
    from sdk.tool import SessionStateHandle
    conv_store = ConversationStore()
    cm = ConversationManager(store=conv_store)
    conv_id = conv_store.create_conversation("tester")["id"]

    ctx_kw = dict(
        llm_client=None, asset_client=None, conversation=cm,
        emit=lambda *a, **k: None, conv_id=conv_id, registry=None,
        session_state=SessionStateHandle(conv_store, conv_id, "chatbi"),
    )

    tool = AskDataTool(db=db, settings={
        "graph_enabled": False,  # 集成测试关图谱(2 表场景), 走检索降级路径
        "retrieve_top_k": 5, "fewshot_top_k": 2,
    })
    yield {"db": db, "ds": ds, "content": content, "tool": tool,
           "ctx_kw": ctx_kw, "conv_store": conv_store, "conv_id": conv_id,
           "cm": cm}


def _run(env, question, llm, session_state=None):
    """构造 state + ctx 跑一次 ask_data。"""
    from sdk.tool import ToolContext
    kw = dict(env["ctx_kw"])
    if session_state is not None:
        kw["session_state"] = session_state
    kw["llm_client"] = llm
    ctx = ToolContext(**kw)
    state = {"user_input": question}
    result = env["tool"].execute(state, ctx)
    return result, state


def _sess(env):
    from sdk.tool import SessionStateHandle
    return SessionStateHandle(env["conv_store"], env["conv_id"], "chatbi")


# ── 场景 1: 完整查询管线(向量设施不可用 → 全表降级 → FakeLLM 生成) ──

class TestFullPipeline:
    def test_end_to_end_query_and_chart(self, env):
        sql = ('SELECT u.city AS "城市", COUNT(*) AS "订单数" FROM public.biz_orders o '
               'JOIN public.biz_users u ON o.user_id = u.id GROUP BY u.city')
        llm = FakeLLM({
            "chatbi.think": {"tables": ["biz_orders(订单表)"], "aggregation": "COUNT",
                             "caveats": [], "prev_sql_review": ""},
            "chatbi.generate_sql": f"```sql\n{sql}\n```",
            "chatbi.chart": {"chart_type": "bar", "dim_col": "城市",
                             "measure_cols": ["订单数"]},
        })
        result, state = _run(env, "各城市的订单数量", llm)
        assert result.error_for_llm is None, f"管线失败: {result.error_for_llm}"
        assert result.artifact["sql"].replace("public.", "").lower().startswith("select")
        assert result.artifact["rowcount"] >= 1
        assert result.formatted["chart"]["series"][0]["type"] == "bar"
        assert "命中指标" not in result.summary  # 无指标反哺时不虚报
        # thinking 进制品
        assert result.artifact["thinking"]["aggregation"] == "COUNT"
        # 会话状态写回(多轮继承的载体)
        sess = _sess(env)
        assert sess.get("prev_sql")
        assert "biz_orders" in (sess.get("prev_tables") or [])

    def test_sql_validation_rejects_write(self, env):
        llm = FakeLLM({"chatbi.think": {"tables": [], "aggregation": "", "caveats": []},
                       "chatbi.generate_sql": "DROP TABLE biz_orders",
                       "*": "x"})
        result, _ = _run(env, "删表", llm)
        # 校验失败 → 进自愈循环 → 耗尽 → 失败(AEE-002 + T030)。
        # 自愈耗尽属内部错误 → summary 脱敏(源 error_is_internal 语义),
        # 原文只进 error_for_llm 供引擎重试判定
        assert result.error_for_llm and "SQL 失败" in result.error_for_llm
        assert "查询执行失败" in result.summary
        assert "DROP" not in result.summary


# ── 场景 2: 自愈回环(执行失败 → heal → 成功) ─────────────────

class TestSelfHealLoop:
    def test_heal_on_execution_failure(self, env):
        bad_sql = 'SELECT nonexist_col FROM public.biz_orders'
        good_sql = 'SELECT status AS "状态", COUNT(*) AS "单数" FROM public.biz_orders GROUP BY status'
        stages = {"generate": [bad_sql, good_sql]}

        class HealLLM(FakeLLM):
            def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
                if stage == "chatbi.generate_sql":
                    sql = stages["generate"].pop(0) if stages["generate"] else good_sql
                    return f"```sql\n{sql}\n```", {}
                if stage == "chatbi.heal":
                    return f"```sql\n{good_sql}\n```", {}
                if stage == "chatbi.chart":
                    return ('{"chart_type": "table"}', {})
                if stage == "chatbi.think":
                    return ({"tables": [], "aggregation": "", "caveats": []}), {}
                raise AssertionError(f"意外调用 {stage}")

        result, state = _run(env, "各状态订单数", HealLLM({}))
        assert result.error_for_llm is None
        assert state.get("heal_rounds") == 1  # 一轮自愈成功
        assert state.get("heal_before_sql") == bad_sql  # 记录自愈前 SQL


# ── 场景 3: 澄清场景②(表不确定) ─────────────────────────────

class TestClarifyTable:
    def test_many_tables_no_hit_asks(self, env, monkeypatch):
        # 澄清场景②触发条件 = 检索真无命中(agent.py: 无召回 → ask 换问法);
        # 向量降级全表不算无命中(等价高置信全命中)。
        # 注意: 抛异常的是 stores.get_vector(app_state) → 必须连它一起 patch,
        # 否则走"向量设施故障→全表降级"分支而非无命中分支。
        import domains.chatbi.retrieval as retrieval_mod
        import domains.chatbi.stores as stores_mod
        from domains.chatbi.retrieval import RetrievalResult

        def fake_retrieve_context(*a, **kw):
            return {"retrieval": RetrievalResult(models=[], no_match_reason="无匹配"),
                    "model_names": [], "schema_context": "", "allowed_columns": set(),
                    "metrics_hint": ""}
        monkeypatch.setattr(stores_mod, "get_vector",
                            lambda app_state: object())  # 不触达真 Milvus
        monkeypatch.setattr(retrieval_mod, "retrieve_context", fake_retrieve_context)
        env["tool"]._settings = dict(env["tool"]._settings, clarify_table_threshold=1)
        llm = FakeLLM({"*": "x"})
        result, _ = _run(env, "随便看看", llm)
        assert result.ask is not None
        q = result.ask.questions[0]
        assert q.header == "选择数据表"
        assert len(q.options) >= 2

    def test_few_tables_no_hit_asks_with_reason(self, env, monkeypatch):
        # 表数 ≤ 阈值 + 检索无召回 → no_match 澄清(问题=检索原因,
        # 选项=全量表供点名;不再空 schema 硬跑——管线-C1 修复)
        import domains.chatbi.retrieval as retrieval_mod
        import domains.chatbi.stores as stores_mod
        from domains.chatbi.retrieval import RetrievalResult

        def fake_retrieve_context(*a, **kw):
            return {"retrieval": RetrievalResult(models=[], no_match_reason="无法匹配到相关表"),
                    "model_names": [], "schema_context": "", "allowed_columns": set(),
                    "metrics_hint": ""}
        monkeypatch.setattr(stores_mod, "get_vector",
                            lambda app_state: object())
        monkeypatch.setattr(retrieval_mod, "retrieve_context", fake_retrieve_context)
        # 阈值 > 表数(默认 8, env 只有 3 表) → 走 no_match 分支
        llm = FakeLLM({"*": "x"})
        result, _ = _run(env, "完全不相关的问题", llm)
        assert result.ask is not None
        q = result.ask.questions[0]
        assert "无法匹配到相关表" in q.question
        assert q.header == "未匹配到数据表"

    def test_vector_down_many_tables_fail_closed(self, env, monkeypatch):
        # 向量设施故障 + 大 schema(表 > 阈值) → 不再全表灌种子,
        # 走表选择澄清让用户点名(I2 修复: prompt 膨胀且违背宁缺毋滥)
        import domains.chatbi.stores as stores_mod

        def boom(app_state):
            raise RuntimeError("milvus down")
        monkeypatch.setattr(stores_mod, "get_vector", boom)
        env["tool"]._settings = dict(env["tool"]._settings, clarify_table_threshold=1)
        llm = FakeLLM({"*": "x"})
        result, _ = _run(env, "各城市订单量", llm)
        assert result.ask is not None  # 澄清: 用户点名表
        assert result.ask.questions[0].header == "选择数据表"
        # 种子为空(不再全表灌入) — extra 里 tables 是全量清单供点名
        assert result.extra.get("clarify_kind") == "table_confirm"


# ── 场景 4: 澄清场景③(结果异常 → 自动修正 → ask 兜底) ────────

class TestClarifyResultAbnormal:
    def test_auto_heal_on_all_null_then_success(self, env):
        # 首版 SQL 产生全 NULL JOIN → 自检异常 → suggestion 自动修正 → 复检通过
        # 注意: 单列全空是源设计放行(result_checker"单列全空不算异常"),
        # 需两列全空才触发 ALL_NULL
        null_sql = ('SELECT u.city AS "城市", u.username AS "用户名" '
                    'FROM public.biz_orders o LEFT JOIN public.biz_users u '
                    'ON o.user_id = u.id AND u.id = -1')
        good_sql = 'SELECT status AS "状态", COUNT(*) AS "单数" FROM public.biz_orders GROUP BY status'

        class AutoHealLLM(FakeLLM):
            def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
                if stage == "chatbi.generate_sql":
                    return f"```sql\n{null_sql}\n```", {}
                if stage == "chatbi.heal":
                    # error_detail 通道: 自检修正的 prompt 应含 ALL_NULL 统计描述
                    assert "NULL" in messages[-1]["content"]
                    return f"```sql\n{good_sql}\n```", {}
                if stage == "chatbi.chart":
                    return '{"chart_type": "table"}', {}
                raise AssertionError(stage)

        result, state = _run(env, "看看数据", AutoHealLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []}}))
        assert result.error_for_llm is None
        assert result.ask is None  # 自动修正成功 → 不问用户
        assert state["check"].ok

    def test_ask_when_auto_heal_exhausted(self, env):
        null_sql = ('SELECT u.city AS "城市", u.username AS "用户名" '
                    'FROM public.biz_orders o LEFT JOIN public.biz_users u '
                    'ON o.user_id = u.id AND u.id = -1')

        class NoFixLLM(FakeLLM):
            def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
                if stage == "chatbi.generate_sql":
                    return f"```sql\n{null_sql}\n```", {}
                if stage == "chatbi.heal":
                    return f"```sql\n{null_sql}\n```", {}  # 修正无效
                raise AssertionError(stage)

        result, _ = _run(env, "看看数据", NoFixLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []}}))
        assert result.ask is not None  # 自动修正失败 → ask_user(场景③)
        assert result.ask.questions[0].header == "结果异常"


# ── 场景 5: 多轮继承(prev_tables 进 state) ───────────────────

class TestMultiTurn:
    def test_prev_tables_merged_on_followup(self, env):
        sql1 = 'SELECT status AS "状态", COUNT(*) AS "单数" FROM public.biz_orders GROUP BY status'
        sql2 = ('SELECT u.city AS "城市", COUNT(*) AS "单数" FROM public.biz_orders o '
                'JOIN public.biz_users u ON o.user_id = u.id GROUP BY u.city')
        llm = FakeLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []},
            "chatbi.generate_sql": lambda **kw: f"```sql\n{sql1}\n```",
            "chatbi.chart": {"chart_type": "table"},
        })
        result1, state1 = _run(env, "各状态订单数", llm)
        assert result1.error_for_llm is None
        prev_tables = _sess(env).get("prev_tables")
        assert "biz_orders" in prev_tables

        # 第二轮: 生成器记录收到的 schema_context(应含继承表)
        seen = {}

        class WatchLLM(FakeLLM):
            def chat(self, messages=None, temperature=None, stage=None, conv_id=None):
                if stage == "chatbi.generate_sql":
                    seen["ctx"] = messages[0]["content"] + messages[-1]["content"]
                    return f"```sql\n{sql2}\n```", {}
                if stage == "chatbi.think":
                    return ({"tables": [], "aggregation": "", "caveats": []}), {}
                if stage == "chatbi.chart":
                    return ('{"chart_type": "table"}', {})
                raise AssertionError(stage)

        result2, state2 = _run(env, "按城市呢", WatchLLM({}))
        assert result2.error_for_llm is None
        # 继承表出现在 SQL 生成上下文(检索降级全表 + prev 合并, biz_users 都在)
        assert "biz_users" in state2["current_tables"]


# ── 场景 6: switch_chart(S7 重校验 + 类型切换) ────────────────

class TestSwitchChart:
    def test_switch_after_query(self, env):
        sql = 'SELECT status AS "状态", COUNT(*) AS "单数" FROM public.biz_orders GROUP BY status'
        llm = FakeLLM({
            "chatbi.think": {"tables": [], "aggregation": "", "caveats": []},
            "chatbi.generate_sql": f"```sql\n{sql}\n```",
            "chatbi.chart": {"chart_type": "bar", "dim_col": "状态",
                             "measure_cols": ["单数"]},
        })
        _run(env, "各状态订单数", llm)
        sess = _sess(env)
        assert sess.get("prev_chart_config")["chart_type"] == "bar"

        tool = SwitchChartTool(db=env["db"], settings={})
        from sdk.tool import ToolContext
        ctx = ToolContext(llm_client=None, asset_client=None,
                          conversation=env["cm"], emit=lambda *a, **k: None,
                          conv_id=env["conv_id"], registry=None,
                          session_state=sess)
        result = tool.execute({"user_input": "换成饼图"}, ctx)
        assert result.error_for_llm is None
        assert result.formatted["chart"]["series"][0]["type"] == "pie"
        assert _sess(env).get("prev_chart_config")["chart_type"] == "pie"

    def test_switch_rejects_tampered_prev_sql(self, env):
        # SEC S7: 持久化 prev_sql 被篡改(写操作) → 重校验拦截
        sess = _sess(env)
        sess.set("prev_sql", "DROP TABLE biz_orders")
        tool = SwitchChartTool(db=env["db"], settings={})
        from sdk.tool import ToolContext
        ctx = ToolContext(llm_client=None, asset_client=None,
                          conversation=env["cm"], emit=lambda *a, **k: None,
                          conv_id=env["conv_id"], registry=None,
                          session_state=sess)
        result = tool.execute({"user_input": "换成饼图"}, ctx)
        assert result.error_for_llm and "重新提问" in result.summary
        assert "校验未通过" in result.summary


# ── 场景: 持久化降级可见性(三审 P0: persist_warnings 引用修复) ──────

class TestPersistWarnings:
    """四类持久化失败 → 查询仍成功返回, warning 进 artifact/formatted 对用户可见。

    此前 bug: artifact 在持久化前用 ``state.get(...) or []`` 定格了独立
    空列表, 后续 setdefault 追加到另一个列表——warning 永远传不出去。
    """

    SQL = ('SELECT u.city AS "城市", COUNT(*) AS "订单数" FROM public.biz_orders o '
           'JOIN public.biz_users u ON o.user_id = u.id GROUP BY u.city')

    def _llm(self):
        return FakeLLM({
            "chatbi.think": {"tables": ["biz_orders(订单表)"], "aggregation": "COUNT",
                             "caveats": [], "prev_sql_review": ""},
            "chatbi.generate_sql": f"```sql\n{self.SQL}\n```",
            "chatbi.chart": {"chart_type": "bar", "dim_col": "城市",
                             "measure_cols": ["订单数"]},
        })

    @pytest.mark.parametrize("target,fragment", [
        ("domains.chatbi.memory.extract_and_save_memory", "记忆未沉淀"),
        ("domains.chatbi.memory.persist_linkage_memory", "JOIN经验未沉淀"),
        ("domains.chatbi.m4.save_query", "查询未自动保存"),
        ("domains.chatbi.fewshot.index_fewshot_example", "经验未回流"),
    ])
    def test_each_persist_failure_visible(self, env, monkeypatch, target, fragment):
        import importlib
        module_path, fn_name = target.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        monkeypatch.setattr(
            mod, fn_name,
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))
        result, _ = _run(env, "各城市的订单数量", self._llm())
        # 查询本身成功(持久化 fail-open)
        assert result.error_for_llm is None
        assert result.artifact["rowcount"] >= 1
        # warning 随 artifact 落库(历史恢复可见)
        warnings = result.artifact.get("persist_warnings") or []
        assert any(fragment in w for w in warnings), f"{fragment} 不在 {warnings}"
        # format_result 钩子透出(引擎合并进 SSE result, 前端消息卡可见)
        formatted = env["tool"].format_result(result.artifact)
        assert any(fragment in w
                   for w in formatted.get("persistWarnings") or [])

    def test_no_unexpected_warnings(self, env, monkeypatch):
        # 三段注入 no-op(记忆/linkage/保存查询); fewshot 在无向量配置的
        # 测试环境参数求值即失败(环境性, 其可见性由参数化用例覆盖)——
        # 断言: 其余三段零 warning, 出现的 warning 均为已知四类前缀
        import domains.chatbi.memory as mem_mod
        import domains.chatbi.m4 as m4_mod
        monkeypatch.setattr(mem_mod, "extract_and_save_memory", lambda *a, **kw: None)
        monkeypatch.setattr(mem_mod, "persist_linkage_memory", lambda *a, **kw: None)
        monkeypatch.setattr(m4_mod, "save_query", lambda *a, **kw: {"id": "x"})
        result, _ = _run(env, "各城市的订单数量", self._llm())
        assert result.error_for_llm is None
        ws = result.artifact.get("persist_warnings") or []
        prefixes = ("记忆未沉淀", "JOIN经验未沉淀", "查询未自动保存", "经验未回流")
        assert all(w.startswith(prefixes) for w in ws), ws
        assert not any(w.startswith(prefixes[:3]) for w in ws), ws
