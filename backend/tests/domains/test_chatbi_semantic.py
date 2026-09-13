"""chatbi 语义层栈测试 —— 内省/LLM 富化/指标推断/示例问题/版本管理/防御校验。

移植对齐锚: 与 chat-bi 的 T013(扫描)/SEM-005(指标)/SEM-004(版本回滚)/
DSO-04(diff) 行为逐点对齐;LLM 用可编程 Fake(.chat/.chat_json 按 stage 出料)。

跑法(专属库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_semantic \\
      ./venv/bin/python -m pytest tests/domains/test_chatbi_semantic.py -q

注意: 业务表建在 public schema(限定名,PackRelationalDB 连接的 search_path
定向在 chatbi schema);conftest 每测试 TRUNCATE 全部业务表(含 chatbi schema),
语义版本/数据源行每测试从零开始。测试库 public 还有宿主平台表(conversations/
task_logs 等,由 conftest 会话级建表产生)——扫描会一并扫到,断言只针对 biz_* 表。
"""
import json
import os
from types import SimpleNamespace

import pytest
from psycopg.conninfo import conninfo_to_dict

# ── 测试库连接信息(与 conftest 引导的库一致;扫描 connect_info 即指向它) ──
_URL = conninfo_to_dict(os.environ["DATABASE_URL"])
TEST_DB = {
    "host": _URL.get("host") or "localhost",
    "port": int(_URL.get("port") or 5432),
    "database": _URL.get("dbname") or "",
    "user": _URL.get("user") or "root",
    "password": _URL.get("password") or "",
}

# ── Fake LLM 出料(按 stage 可编程) ───────────────────────────
STAGE_ENRICH = "chatbi.semantic.enrich"
STAGE_METRICS = "chatbi.semantic.metrics"
STAGE_QUESTIONS = "chatbi.semantic.sample_questions"

# 富化: {"表名": {"列名": "中文名"}}(chat_json 返回 dict)
ENRICH_MAP = {
    "biz_orders": {"remark": "备注", "status": "订单状态"},
    "biz_users": {"user_name": "用户名称", "city": "所在城市"},
}
# 示例问题: 每行一个(chat 返回文本)
QUESTIONS_TEXT = "各城市的订单总金额排名\n订单总金额最高的前10个用户\n各订单状态的数量占比"
# LLM 复合指标: 引用规则推断出的 simple 指标(chat 返回 JSON 数组文本)
METRICS_TEXT = json.dumps([
    {"name": "avg_order_amount", "display_name": "客单价",
     "formula": "total_amount_sum / total_amount_avg", "type": "composite",
     "factor_metric_names": ["total_amount_sum", "total_amount_avg"]},
], ensure_ascii=False)
# 非法指标混合: 非 dict 项 / composite 缺 factor / 合法 single
METRICS_BAD_TEXT = json.dumps([
    {"garbage": True},
    {"name": "bad_composite", "display_name": "坏指标",
     "formula": "a / b", "type": "composite"},
    {"name": "total_amount_max", "display_name": "最大订单金额",
     "formula": "MAX(total_amount)", "type": "single"},
], ensure_ascii=False)


class FakeLLM:
    """可编程假 LLM: .chat/.chat_json 按 stage 返回预设内容;可整体抛错。

    chat 按 (content, meta) 二元组返回(宿主 LLM 契约);
    raise_on_chat=True 时 chat/chat_json 都抛 RuntimeError(模拟 LLM 故障)。
    """

    def __init__(self, chat_by_stage=None, chat_json_by_stage=None,
                 raise_on_chat=False):
        self.chat_by_stage = chat_by_stage or {}
        self.chat_json_by_stage = chat_json_by_stage or {}
        self.raise_on_chat = raise_on_chat
        self.chat_calls = []
        self.json_calls = []

    def chat(self, messages=None, temperature=None, stage=None, conv_id=None, **kw):
        self.chat_calls.append({
            "stage": stage,
            "temperature": temperature,
            "prompt": messages[-1]["content"] if messages else "",
        })
        if self.raise_on_chat:
            raise RuntimeError("LLM 不可用")
        return self.chat_by_stage.get(stage, ""), {"stage": stage}

    def chat_json(self, messages=None, temperature=None, stage=None, conv_id=None, **kw):
        self.json_calls.append({
            "stage": stage,
            "prompt": messages[-1]["content"] if messages else "",
        })
        if self.raise_on_chat:
            raise RuntimeError("LLM 不可用")
        return self.chat_json_by_stage.get(stage, {})


def default_llm(metrics_text: str = METRICS_TEXT) -> FakeLLM:
    """全链路可用的 Fake LLM(富化/指标/示例问题三路都有料)。"""
    return FakeLLM(
        chat_by_stage={STAGE_QUESTIONS: QUESTIONS_TEXT, STAGE_METRICS: metrics_text},
        chat_json_by_stage={STAGE_ENRICH: ENRICH_MAP})


# ── 环境夹具: pack 库 + public 业务表 + 可注册数据源 ─────────────

@pytest.fixture()
def env():
    """chatbi pack 库 + 测试库(public)两张业务表;register() 注册数据源行。

    业务表结构(注释用于验证 manual/auto_inferred 分流与外键关系):
      biz_users(id SERIAL PK, user_name VARCHAR(50), city VARCHAR(30))
      biz_orders(id SERIAL PK, user_id →biz_users(id), total_amount NUMERIC(10,2)
                 [列注释'订单总金额'], status VARCHAR(20), created_at TIMESTAMP,
                 remark TEXT; 表注释'业务订单表')
    """
    from cryptography.fernet import Fernet

    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import configure_encryption, init_store

    configure_encryption(Fernet.generate_key().decode())
    db = PackRelationalDB("chatbi")
    init_store(db)
    # 业务表务必写 public. 前缀(search_path 定向在 chatbi schema)
    with db.connect() as conn:
        conn.execute("DROP TABLE IF EXISTS public.biz_orders CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.biz_users CASCADE")
        conn.execute("""CREATE TABLE IF NOT EXISTS public.biz_users (
            id SERIAL PRIMARY KEY,
            user_name VARCHAR(50),
            city VARCHAR(30))""")
        conn.execute("""CREATE TABLE IF NOT EXISTS public.biz_orders (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES public.biz_users(id),
            total_amount NUMERIC(10,2),
            status VARCHAR(20),
            created_at TIMESTAMP,
            remark TEXT)""")
        conn.execute("COMMENT ON TABLE public.biz_orders IS '业务订单表'")
        conn.execute("COMMENT ON COLUMN public.biz_orders.total_amount IS '订单总金额'")
    # 幂等重跑时清掉业务表数据(表结构/注释保留)
    with db.connect() as conn:
        conn.execute("TRUNCATE public.biz_orders, public.biz_users RESTART IDENTITY CASCADE")

    def register():
        from domains.chatbi import datasources
        return datasources.create_datasource(
            db, "测试电商库", "postgresql", TEST_DB["host"], TEST_DB["port"],
            TEST_DB["database"], TEST_DB["user"], TEST_DB["password"])

    return SimpleNamespace(db=db, connect_info=dict(TEST_DB), register=register)


def _model(content, name):
    """取指定表名的语义模型。"""
    return next(m for m in content.models if m.name == name)


def _versions(db, ds_id):
    """读该数据源的全部语义版本号与 is_current(升序)。"""
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT version, is_current FROM chatbi_semantic_models "
            "WHERE data_source_id = ? ORDER BY version", (ds_id,)).fetchall()
    return [(int(r["version"]), int(r["is_current"])) for r in rows]


# ════════════════════════════════════════════════════════════════
# 扫描流水线: 内省 → 富化 → 指标 → 示例问题 → 版本落库
# ════════════════════════════════════════════════════════════════

class TestScanPipeline:
    def test_full_scan_enrich_metrics_questions_and_save(self, env):
        """全链路: 注释列 manual / LLM 富化列 0.8 / 规则+LLM 指标 / 外键关系 / 落库。"""
        from domains.chatbi import semantic

        info = env.register()
        llm = default_llm()
        content = semantic.scan_datasource(llm, env.db, env.connect_info)

        # 内省范围: biz 表在,chatbi 插件元数据表(独立 schema)不在
        names = {m.name for m in content.models}
        assert {"biz_users", "biz_orders"} <= names
        assert not any(n.startswith("chatbi_") for n in names)

        # 表级: 注释 → manual/1.0;无注释 → 退化表名
        orders = _model(content, "biz_orders")
        users = _model(content, "biz_users")
        assert orders.display_name == "业务订单表" and orders.source == "manual"
        assert orders.confidence == 1.0
        assert users.display_name == "biz_users" and users.confidence == 0.5

        # 列级: 类型规范化 + 语义角色 + 注释/富化分流
        by_name = {c.name: c for c in orders.columns}
        assert by_name["total_amount"].data_type == "NUMERIC(10,2)"
        assert by_name["total_amount"].display_name == "订单总金额"
        assert by_name["total_amount"].source == "manual"
        assert by_name["total_amount"].confidence == 1.0
        assert by_name["total_amount"].semantic_type == "measure"
        assert by_name["id"].semantic_type == "key"          # 主键
        assert by_name["user_id"].semantic_type == "key"     # xxx_id 整型外键
        assert by_name["status"].semantic_type == "dimension"
        assert by_name["created_at"].data_type == "TIMESTAMP"
        assert by_name["created_at"].semantic_type == "dimension"
        assert by_name["remark"].display_name == "备注"       # LLM 富化
        assert by_name["remark"].confidence == 0.8
        assert by_name["status"].display_name == "订单状态"
        ucols = {c.name: c for c in users.columns}
        assert ucols["id"].semantic_type == "key"
        assert ucols["user_name"].display_name == "用户名称"  # LLM 富化
        assert ucols["user_name"].confidence == 0.8
        assert ucols["city"].display_name == "所在城市"

        # 外键关系(foreign_key / N:1 / confidence 1.0)
        assert len(orders.relationships) == 1
        rel = orders.relationships[0]
        assert rel.name == "biz_orders_to_biz_users"
        assert rel.target_model == "biz_users"
        assert rel.on == "biz_orders.user_id = biz_users.id"
        assert rel.type == "N:1" and rel.source == "foreign_key" and rel.confidence == 1.0
        assert users.relationships == []

        # 指标: 规则 simple(金额列 → SUM+AVG) + LLM composite(子指标引用合法)
        metric_names = {m.name for m in orders.metrics}
        assert {"total_amount_sum", "total_amount_avg"} <= metric_names
        sum_metric = next(m for m in orders.metrics if m.name == "total_amount_sum")
        assert sum_metric.formula == "SUM(total_amount)"
        assert sum_metric.display_name == "订单总金额合计"
        assert sum_metric.source == "rule_inferred"
        composite = next(m for m in orders.metrics if m.type == "composite")
        assert composite.name == "avg_order_amount"
        assert set(composite.factor_metric_names) == {"total_amount_sum", "total_amount_avg"}
        assert _model(content, "biz_users").metrics == []    # 无 measure 列

        # 富化走了 chat_json 通道
        assert any(c["stage"] == STAGE_ENRICH for c in llm.json_calls)

        # 示例问题: LLM 出料原样解析
        assert content.sample_questions == [
            "各城市的订单总金额排名",
            "订单总金额最高的前10个用户",
            "各订单状态的数量占比",
        ]

        # 版本落库: 注册表按连接信息匹配 → v1 且 current
        assert _versions(env.db, info.id) == [(1, 1)]

    def test_progress_callback_sequence(self, env):
        """进度回调: 首阶段连接、含保存阶段、百分比单调不减、结束于 95。"""
        from domains.chatbi import semantic

        env.register()
        recorded = []
        semantic.scan_datasource(default_llm(), env.db, env.connect_info,
                                 progress_cb=lambda pct, stage: recorded.append((pct, stage)))
        assert recorded[0] == (10, "连接数据库...")
        stages = [s for _, s in recorded]
        for expected in ("扫描表结构...", "扫描外键关系...", "LLM 推断中文名...",
                         "LLM 推断业务指标...", "生成示例问题...", "保存语义层..."):
            assert expected in stages
        pcts = [p for p, _ in recorded]
        assert pcts == sorted(pcts) and pcts[-1] == 95
        assert "v1," in [s for p, s in recorded if p == 95][0]

    def test_progress_callback_failure_tolerated(self, env):
        """进度回调抛异常不阻塞扫描(移植 _update_scan 的容错语义)。"""
        from domains.chatbi import semantic

        env.register()

        def bad_cb(pct, stage):
            raise RuntimeError("进度写入失败")

        content = semantic.scan_datasource(default_llm(), env.db, env.connect_info,
                                           progress_cb=bad_cb)
        assert {"biz_users", "biz_orders"} <= {m.name for m in content.models}

    def test_scan_without_registry_match_returns_content_only(self, env):
        """注册表无匹配行(未注册数据源) → 仅返回内容,不落任何版本。"""
        from domains.chatbi import semantic

        content = semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        assert {"biz_users", "biz_orders"} <= {m.name for m in content.models}
        with env.db.connect() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM chatbi_semantic_models").fetchone()["n"]
        assert int(n) == 0

    def test_infer_metrics_false_skips_metrics(self, env):
        """infer_metrics=False(映射源配置 scan_metric_inference) → 指标为空。"""
        from domains.chatbi import semantic

        env.register()
        llm = default_llm()
        content = semantic.scan_datasource(llm, env.db, env.connect_info,
                                           infer_metrics=False)
        for m in content.models:
            assert m.metrics == []
        assert not any(c["stage"] == STAGE_METRICS for c in llm.chat_calls)
        # 富化与示例问题不受影响
        by_name = {c.name: c for c in _model(content, "biz_orders").columns}
        assert by_name["remark"].display_name == "备注"
        assert content.sample_questions


# ════════════════════════════════════════════════════════════════
# 版本管理: is_current 翻转 / 幂等 / load / diff / rollback / delete
# ════════════════════════════════════════════════════════════════

class TestVersions:
    def test_save_flip_idempotent_and_load(self, env):
        """save 翻转 is_current;内容不变幂等返回原版本;load 取当前/指定版本。"""
        from domains.chatbi import semantic

        info = env.register()
        v1 = semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        assert _versions(env.db, info.id) == [(1, 1)]

        # 重扫内容不变 → 幂等: 不产新版本,返回 v1
        v_again = semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        assert v_again == v1
        assert _versions(env.db, info.id) == [(1, 1)]

        # 内容变化 → v2 落库 + is_current 翻转
        v1.models[0].display_name = "改过的表名"
        assert semantic.save_content(env.db, info.id, v1, source="manual") == 2
        assert _versions(env.db, info.id) == [(1, 0), (2, 1)]

        # load: 当前版本 = v2 内容;指定版本 = v1 原内容
        current, cur_ver = semantic.load_content(env.db, info.id)
        assert cur_ver == 2 and current.models[0].display_name == "改过的表名"
        old, old_ver = semantic.load_content(env.db, info.id, version=1)
        assert old_ver == 1 and old.models[0].display_name != "改过的表名"
        current_only = semantic.load_current_content(env.db, info.id)
        assert current_only.models[0].display_name == "改过的表名"

    def test_load_missing_returns_none(self, env):
        from domains.chatbi import semantic

        assert semantic.load_current_content(env.db, "no-ds") is None
        assert semantic.load_content(env.db, "no-ds") == (None, None)
        assert semantic.load_content(env.db, "no-ds", version=3) == (None, None)

    def test_diff_versions(self, env):
        """diff: 表级/列级变更识别;版本缺失 → ValueError(api 回 404)。"""
        from domains.chatbi import semantic

        info = env.register()
        base = semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        # v2: biz_orders 增加一列(结构变更)
        from domains.chatbi.models import Column
        orders = _model(base, "biz_orders")
        orders.columns.append(Column(name="discount_amount", display_name="折扣金额",
                                     data_type="NUMERIC(10,2)", semantic_type="measure",
                                     source="manual", confidence=1.0))
        semantic.save_content(env.db, info.id, base, source="manual")

        diff = semantic.diff_versions(env.db, info.id, 1, 2)
        assert diff["from_version"] == 1 and diff["to_version"] == 2
        assert diff["has_changes"] is True
        changed = {c["table"]: c for c in diff["changed_models"]}
        assert "biz_orders" in changed
        assert changed["biz_orders"]["added_columns"] == ["discount_amount"]
        assert diff["added_models"] == [] and diff["removed_models"] == []

        # 无变化对比
        same = semantic.diff_versions(env.db, info.id, 2, 2)
        assert same["has_changes"] is False

        with pytest.raises(ValueError, match="版本 9 不存在"):
            semantic.diff_versions(env.db, info.id, 1, 9)

    def test_rollback_append_only(self, env):
        """回滚 = 历史版本内容落成新版本(append-only,不改写历史)。"""
        from domains.chatbi import semantic

        info = env.register()
        v1 = semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        original_name = _model(v1, "biz_orders").display_name
        v1.models[0].display_name = "改过的表名"
        semantic.save_content(env.db, info.id, v1, source="manual")   # v2
        assert _versions(env.db, info.id) == [(1, 0), (2, 1)]

        new_ver, _rolled = semantic.rollback(env.db, info.id, 1)
        assert new_ver == 3
        assert _versions(env.db, info.id) == [(1, 0), (2, 0), (3, 1)]
        content, ver = semantic.load_content(env.db, info.id)
        assert ver == 3
        assert _model(content, "biz_orders").display_name == original_name

        with pytest.raises(ValueError, match="版本 99 不存在"):
            semantic.rollback(env.db, info.id, 99)

    def test_delete_by_datasource(self, env):
        from domains.chatbi import semantic

        info = env.register()
        semantic.scan_datasource(default_llm(), env.db, env.connect_info)
        v1 = semantic.load_current_content(env.db, info.id)
        v1.models[0].display_name = "再改一次"
        semantic.save_content(env.db, info.id, v1)                    # v2
        assert len(_versions(env.db, info.id)) == 2

        assert semantic.delete_by_datasource(env.db, info.id) == 2
        assert _versions(env.db, info.id) == []
        assert semantic.load_current_content(env.db, info.id) is None
        # 幂等: 再删返回 0
        assert semantic.delete_by_datasource(env.db, info.id) == 0


# ════════════════════════════════════════════════════════════════
# 降级链: LLM 故障不阻塞(退化列名/仅规则指标/规则示例问题)
# ════════════════════════════════════════════════════════════════

class TestDegradation:
    def test_llm_failure_degrades_everywhere(self, env):
        """LLM 全故障: 扫描仍成功落库;列名退化 0.5;指标仅规则;问题走规则降级。"""
        from domains.chatbi import semantic

        info = env.register()
        content = semantic.scan_datasource(FakeLLM(raise_on_chat=True), env.db,
                                           env.connect_info)
        orders = _model(content, "biz_orders")
        by_name = {c.name: c for c in orders.columns}
        # 富化失败 → 退化列名(无注释列保持 auto_inferred/0.5)
        assert by_name["remark"].display_name == "remark"
        assert by_name["remark"].confidence == 0.5
        assert by_name["total_amount"].display_name == "订单总金额"  # 注释列不受影响
        # 指标: 仅规则 simple,无 LLM composite
        assert {m.name for m in orders.metrics} == {"total_amount_sum", "total_amount_avg"}
        # 示例问题: 规则降级(measure/dimension 组合,biz_orders 度量=订单总金额)
        assert content.sample_questions
        assert any("订单总金额" in q for q in content.sample_questions)
        # 版本照常落库
        assert _versions(env.db, info.id) == [(1, 1)]

    def test_metric_validation_failure_not_blocking(self, env):
        """LLM 指标坏条目(非 dict 字段/composite 缺 factor)逐条丢弃,好条目保留。"""
        from domains.chatbi import semantic

        info = env.register()
        content = semantic.scan_datasource(
            default_llm(metrics_text=METRICS_BAD_TEXT), env.db, env.connect_info)
        orders = _model(content, "biz_orders")
        names = {m.name for m in orders.metrics}
        # 规则 2 条 + 合法 single 1 条;坏 composite/garbage 被丢弃
        assert names == {"total_amount_sum", "total_amount_avg", "total_amount_max"}
        assert _versions(env.db, info.id) == [(1, 1)]

    def test_sample_questions_empty_llm_falls_back_to_rules(self, env):
        """LLM 返回空文本 → 规则降级(measure+dimension 组合)。"""
        from domains.chatbi import semantic

        env.register()
        llm = FakeLLM(chat_by_stage={STAGE_QUESTIONS: ""},
                      chat_json_by_stage={STAGE_ENRICH: {}})
        content = semantic.scan_datasource(llm, env.db, env.connect_info)
        assert content.sample_questions
        assert any("订单总金额" in q for q in content.sample_questions)

    def test_sample_questions_hardcoded_fallback(self, env):
        """极端兜底: 无业务表(规则也无法生成) → 3 条硬编码通用问题。"""
        from domains.chatbi import semantic

        assert semantic.generate_sample_questions([], FakeLLM(raise_on_chat=True)) == [
            "本月数据概览", "最近新增的记录", "各分类的数量统计"]

    def test_enrich_metrics_rule_disabled_llm_fallback_path(self, env):
        """rule_inference=False → 全量 LLM 推断回退路径(single+composite)。"""
        from domains.chatbi.models import Model, Column
        from domains.chatbi import semantic

        model = Model(name="biz_orders", display_name="订单", columns=[
            Column(name="total_amount", display_name="订单总金额",
                   data_type="NUMERIC(10,2)", semantic_type="measure"),
        ])
        content = semantic.SemanticModelContent(models=[model])
        llm = FakeLLM(chat_by_stage={STAGE_METRICS: json.dumps([
            {"name": "gmv", "display_name": "成交总额",
             "formula": "SUM(total_amount)", "type": "single"},
            {"name": "avg_gmv", "display_name": "均价",
             "formula": "gmv / cnt", "type": "composite",
             "factor_metric_names": ["gmv", "ghost"]},   # ghost 不存在 → 过滤后为空 → 丢弃
        ], ensure_ascii=False)})
        semantic.enrich_metrics(content, llm, rule_inference=False)
        # F4 交叉校验(对齐源 _infer_all): ghost 不存在被过滤,剩余有效因子 gmv →
        # composite 保留;single 原样保留
        names = {m.name for m in model.metrics}
        assert names == {"gmv", "avg_gmv"}
        assert next(m for m in model.metrics if m.name == "avg_gmv") \
            .factor_metric_names == ["gmv"]


# ════════════════════════════════════════════════════════════════
# 注入防御 + 纯函数 diff
# ════════════════════════════════════════════════════════════════

class TestDefense:
    def test_validate_metric_formula_clean(self):
        from domains.chatbi.semantic import validate_metric_formula

        assert validate_metric_formula("SUM(total_amount)") == []
        assert validate_metric_formula("gmv / order_count") == []
        assert validate_metric_formula("SUM(amount) WHERE status IN ('paid')") == []

    def test_validate_metric_formula_dangerous(self):
        from domains.chatbi.semantic import validate_metric_formula

        assert validate_metric_formula("SUM(a); DROP TABLE users")
        assert validate_metric_formula("SUM(a) -- comment")
        assert validate_metric_formula("SUM(a) /* 注释 */")
        assert validate_metric_formula("TRUNCATE t")
        assert validate_metric_formula("ALTER TABLE t")
        assert validate_metric_formula("EXEC xp_cmd")
        assert validate_metric_formula("EXECUTE x")
        assert validate_metric_formula("INSERT INTO t VALUES (1)")
        assert validate_metric_formula("DELETE FROM t")
        assert validate_metric_formula("UPDATE t SET a = 1")
        assert validate_metric_formula("DROP DATABASE x")
        assert validate_metric_formula("DROP SCHEMA x")
        # 大小写不敏感
        assert validate_metric_formula("sum(a); delete from t")

    def test_diff_semantic_contents_pure_edges(self):
        from domains.chatbi.semantic import diff_semantic_contents, is_empty_diff

        # 旧版本为 None(初始状态) → 全部视为新增
        diff = diff_semantic_contents(None, {"models": [{"name": "t1", "columns": []}]})
        assert diff["added_models"] == ["t1"] and diff["has_changes"] is True
        assert is_empty_diff(diff) is False
        # 双 None → 无变化
        empty = diff_semantic_contents(None, None)
        assert empty["has_changes"] is False and is_empty_diff(empty)
        # 列级: data_type / semantic_type 变更被识别,display_name 不算结构变更
        old = {"models": [{"name": "t", "display_name": "x", "columns": [
            {"name": "a", "data_type": "VARCHAR", "semantic_type": "dimension"}]}]}
        new_type = {"models": [{"name": "t", "display_name": "x", "columns": [
            {"name": "a", "data_type": "INTEGER", "semantic_type": "dimension"}]}]}
        assert diff_semantic_contents(old, new_type)["changed_models"][0]["changed_columns"] == ["a"]
        new_display = {"models": [{"name": "t", "display_name": "y", "columns": [
            {"name": "a", "data_type": "VARCHAR", "semantic_type": "dimension"}]}]}
        col_diff = diff_semantic_contents(old, new_display)
        assert col_diff["changed_models"][0]["changed_columns"] == []   # 列级无结构变化
        assert col_diff["changed_models"]                                # 但顶层字段变更
        # 删除表
        assert diff_semantic_contents(old, {"models": []})["removed_models"] == ["t"]
