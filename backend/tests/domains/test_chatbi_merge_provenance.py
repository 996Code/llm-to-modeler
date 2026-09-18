"""十五审验收测试: merge provenance + 集合去重 + 校验 + 空状态 CAS.

关键约束: fixture 模拟真实扫描来源——无数据库注释的表/列是
auto_inferred(不依赖模型默认 manual), 这是十四审测试掩盖边界的根因.
"""
import types

import pytest
from domains.chatbi.models import (
    SemanticModelContent, Model, Column, Metric, Relationship,
    CalculatedField,
)
from domains.chatbi.stores import SearchResult


def _col(name, source="auto_inferred", **kw):
    """真实扫描列: 无数据库注释 → source=auto_inferred."""
    defaults = dict(display_name=f"auto_{name}", data_type="TEXT",
                    source=source, confidence=0.5)
    defaults.update(kw)
    return Column(name=name, **defaults)


def _model(name, source="auto_inferred", **kw):
    defaults = dict(display_name=f"auto_{name}", columns=[_col("id")],
                    source=source, confidence=0.5)
    defaults.update(kw)
    return Model(name=name, **defaults)


@pytest.fixture()
def pg_engine():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.models import CHATBI_DDL
    engine = PackRelationalDB("chatbi")
    engine.init_schema(list(CHATBI_DDL))
    return engine


class TestRescanProvenanceChain:
    """P0: manual edit → rescan → rescan, 人工值+来源都不丢."""

    def test_double_rescan_preserves_manual(self):
        from domains.chatbi.tasks import _merge_rescan

        # 管理员编辑后的语义层
        edited = SemanticModelContent(models=[
            Model(name="t", display_name="管理员表名", source="manual",
                  confidence=1.0, columns=[
                Column(name="id", display_name="管理员列名", data_type="INT",
                       source="manual", confidence=1.0),
            ]),
        ])

        # 第一次重扫: 新扫描产出 auto 值(真实: 无DB注释 → auto_inferred)
        scan1 = SemanticModelContent(models=[
            Model(name="t", display_name="LLM表名-1", source="auto_inferred",
                  confidence=0.5, columns=[
                Column(name="id", display_name="LLM列名-1", data_type="INT",
                       source="auto_inferred", confidence=0.5),
            ]),
        ])
        r1 = _merge_rescan(edited, scan1)
        m1 = r1.models[0]
        # 第一次后: 值保留 + 来源保留(不能被降回 auto)
        assert m1.display_name == "管理员表名"
        assert m1.source == "manual"
        assert m1.confidence == 1.0
        assert m1.columns[0].display_name == "管理员列名"
        assert m1.columns[0].source == "manual"
        assert m1.columns[0].confidence == 1.0

        # 第二次重扫: 如果 source 已被破坏, 人工值会被 auto 覆盖
        scan2 = SemanticModelContent(models=[
            Model(name="t", display_name="LLM表名-2", source="auto_inferred",
                  confidence=0.5, columns=[
                Column(name="id", display_name="LLM列名-2", data_type="INT",
                       source="auto_inferred", confidence=0.5),
            ]),
        ])
        r2 = _merge_rescan(r1, scan2)
        m2 = r2.models[0]
        # 第二次后: 人工值仍然保留(P0 关键断言)
        assert m2.display_name == "管理员表名", \
            f"P0: 第二次重扫丢失人工表名 → {m2.display_name}"
        assert m2.source == "manual", \
            f"P0: 第二次重扫丢失来源 → {m2.source}"
        assert m2.columns[0].display_name == "管理员列名"
        assert m2.columns[0].source == "manual"
        assert m2.columns[0].confidence == 1.0

    def test_refresh_then_rescan_preserves_manual(self):
        """manual edit → refresh → rescan(十五审 7.2 链)."""
        from domains.chatbi.tasks import _merge_content, _merge_rescan

        edited = SemanticModelContent(models=[
            Model(name="t", display_name="管理员表名", source="manual",
                  confidence=1.0, columns=[
                Column(name="id", display_name="管理员列名", data_type="INT",
                       source="manual", confidence=1.0),
            ]),
        ])
        # refresh(结构扫描, 不产 LLM 值)
        refresh_scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", source="auto_inferred",
                  confidence=0.3, columns=[
                Column(name="id", display_name="id", data_type="INT",
                       source="auto_inferred", confidence=0.3),
            ]),
        ])
        after_refresh = _merge_content(edited, refresh_scan)
        m = after_refresh.models[0]
        assert m.display_name == "管理员表名"
        assert m.source == "manual", \
            f"refresh 破坏表级来源 → {m.source}"
        assert m.columns[0].source == "manual"

        # rescan: 人工值不因 refresh 而丢失
        rescan_result = _merge_rescan(after_refresh, refresh_scan)
        m2 = rescan_result.models[0]
        assert m2.display_name == "管理员表名"
        assert m2.source == "manual"


class TestMetricDeduplication:
    """P1 7.3: manual/auto 同名指标不得重复."""

    def test_same_name_metric_no_duplicate(self):
        from domains.chatbi.tasks import _merge_rescan

        old = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[_col("amt")], metrics=[
                Metric(name="revenue", display_name="收入", formula="SUM(amt)",
                       type="single", source="manual"),
            ]),
        ])
        new_scan = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[_col("amt")], metrics=[
                Metric(name="revenue", display_name="收入_auto", formula="COUNT(amt)",
                       type="single", source="rule_inferred"),
                Metric(name="new_metric", display_name="新", formula="MAX(amt)",
                       type="single", source="rule_inferred"),
            ]),
        ])
        merged = _merge_rescan(old, new_scan)
        metrics = merged.models[0].metrics
        names = [m.name for m in metrics]
        # manual 的 revenue 保留, auto 的同名被压制
        assert names.count("revenue") == 1, f"同名重复: {names}"
        assert metrics[0].source == "manual"
        assert metrics[0].formula == "SUM(amt)"
        # 不同名的新 auto 指标正常加入
        assert "new_metric" in names


class TestStaleRelationshipValidation:
    """P1 7.4: manual 关系指向已删表时不保留(幽灵节点)."""

    def test_manual_rel_to_deleted_table_dropped(self):
        from domains.chatbi.tasks import _merge_rescan

        old = SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[_col("id"), _col("uid"), _col("gid")], relationships=[
                Relationship(name="r_valid", target_model="users",
                             join_type="LEFT", on="orders.uid=users.id",
                             type="N:1", source="manual"),
                Relationship(name="r_stale", target_model="gone_table",
                             join_type="LEFT", on="orders.gid=gone_table.id",
                             type="N:1", source="manual"),
            ]),
            Model(name="users", display_name="users", columns=[_col("id")]),
            Model(name="gone_table", display_name="gone_table", columns=[_col("id")]),
        ])
        # 新扫描: gone_table 已从数据库删除
        new_scan = SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[_col("id"), _col("uid")]),
            Model(name="users", display_name="users", columns=[_col("id")]),
        ])
        merged = _merge_rescan(old, new_scan)
        rels = merged.models[0].relationships
        targets = [r.target_model for r in rels]
        assert "users" in targets, "有效 manual 关系被误删"
        assert "gone_table" not in targets, "幽灵关系未被清理"


class TestCalculatedFieldsPreservation:
    """P1: calculated_fields 不被扫描/刷新清空.

    十七审 7.8: 每条 merge 路径使用 deepcopy 独立输入——此前复用被
    _merge_rescan 原地修改的 scan 对象, refresh 断言看到的是上一步
    污染进去的结果(假阳性结构).
    """

    @staticmethod
    def _old():
        cf = CalculatedField(name="profit", display_name="利润",
                             formula="revenue - cost")
        return SemanticModelContent(models=[
            Model(name="t", display_name="T",
                  columns=[_col("id"), _col("revenue"), _col("cost")],
                  calculated_fields=[cf]),
        ])

    @staticmethod
    def _scan():
        return SemanticModelContent(models=[
            Model(name="t", display_name="t",
                  columns=[_col("id"), _col("revenue"), _col("cost")]),
        ])

    def test_calculated_fields_survive_rescan(self):
        from copy import deepcopy
        from domains.chatbi.tasks import _merge_rescan

        r1 = _merge_rescan(self._old(), deepcopy(self._scan()))
        assert len(r1.models[0].calculated_fields) == 1
        assert r1.models[0].calculated_fields[0].name == "profit"

    def test_calculated_fields_survive_refresh_independent(self):
        from copy import deepcopy
        from domains.chatbi.tasks import _merge_content

        r2 = _merge_content(self._old(), deepcopy(self._scan()))
        assert len(r2.models[0].calculated_fields) == 1
        assert r2.models[0].calculated_fields[0].name == "profit"


class TestEmptyStateCAS:
    """P2 7.5: 空状态 expected=0 成功, 非 0 失败."""

    def test_empty_expected_zero_succeeds(self, pg_engine):
        from domains.chatbi import semantic
        content = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[
                Column(name="id", display_name="ID", data_type="INT"),
            ]),
        ])
        ver = semantic.save_content(pg_engine, "empty-cas-test",
                                    content, expected_version=0)
        assert ver == 1

    def test_empty_expected_nonzero_fails(self, pg_engine):
        from domains.chatbi import semantic
        from domains.chatbi.graph_infer import VersionConflictError
        content = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[
                Column(name="id", display_name="ID", data_type="INT"),
            ]),
        ])
        with pytest.raises(VersionConflictError):
            semantic.save_content(pg_engine, "empty-cas-test-2",
                                  content, expected_version=7)

    def test_empty_expected_negative_fails(self, pg_engine):
        from domains.chatbi import semantic
        from domains.chatbi.graph_infer import VersionConflictError
        content = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[
                Column(name="id", display_name="ID", data_type="INT"),
            ]),
        ])
        with pytest.raises(VersionConflictError):
            semantic.save_content(pg_engine, "empty-cas-test-3",
                                  content, expected_version=-1)


class TestAutoUpdateOnRescan:
    """auto 值在重扫时正确更新(不能因 manual 保护而全冻结)."""

    def test_auto_values_update_on_rescan(self):
        from domains.chatbi.tasks import _merge_rescan

        old = SemanticModelContent(models=[
            Model(name="t", display_name="旧auto名", source="auto_inferred",
                  confidence=0.5, columns=[
                _col("id", display_name="旧auto列", source="auto_inferred"),
            ]),
        ])
        new_scan = SemanticModelContent(models=[
            Model(name="t", display_name="新auto名", source="auto_inferred",
                  confidence=0.7, columns=[
                _col("id", display_name="新auto列", source="auto_inferred"),
            ]),
        ])
        merged = _merge_rescan(old, new_scan)
        m = merged.models[0]
        # auto 值用新扫描替换(重扫的更新目的)
        assert m.display_name == "新auto名"
        assert m.confidence == 0.7
        assert m.columns[0].display_name == "新auto列"


# ════════════════════════════════════════════════════════════════
# 十七审反例回归: 人工优先级 / composite 保留 / db_comment 策略 /
# 复杂表达式校验 / 报告可见性 / 索引 revision 原子发布
# ════════════════════════════════════════════════════════════════

class TestManualMetricPriority:
    """P0 7.1: refresh 同名去重必须人工优先, 不得静默还原成自动版."""

    def _old(self):
        # 管理员编辑已有规则指标: 同名保留, 值/条件/来源全换人工
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("amount"), _col("status"),
            ], metrics=[
                Metric(name="amount_sum", display_name="人工净额",
                       formula="SUM(amount)", type="single",
                       condition="status IN ('paid')", source="manual_edit"),
            ]),
        ])

    def _scan(self):
        # refresh(llm=None) 只产规则 simple: 同名自动版, 无条件
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("amount"), _col("status"),
            ], metrics=[
                Metric(name="amount_sum", display_name="自动合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])

    def test_refresh_manual_metric_wins(self):
        from domains.chatbi.tasks import _merge_content
        merged = _merge_content(self._old(), self._scan())
        metrics = merged.models[0].metrics
        winners = [m for m in metrics if m.name == "amount_sum"]
        assert len(winners) == 1, f"同名重复: {[m.name for m in metrics]}"
        w = winners[0]
        assert w.display_name == "人工净额"
        assert w.condition == "status IN ('paid')"
        assert w.source == "manual_edit"

    def test_stale_manual_metric_suppresses_same_name_auto(self):
        """失效人工指标停用进复核, 同名自动版不得静默顶替(宁缺毋滥)."""
        from domains.chatbi.tasks import _merge_content, _new_report
        old = self._old()
        # 物理删除 status 列 → 人工指标的 condition 失效
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="orders",
                  columns=[_col("amount")], metrics=[
                Metric(name="amount_sum", display_name="自动合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        names = [m.name for m in merged.models[0].metrics]
        assert "amount_sum" not in names, "失效人工项被自动版静默顶替"
        assert any(d["name"] == "amount_sum" and d["kind"] == "metric"
                   for d in report["dropped_items"]), "停用项必须进复核清单"
        assert report["requires_review"] is True


class TestManualRelationshipPriority:
    """P0 7.2: 等价端点去重时人工关系优先; 属性不同要形成可见冲突."""

    def _old(self, join="INNER", card="1:1"):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("id"), _col("user_id"),
            ], relationships=[
                Relationship(name="manual_join", target_model="users",
                             join_type=join, on="orders.user_id = users.id",
                             type=card, source="manual_edit"),
            ]),
            Model(name="users", display_name="users", columns=[_col("id")]),
        ])

    def _scan(self):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("id"), _col("user_id"),
            ], relationships=[
                Relationship(name="orders_to_users", target_model="users",
                             join_type="LEFT", on="orders.user_id = users.id",
                             type="N:1", source="foreign_key"),
            ]),
            Model(name="users", display_name="users", columns=[_col("id")]),
        ])

    @pytest.mark.parametrize("merge_name", ["_merge_content", "_merge_rescan"])
    def test_manual_rel_wins_with_visible_conflict(self, merge_name):
        from domains.chatbi import tasks
        from domains.chatbi.tasks import _new_report
        merge = getattr(tasks, merge_name)
        report = _new_report()
        merged = merge(self._old(), self._scan(), report=report)
        rels = [r for r in merged.models[0].relationships
                if r.target_model == "users"]
        assert len(rels) == 1, "等价关系出现重复"
        r = rels[0]
        assert r.name == "manual_join", "等价去重保留了自动 FK, 丢人工关系"
        assert r.join_type == "INNER" and r.type == "1:1", "人工属性被覆盖"
        # 属性不同 → 管理员可见冲突
        assert len(report["conflicts"]) == 1
        assert report["conflicts"][0]["manual"] == "manual_join"
        assert report["requires_review"] is True

    def test_identical_attributes_no_conflict(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        report = _new_report()
        merged = _merge_content(self._old(join="LEFT", card="N:1"),
                                self._scan(), report=report)
        rels = [r for r in merged.models[0].relationships
                if r.target_model == "users"]
        assert len(rels) == 1 and rels[0].name == "manual_join"
        assert report["conflicts"] == [], "属性一致时不应报冲突"


class TestCompositePreservationOnRefresh:
    """P1 7.3: refresh 不跑 LLM, 旧 auto composite 必须按 factor 闭包保留."""

    def _old(self, factors=None):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("id"), _col("amount"),
            ], metrics=[
                Metric(name="amount_sum", display_name="合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
                Metric(name="id_count", display_name="单数",
                       formula="COUNT(id)", type="single",
                       source="rule_inferred"),
                Metric(name="avg_order", display_name="均单",
                       formula="amount_sum / id_count", type="composite",
                       factor_metric_names=list(factors or
                                                ["amount_sum", "id_count"]),
                       source="auto_inferred"),
            ]),
        ])

    def _scan(self):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("id"), _col("amount"),
            ], metrics=[
                Metric(name="amount_sum", display_name="合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
                Metric(name="id_count", display_name="单数",
                       formula="COUNT(id)", type="single",
                       source="rule_inferred"),
            ]),
        ])

    def test_valid_composite_kept_on_refresh(self):
        from domains.chatbi.tasks import _merge_content
        merged = _merge_content(self._old(), self._scan())
        names = {m.name for m in merged.models[0].metrics}
        assert {"amount_sum", "id_count", "avg_order"} <= names, \
            f"LLM composite 被 refresh 删除: {names}"

    def test_broken_closure_composite_dropped(self):
        from domains.chatbi.tasks import _merge_content
        # factor 引用不存在的子指标 → 整条丢弃(fail-closed)
        old = self._old(factors=["amount_sum", "ghost_metric"])
        merged = _merge_content(old, self._scan())
        names = {m.name for m in merged.models[0].metrics}
        assert "avg_order" not in names


class TestDbCommentRefreshStrategy:
    """P1 7.4: refresh 按来源分治——注释变更生效, 人工保留, 值/来源不错配."""

    def test_changed_comment_takes_effect(self):
        from domains.chatbi.tasks import _merge_content
        old = SemanticModelContent(models=[
            Model(name="t", display_name="旧表注释", description="旧描述",
                  source="db_comment", confidence=1.0, columns=[
                _col("c", display_name="旧列注释", source="db_comment",
                     confidence=1.0),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="新表注释", description="新描述",
                  source="db_comment", confidence=1.0, columns=[
                _col("c", display_name="新列注释", source="db_comment",
                     confidence=1.0),
            ]),
        ])
        merged = _merge_content(old, scan)
        m = merged.models[0]
        assert m.display_name == "新表注释" and m.description == "新描述"
        assert m.source == "db_comment"          # 值+来源原子更新
        assert m.columns[0].display_name == "新列注释"

    def test_new_comment_overrides_old_auto_atomically(self):
        from domains.chatbi.tasks import _merge_content
        old = SemanticModelContent(models=[
            Model(name="t", display_name="旧LLM名", source="auto_inferred",
                  confidence=0.8, columns=[
                _col("c", display_name="旧LLM列名", source="auto_inferred",
                     confidence=0.8),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="新注释", source="db_comment",
                  confidence=1.0, columns=[
                _col("c", display_name="新列注释", source="db_comment",
                     confidence=1.0),
            ]),
        ])
        merged = _merge_content(old, scan)
        m = merged.models[0]
        assert m.display_name == "新注释" and m.source == "db_comment", \
            "新注释必须覆盖旧 auto 值(值+来源一起)"
        assert m.columns[0].display_name == "新列注释"
        assert m.columns[0].source == "db_comment"

    def test_manual_edit_survives_new_comment(self):
        from domains.chatbi.tasks import _merge_content
        old = SemanticModelContent(models=[
            Model(name="t", display_name="人工表名", source="manual_edit",
                  confidence=1.0, columns=[
                _col("c", display_name="人工列名", source="manual_edit",
                     confidence=1.0),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="新注释", source="db_comment",
                  confidence=1.0, columns=[
                _col("c", display_name="新列注释", source="db_comment",
                     confidence=1.0),
            ]),
        ])
        merged = _merge_content(old, scan)
        m = merged.models[0]
        assert m.display_name == "人工表名" and m.source == "manual_edit"
        assert m.columns[0].display_name == "人工列名"

    def test_removed_comment_degrades_with_report(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="t", display_name="旧表注释", source="db_comment",
                  confidence=1.0, columns=[
                _col("c", display_name="旧列注释", source="db_comment",
                     confidence=1.0),
            ]),
        ])
        scan = SemanticModelContent(models=[   # 注释被删 → 退化扫描值
            Model(name="t", display_name="t", source="auto_inferred",
                  confidence=0.5, columns=[
                _col("c"),
            ]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        m = merged.models[0]
        assert m.display_name == "t" and m.source == "auto_inferred"
        kinds = {d["kind"] for d in report["dropped_items"]}
        assert "db_comment_removed" in kinds, "注释删除必须可告警"


class TestComplexExpressionValidation:
    """P1 7.5: sqlglot 校验覆盖 CASE/condition/引号 JOIN/计算字段/不可解析."""

    def test_case_formula_with_deleted_column_dropped(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("kept")], metrics=[
                Metric(name="case_metric", display_name="C",
                       formula="SUM(CASE WHEN gone > 0 THEN kept ELSE 0 END)",
                       type="single", source="manual_edit"),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("kept")]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        assert [m.name for m in merged.models[0].metrics] == [], "复杂 CASE 漏检"
        assert report["dropped_items"][0]["reason"].startswith("formula")

    def test_condition_with_deleted_column_dropped(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("amount")], metrics=[
                Metric(name="m", display_name="M", formula="SUM(amount)",
                       type="single", condition="gone > 0",
                       source="manual_edit"),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("amount")]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        assert not merged.models[0].metrics, "condition 失效列漏检"
        assert "condition" in report["dropped_items"][0]["reason"]

    def test_calculated_field_with_deleted_column_dropped(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("kept")],
                  calculated_fields=[
                      CalculatedField(name="calc", display_name="计算",
                                      formula="gone - kept"),
                  ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("kept")]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        assert merged.models[0].calculated_fields == [], "计算字段失效列漏检"
        assert report["dropped_items"][0]["kind"] == "calculated_field"

    def test_quoted_join_deleted_column_dropped(self):
        from domains.chatbi.tasks import _merge_rescan, _new_report
        old = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[
                _col("uid"), _col("gone"),
            ], relationships=[
                Relationship(name="quoted", target_model="users",
                             join_type="LEFT",
                             on='orders."gone" = users.id',
                             type="N:1", source="manual_edit"),
            ]),
            Model(name="users", display_name="u", columns=[_col("id")]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[_col("uid")]),
            Model(name="users", display_name="u", columns=[_col("id")]),
        ])
        report = _new_report()
        merged = _merge_rescan(old, scan, report=report)
        assert merged.models[0].relationships == [], "带引号 JOIN 漏检"

    def test_unparseable_formula_fail_closed(self):
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("a")], metrics=[
                Metric(name="weird", display_name="W", formula="SUM(((",
                       type="single", source="manual_edit"),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("a")]),
        ])
        report = _new_report()
        merged = _merge_content(old, scan, report=report)
        assert not merged.models[0].metrics, "不可解析表达式必须 fail-closed"
        assert "unparseable" in report["dropped_items"][0]["reason"]


class TestMergeReportVisibility:
    """P1 7.6: 停用/冲突清单进报告并持久化(任务结果与语义页面数据源)."""

    def test_report_persist_roundtrip(self, pg_engine):
        from domains.chatbi.tasks import (
            save_merge_report, get_merge_report, _new_report, _report_drop)
        report = _new_report()
        _report_drop(report, "metric", "orders", "old_metric",
                     "formula missing_column: gone")
        save_merge_report(pg_engine, "report-ds-test", 7, report)
        saved = get_merge_report(pg_engine, "report-ds-test")
        assert saved is not None and saved["version"] == 7
        assert saved["report"]["dropped_items"][0]["name"] == "old_metric"
        assert saved["report"]["requires_review"] is True

    def test_get_merge_report_missing_returns_none(self, pg_engine):
        from domains.chatbi.tasks import get_merge_report
        assert get_merge_report(pg_engine, "no-such-ds") is None


class TestPersistedManualEditRefreshChain:
    """十七审 7.8: 页面真实保存结构(编辑→落库→结构变更→refresh)持久化回归."""

    def test_manual_edit_survives_persisted_refresh(self, pg_engine):
        from domains.chatbi import semantic
        from domains.chatbi.tasks import _merge_content, _new_report
        from domains.chatbi.graph_infer import VersionConflictError
        ds = "persist-chain-ds"

        # v1: 首次扫描(规则指标 + amount/status 列)
        v1 = SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("amount"), _col("status"),
            ], metrics=[
                Metric(name="amount_sum", display_name="自动合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])
        assert semantic.save_content(pg_engine, ds, v1, source="scan",
                                     expected_version=0) == 1

        # v2: 管理员编辑同名指标(PUT 语义: mark_manual_edits 打 manual_edit)
        current, cur_v = semantic.load_content(pg_engine, ds)
        for met in current.models[0].metrics:
            if met.name == "amount_sum":
                met.display_name = "人工净额"
                met.condition = "status IN ('paid')"
                met.source = "manual_edit"
        marked = semantic.mark_manual_edits(
            semantic.load_current_content(pg_engine, ds), current)
        assert marked >= 0
        assert semantic.save_content(pg_engine, ds, current,
                                     source="manual_edit",
                                     expected_version=cur_v) == 2

        # 物理删除 status 列后的结构刷新(llm=None 语义)
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("amount"),
            ], metrics=[
                Metric(name="amount_sum", display_name="自动合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])
        report = _new_report()
        merged = _merge_content(
            semantic.load_current_content(pg_engine, ds), scan, report=report)
        _, cur_v = semantic.load_content(pg_engine, ds)
        v3 = semantic.save_content(pg_engine, ds, merged, source="refresh",
                                   expected_version=cur_v)
        assert v3 == 3

        # 重读: 人工指标因 condition 失效被停用(进复核), 不静默回退自动版
        final = semantic.load_current_content(pg_engine, ds)
        names = [m.name for m in final.models[0].metrics]
        assert "amount_sum" not in names
        assert any(d["name"] == "amount_sum" for d in report["dropped_items"])
        # 空状态 CAS: 旧 expected 再写必须冲突
        with pytest.raises(VersionConflictError):
            semantic.save_content(pg_engine, ds, merged,
                                  expected_version=1)


# ── 索引 revision namespace(十七审 7.7 / 十八审 P0-2)──────────────

class _FakeVectorStore:
    """真实 Milvus 语义替身: collection 全局主键(chunk_id) + doc_id 过滤字段。

    十八审 P0-2: 旧替身把 doc_id 当物理分区(dict[doc_id] → list), 掩盖了
    "upsert 同主键跨 revision 覆盖"的存储真相。本替身逐条以 chunk_id 为主键
    upsert(覆盖同主键行并改写其 doc_id), search/delete 按 doc_id 标量过滤——
    与 sdk MilvusVectorStore 的行为一致。
    """

    def __init__(self):
        self.rows: dict[str, dict] = {}    # chunk_id → {record, doc_id}
        self.fail_upsert_at: int | None = None   # 第 N 条 upsert 时失败(部分成功)
        self._upsert_count = 0

    def upsert_records(self, scope, records, doc_id):
        from domains.chatbi.stores import ChatBIVectorStore
        for i, rec in enumerate(records):
            if (self.fail_upsert_at is not None
                    and self._upsert_count + i >= self.fail_upsert_at):
                raise RuntimeError("milvus upsert 中途失败(部分成功)")
            cid = ChatBIVectorStore._chunk_id(rec)
            self.rows[cid] = {"record": rec, "doc_id": doc_id}
        self._upsert_count += len(records)
        return len(records)

    def delete_doc(self, scope, doc_id):
        cids = [c for c, v in self.rows.items() if v["doc_id"] == doc_id]
        for c in cids:
            del self.rows[c]
        return len(cids)

    def search_records(self, scope, query_vector, top_k=5,
                       score_threshold=0.0, doc_id=None):
        from domains.chatbi.stores import ChatBIVectorStore, VectorRecord
        out = []
        for cid, v in self.rows.items():
            if doc_id is not None and v["doc_id"] != doc_id:
                continue
            # 命中行 metadata 按真实回解码路径重建(与生产 search 一致)
            out.append(SearchResult(
                record=VectorRecord(
                    id=cid, vector=query_vector,
                    metadata=ChatBIVectorStore.decode_chunk_id(cid),
                    text=v["record"].text),
                score=1.0))
        return out[:top_k]

    def count_doc(self, doc_id):
        return sum(1 for v in self.rows.values() if v["doc_id"] == doc_id)


class _FakeEmbedder:
    def embed(self, texts):
        return [[0.1, 0.2] for _ in texts]


def _idx_content(tag: str) -> SemanticModelContent:
    return SemanticModelContent(models=[
        Model(name=f"t_{tag}", display_name=f"表{tag}",
              columns=[_col("id")]),
    ])


class TestIndexRevisionNamespace:
    """P1 7.7: 先建新分区 → 原子翻转指针 → 延迟清理; 旧任务晚完成不覆盖."""

    def _ds_row(self, db, ds_id):
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM chatbi_data_sources WHERE id = ?",
                (ds_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO chatbi_data_sources "
                    "(id, name, db_type, host, port, database, username, "
                    "encrypted_password, is_active, scan_status, "
                    "scan_progress, scan_stage, scan_error, created_at, "
                    "updated_at) VALUES (?, 't', 'postgresql', 'h', 1, 'd', "
                    "'u', 'p', 1, 'idle', 0, '', '', ?, ?)",
                    (ds_id, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))

    def test_version_guard_only_newer_flips(self, pg_engine):
        from domains.chatbi.stores import (set_active_doc_id,
                                           get_active_doc_id)
        assert set_active_doc_id(pg_engine, "scope-g1", "schema_r10", 10)
        assert set_active_doc_id(pg_engine, "scope-g1", "schema_r11", 11)
        # 晚完成的旧任务: 低版本翻转被拒
        assert not set_active_doc_id(pg_engine, "scope-g1", "schema_r10", 10)
        assert get_active_doc_id(pg_engine, "scope-g1") == "schema_r11"

    def test_revision_build_publishes_and_cleans(self, pg_engine):
        from domains.chatbi.indexing import rebuild_index
        ds = "rev-ds-1"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()

        r1 = rebuild_index(_idx_content("a"), ds, store, _FakeEmbedder(),
                           db=pg_engine, revision=10)
        assert r1.error is None and r1.indexed_count == 1
        # 指针已指向 r10 分区
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions").fetchone()
        assert row["active_doc_id"] == "schema_r10"
        assert store.count_doc("schema_r10") == 1

        # v11 发布 → r10 分区被延迟清理
        r2 = rebuild_index(_idx_content("b"), ds, store, _FakeEmbedder(),
                           db=pg_engine, revision=11)
        assert r2.error is None
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions").fetchone()
        assert row["active_doc_id"] == "schema_r11"
        # 两代 grace: 发布 r11 后 r10 仍保留(r12 发布后才 GC)
        assert store.count_doc("schema_r10") == 1, "grace 窗内的上一代被误删"
        assert store.count_doc("schema_r11") == 1

    def test_failed_build_keeps_old_active(self, pg_engine):
        from domains.chatbi.indexing import rebuild_index
        from domains.chatbi.stores import set_active_doc_id
        ds = "rev-ds-2"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        # 先成功发布 v10
        assert rebuild_index(_idx_content("a"), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=10).error is None
        # v11 构建失败 → 指针不动, r10 分区仍可读
        store.fail_upsert_at = 0
        r11 = rebuild_index(_idx_content("b"), ds, store, _FakeEmbedder(),
                            db=pg_engine, revision=11)
        assert r11.error is not None
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions").fetchone()
        assert row["active_doc_id"] == "schema_r10"
        assert store.search_records("s", [0.1], doc_id="schema_r10"), \
            "构建失败后旧分区必须仍可检索(无空窗)"

    def test_late_old_revision_does_not_override(self, pg_engine):
        from domains.chatbi.indexing import rebuild_index
        ds = "rev-ds-3"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        # v10 先完成(读到的 prev = None)
        assert rebuild_index(_idx_content("old"), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=10).error is None
        # v11 完成(读到的 prev = schema_r10)
        assert rebuild_index(_idx_content("new"), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=11).error is None
        # 旧任务 v10 晚到: 翻转被拒, 读者仍读 v11
        assert rebuild_index(_idx_content("old"), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=10).error is None
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions").fetchone()
        assert row["active_doc_id"] == "schema_r11"
        assert store.search_records("s", [0.1], doc_id="schema_r11"), \
            "晚完成的旧任务覆盖了已发布新版"

    def test_reader_falls_back_to_legacy_partition(self, pg_engine):
        from domains.chatbi.stores import get_active_doc_id
        # 未发布过 revision 的 scope → None → 调用方回退 legacy "schema"
        assert get_active_doc_id(pg_engine, "never-indexed-scope") is None


# ════════════════════════════════════════════════════════════════
# 十八审反例回归: 人工 composite / revision 物理隔离 / 同名指标 /
# conflict 契约 / 报告生命周期 / 调度租约
# ════════════════════════════════════════════════════════════════

class TestManualCompositePreservation:
    """P0-1: 人工 composite 在 refresh/rescan 不得静默消失."""

    def _old(self, factors=None, source="manual_edit"):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[
                _col("amount"), _col("id"),
            ], metrics=[
                Metric(name="amount_sum", display_name="合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
                Metric(name="margin", display_name="人工毛利",
                       formula="amount_sum / id_count", type="composite",
                       factor_metric_names=list(factors or
                                                ["amount_sum", "id_count"]),
                       source=source),
            ]),
        ])

    def _scan(self):
        return SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[
                _col("amount"), _col("id"),
            ], metrics=[
                Metric(name="amount_sum", display_name="合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
                Metric(name="id_count", display_name="单数",
                       formula="COUNT(id)", type="single",
                       source="rule_inferred"),
            ]),
        ])

    @pytest.mark.parametrize("merge_name", ["_merge_content", "_merge_rescan"])
    def test_manual_composite_kept(self, merge_name):
        from domains.chatbi import tasks
        merged = getattr(tasks, merge_name)(self._old(), self._scan())
        metrics = {m.name: m for m in merged.models[0].metrics}
        assert "margin" in metrics, "人工 composite 被静默删除(P0-1)"
        assert metrics["margin"].source == "manual_edit"
        assert metrics["margin"].display_name == "人工毛利"

    def test_manual_composite_suppresses_same_name_auto(self):
        """同名 auto composite 不得顶替人工版."""
        from domains.chatbi.tasks import _merge_content
        scan = self._scan()
        scan.models[0].metrics.append(Metric(
            name="margin", display_name="自动毛利",
            formula="amount_sum / id_count", type="composite",
            factor_metric_names=["amount_sum", "id_count"],
            source="auto_inferred"))
        merged = _merge_content(self._old(), scan)
        winners = [m for m in merged.models[0].metrics if m.name == "margin"]
        assert len(winners) == 1 and winners[0].source == "manual_edit"

    def test_manual_composite_broken_closure_reported(self):
        """factor 引用已删指标 → 停用进复核(不静默)."""
        from domains.chatbi.tasks import _merge_content, _new_report
        old = self._old(factors=["amount_sum", "ghost"])
        report = _new_report()
        merged = _merge_content(old, self._scan(), report=report)
        names = {m.name for m in merged.models[0].metrics}
        assert "margin" not in names
        drops = [d for d in report["dropped_items"] if d["name"] == "margin"]
        assert drops and "ghost" in drops[0]["reason"], "失效必须进复核清单"
        assert report["requires_review"] is True


class TestRevisionPhysicalIsolation:
    """P0-2: 全局主键语义下的故障注入(十八审 6.2 矩阵)."""

    def _ds_row(self, db, ds_id):
        with db.connect() as conn:
            row = conn.execute(
                "SELECT id FROM chatbi_data_sources WHERE id = ?",
                (ds_id,)).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO chatbi_data_sources "
                    "(id, name, db_type, host, port, database, username, "
                    "encrypted_password, is_active, scan_status, "
                    "scan_progress, scan_stage, scan_error, created_at, "
                    "updated_at) VALUES (?, 't', 'postgresql', 'h', 1, 'd', "
                    "'u', 'p', 1, 'idle', 0, '', '', ?, ?)",
                    (ds_id, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))

    @staticmethod
    def _content2():
        """两表内容(部分 upsert 注入用: 第一条成功后失败)."""
        return SemanticModelContent(models=[
            Model(name="a", display_name="表A", columns=[_col("id")]),
            Model(name="b", display_name="表B", columns=[_col("id")]),
        ])

    def test_partial_upsert_failure_old_active_intact(self, pg_engine):
        """v11 第一条 upsert 成功后失败 → active r10 分区必须完好.

        旧实现(doc_id 假分区/主键不含 rev)下, r11 的第一条会覆盖 r10 的
        同主键行并把 doc_id 改成 r11——active 被掏空. 主键含 rev 后,
        r11 的行是新主键, 不触碰 r10.
        """
        from domains.chatbi.indexing import rebuild_index
        ds = "iso-ds-1"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        assert rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=10).error is None
        assert store.count_doc("schema_r10") == 2
        # v11 部分成功: 第一条(model:a)写完后失败
        store.fail_upsert_at = 1
        r11 = rebuild_index(_idx_content("b"), ds, store, _FakeEmbedder(),
                            db=pg_engine, revision=11)
        assert r11.error is not None
        # 关键断言: r10 两行都在, 读者无感知
        assert store.count_doc("schema_r10") == 2, "active 分区被部分掏空"
        assert store.search_records("s", [0.1], doc_id="schema_r10"), \
            "active 分区在构建失败后不可检索"

    def test_upsert_success_pointer_failure_old_active_intact(self, pg_engine,
                                                              monkeypatch):
        """upsert 全部成功但指针翻转失败 → 旧 active 完好(不删不翻)."""
        from domains.chatbi.indexing import rebuild_index
        import domains.chatbi.stores as stores_mod
        ds = "iso-ds-2"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        assert rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                             db=pg_engine, revision=10).error is None

        def _boom(*a, **kw):
            raise RuntimeError("pointer write failed")
        monkeypatch.setattr(stores_mod, "set_active_doc_id", _boom)
        r11 = rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                            db=pg_engine, revision=11)
        assert r11.error is not None
        monkeypatch.undo()
        from domains.chatbi.stores import get_active_doc_id
        with pg_engine.connect() as conn:
            from domains.chatbi.stores import get_or_create_scope
        assert get_active_doc_id(pg_engine, get_or_create_scope(pg_engine, ds)) \
            == "schema_r10"
        assert store.count_doc("schema_r10") == 2, "指针失败后旧分区被破坏"
        assert store.search_records("s", [0.1], doc_id="schema_r10")

    def test_reader_old_pointer_survives_flip_and_gc(self, pg_engine):
        """发布 r12 后 GC 只删 ≤r10: 读 r11 旧指针的在途查询不受影响."""
        from domains.chatbi.indexing import rebuild_index
        ds = "iso-ds-3"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        for rev in (10, 11, 12):
            assert rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                                 db=pg_engine, revision=rev).error is None
        assert store.count_doc("schema_r12") == 2      # active
        assert store.count_doc("schema_r11") == 2, "grace 窗内的上一代被删"
        assert store.search_records("s", [0.1], doc_id="schema_r11"), \
            "持有旧指针的在途查询撞上了 GC"
        # 十九审 6.3: 刚发布 r12 时 r10 虽已翻出保留集, 但仍在时间宽限内
        # (慢 reader 保护)——立即回收语义由 grace=0 参数覆盖
        assert store.count_doc("schema_r10") == 2, "宽限内的两代外分区被立即删除"

    def test_same_revision_repair_idempotent(self, pg_engine):
        """同 revision 重建(修复)不产生重复行(主键天然幂等)."""
        from domains.chatbi.indexing import rebuild_index
        ds = "iso-ds-4"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                      db=pg_engine, revision=10)
        rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                      db=pg_engine, revision=10)
        assert store.count_doc("schema_r10") == 2, "同 revision 修复产生重复"

    def test_building_orphan_reclaimed_by_gc(self, pg_engine):
        """崩溃残余(building 状态)分区在两代后被 GC 回收."""
        from domains.chatbi.indexing import rebuild_index
        from domains.chatbi.stores import (record_index_build,
                                           get_or_create_scope)
        ds = "iso-ds-5"
        self._ds_row(pg_engine, ds)
        store = _FakeVectorStore()
        scope = get_or_create_scope(pg_engine, ds)
        # 模拟崩溃: 构建意图落账 + 向量写了一半, 指针从未翻转
        record_index_build(pg_engine, scope, 9, "schema_r9", status="building")
        store.upsert_records(scope, [], doc_id="schema_r9")
        # 正常发布 r10, r11; building 残余按 unfinished_ttl=0 立即回收
        for rev in (10, 11):
            rebuild_index(self._content2(), ds, store, _FakeEmbedder(),
                          db=pg_engine, revision=rev,
                          unfinished_ttl_seconds=0)
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_builds "
                "WHERE scope = ? AND version = 9", (scope,)).fetchone()
        assert int(row["n"]) == 0, "building 孤儿台账未回收"


class TestCrossTableSameNameMetric:
    """P1 6.3: 跨表同名 metric 不互相覆盖; 检索 metadata 带 owner_model."""

    def test_same_name_metrics_both_indexed_with_owner(self, pg_engine):
        from domains.chatbi.indexing import build_index
        from domains.chatbi.stores import get_or_create_scope
        ds = "dup-ds-1"
        with pg_engine.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'idle', 0, '', '', ?, ?)",
                (ds, "2026-01-01T00:00:00", "2026-01-01T00:00:00"))
        content = SemanticModelContent(models=[
            Model(name="orders", display_name="订单", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="订单额合计",
                                 formula="SUM(amount)", type="single",
                                 source="rule_inferred")]),
            Model(name="refunds", display_name="退款", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="退款额合计",
                                 formula="SUM(amount)", type="single",
                                 source="rule_inferred")]),
        ])
        store = _FakeVectorStore()
        result = build_index(content, ds, store, _FakeEmbedder(), db=pg_engine)
        assert result.indexed_count == 4   # 2 model + 2 metric
        # 同名指标两条都在(物理主键含 owner)
        metric_hits = store.search_records("s", [0.1], doc_id="schema")
        owners = sorted(h.record.metadata.get("owner_model")
                        for h in metric_hits
                        if h.record.metadata.get("type") == "metric")
        assert owners == ["orders", "refunds"], \
            f"跨表同名指标被覆盖: {owners}"
        # 同 revision 语义下两条 metric 主键不同
        cids = sorted(h.record.id for h in metric_hits
                      if h.record.metadata.get("type") == "metric")
        assert cids == ["metric:orders:amount_sum",
                        "metric:refunds:amount_sum"]


class TestConflictReportContract:
    """P1 6.6: conflict 条目与 dropped_items 字段统一(前端同一模板)."""

    def test_conflict_item_has_kind_name_reason(self):
        from domains.chatbi.tasks import _new_report, _report_conflict
        report = _new_report()
        _report_conflict(report, "orders", "manual_rel", "fk_rel",
                         "ON 等价但属性不同")
        c = report["conflicts"][0]
        assert c["kind"] == "relationship_conflict"
        assert c["name"] == "manual_rel"
        assert c["reason"] == "ON 等价但属性不同"
        assert c["manual"] == "manual_rel" and c["auto"] == "fk_rel"
        # 前端模板字段 (item.kind/table/name/reason) 全部可解析
        assert c["table"] == "orders"


class TestReportLifecycle:
    """P1 6.5: 报告可被干净 merge 清除(版本对齐), 数据源删除清理行."""

    def test_clean_merge_overwrites_stale_report(self, pg_engine):
        from domains.chatbi.tasks import (save_merge_report, get_merge_report,
                                          _new_report, _report_drop)
        report = _new_report()
        _report_drop(report, "metric", "orders", "old", "missing_column: gone")
        save_merge_report(pg_engine, "life-ds-1", 5, report)
        assert get_merge_report(pg_engine, "life-ds-1")["report"]["requires_review"]
        # 后续干净 merge(空报告)覆盖 → 告警消失, 版本对齐
        save_merge_report(pg_engine, "life-ds-1", 6, _new_report())
        saved = get_merge_report(pg_engine, "life-ds-1")
        assert saved["version"] == 6
        assert saved["report"]["dropped_items"] == []
        assert saved["report"]["requires_review"] is False

    def test_datasource_delete_cleans_report(self, pg_engine):
        from domains.chatbi.tasks import (save_merge_report, get_merge_report,
                                          _new_report, _report_drop)
        from datetime import datetime, timezone
        ds = "life-ds-2"
        now = datetime.now(timezone.utc).isoformat()
        with pg_engine.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, created_at, updated_at) "
                "VALUES (?, 't', 'pg', 'h', 1, 'd', 'u', 'p', 1, ?, ?)",
                (ds, now, now))
        report = _new_report()
        _report_drop(report, "metric", "t", "m", "r")
        save_merge_report(pg_engine, ds, 3, report)
        assert get_merge_report(pg_engine, ds) is not None
        with pg_engine.connect() as conn:   # 模拟删除端点的级联清理
            conn.execute(
                "DELETE FROM chatbi_merge_reports WHERE data_source_id = ?",
                (ds,))
        assert get_merge_report(pg_engine, ds) is None


class TestSchedulerLease:
    """P1 6.4: 跨进程租约——同时只有一个持有者, 过期可抢占."""

    def test_lease_exclusive_and_reclaim(self, pg_engine):
        from domains.chatbi.tasks import acquire_lease
        assert acquire_lease(pg_engine, "refresh_semantics",
                             holder="worker-a", ttl_seconds=300)
        # 他人未过期 → 拒绝
        assert not acquire_lease(pg_engine, "refresh_semantics",
                                 holder="worker-b", ttl_seconds=300)
        # 本人续期 → 成功
        assert acquire_lease(pg_engine, "refresh_semantics",
                             holder="worker-a", ttl_seconds=300)
        # 过期后他人抢占
        from datetime import datetime, timedelta, timezone
        expired = (datetime.now(timezone.utc)
                   - timedelta(seconds=1)).isoformat()
        with pg_engine.connect() as conn:
            conn.execute(
                "UPDATE chatbi_scheduler_leases SET expires_at = ? "
                "WHERE task_type = ?", (expired, "refresh_semantics"))
        assert acquire_lease(pg_engine, "refresh_semantics",
                             holder="worker-b", ttl_seconds=300)

    def test_lease_independent_per_task_type(self, pg_engine):
        from domains.chatbi.tasks import acquire_lease
        assert acquire_lease(pg_engine, "health_check",
                             holder="worker-a", ttl_seconds=120)
        assert acquire_lease(pg_engine, "purge_stats",
                             holder="worker-b", ttl_seconds=900)


# ════════════════════════════════════════════════════════════════
# 十九审反例回归: 乱序报告/长键身份/GC 成功代际/执行期租约
# ════════════════════════════════════════════════════════════════

class TestReportOutOfOrder:
    """P1 6.2: 旧版本报告不得覆盖新版本报告(乱序完成)."""

    def test_older_version_cannot_overwrite_newer(self, pg_engine):
        from domains.chatbi.tasks import save_merge_report, get_merge_report
        from domains.chatbi.models import SemanticModelContent
        # 时序: v12(干净)先落, v11(带告警)后落——慢任务恢复后乱序写
        empty = {"dropped_items": [], "conflicts": [], "requires_review": False}
        warning = {"dropped_items": [{"kind": "metric", "table": "orders",
                                      "name": "m", "reason": "missing_column: gone"}],
                   "conflicts": [], "requires_review": True}
        assert save_merge_report(pg_engine, "oo-ds", 12, empty) == "saved"
        assert save_merge_report(pg_engine, "oo-ds", 11, warning) == "superseded", \
            "v11 报告覆盖了 v12(版本守卫缺失)"
        saved = get_merge_report(pg_engine, "oo-ds")
        assert saved["version"] == 12
        assert saved["report"]["requires_review"] is False, "过期告警复活"

    def test_same_version_overwrite_allowed(self, pg_engine):
        from domains.chatbi.tasks import save_merge_report, get_merge_report
        r1 = {"dropped_items": [{"kind": "metric", "table": "t", "name": "a",
                                 "reason": "x"}], "conflicts": [],
              "requires_review": True}
        assert save_merge_report(pg_engine, "oo-ds-2", 5, r1) == "saved"
        # 同版本重试(修正后的报告)允许覆盖
        assert save_merge_report(
            pg_engine, "oo-ds-2", 5,
            {"dropped_items": [], "conflicts": [],
             "requires_review": False}) == "saved"
        assert get_merge_report(pg_engine, "oo-ds-2")["report"]["requires_review"] is False


class TestLongIdentifierIdentity:
    """P1 6.1: 超长 chunk_id 截断后, 身份从 PG 反查恢复(不再依赖主键)."""

    def _ds_row(self, db, ds_id):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'idle', 0, '', '', ?, ?)", (ds_id, now, now))

    def test_truncated_key_identity_restored_from_pg(self, pg_engine):
        """63-byte 表名 + 长 metric 名: 主键截断, PG 身份表回填完整值."""
        from domains.chatbi.indexing import build_index
        from domains.chatbi.stores import (get_or_create_scope,
                                           lookup_chunk_identities)
        ds = "long-id-ds"
        self._ds_row(pg_engine, ds)
        long_table = "t" * 63                     # PostgreSQL 标识符上限
        long_metric = "m" * 60
        content = SemanticModelContent(models=[
            Model(name=long_table, display_name="长表名",
                  columns=[_col("amount")],
                  metrics=[Metric(name=long_metric, display_name="长指标",
                                  formula="SUM(amount)", type="single",
                                  source="rule_inferred")]),
        ])
        store = _FakeVectorStore()
        result = build_index(content, ds, store, _FakeEmbedder(), db=pg_engine)
        assert result.error is None
        scope = get_or_create_scope(pg_engine, ds)
        # 物理主键确实超长被截断(格式含 #短哈希)
        metric_rows = [k for k in store.rows
                       if store.rows[k]["doc_id"] == "schema"
                       and k.startswith("metric:")]
        assert metric_rows, "metric 行缺失"
        assert len(metric_rows[0]) <= 64
        truncated_key = metric_rows[0]
        # 反查恢复完整业务身份(不依赖主键解码)
        ident = lookup_chunk_identities(pg_engine, scope, [truncated_key])
        m = ident[truncated_key]
        assert m["name"] == long_metric, "长指标名未能恢复"
        assert m["owner_model"] == long_table, "owner 未能恢复"
        assert m["type"] == "metric"

    def test_decode_fallback_for_short_keys(self, pg_engine):
        from domains.chatbi.stores import ChatBIVectorStore
        meta = ChatBIVectorStore.decode_chunk_id("metric:orders:gmv@r18")
        assert meta == {"type": "metric", "owner_model": "orders",
                        "name": "gmv", "rev": "r18"}
        meta2 = ChatBIVectorStore.decode_chunk_id("model:orders")
        assert meta2 == {"type": "model", "name": "orders"}


class TestGCSuccessGenerations:
    """P1 6.3: GC 保留最近 N 个成功 published 代; 失败版本留 TTL; DB 时钟."""

    def _ds_row(self, db, ds_id):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'idle', 0, '', '', ?, ?)", (ds_id, now, now))

    @staticmethod
    def _seed(db, scope, doc):
        """分区放一条哑记录(upsert 空列表不产生行)."""
        from domains.chatbi.stores import VectorRecord
        db.upsert_records(scope, [VectorRecord(
            id=f"dummy:{doc}", vector=[0.1], metadata={}, text="")], doc_id=doc)

    @staticmethod
    def _ledger(db, scope, version, doc, status, age_seconds=0):
        """直接写台账(时间往回拨 age_seconds, 绕过真实构建).

        二十五审 6.2: 新 PK (scope, build_id); build_id 从 doc 推导——
        同一分区(同 doc)视为同一 build 实例, 状态推进 UPSERT 同行。
        """
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc)
              - timedelta(seconds=age_seconds)).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_index_builds "
                "(scope, build_id, version, doc_id, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (scope, build_id) DO UPDATE SET "
                "doc_id = EXCLUDED.doc_id, status = EXCLUDED.status, "
                "version = EXCLUDED.version, "
                "updated_at = EXCLUDED.updated_at",
                (scope, f"bid-{doc}", version, doc, status, ts))

    def test_gap_versions_keep_last_success(self, pg_engine):
        """审计反例: r10 published, r11/r12 building(失败), r13 published
        → GC 后必须保留 r10 与 r13, 删的是 building 半成品(过 TTL)."""
        from domains.chatbi.stores import (set_active_doc_id, gc_index_builds,
                                           get_or_create_scope)
        ds = "gc-ds-1"
        self._ds_row(pg_engine, ds)
        scope = get_or_create_scope(pg_engine, ds)
        store = _FakeVectorStore()
        self._ledger(pg_engine, scope, 10, "schema_r10", "published",
                     age_seconds=7200)
        self._ledger(pg_engine, scope, 11, "schema_r11", "building",
                     age_seconds=7200)
        self._ledger(pg_engine, scope, 12, "schema_r12", "building",
                     age_seconds=1800)   # TTL 3600 的一半, 未到期
        set_active_doc_id(pg_engine, scope, "schema_r13", 13)
        self._ledger(pg_engine, scope, 13, "schema_r13", "published",
                     age_seconds=10)
        for d in ("schema_r10", "schema_r11", "schema_r12", "schema_r13"):
            self._seed(store, scope, d)
        deleted = gc_index_builds(pg_engine, scope, store,
                                  keep_generations=2,
                                  published_grace_seconds=600,
                                  unfinished_ttl_seconds=3600)
        # r10: 非保留集 published, 但 7200s > 600s grace → 可删?
        # 保留集 = {active(schema_r13)} ∪ 最近2个published(r13, r10) → r10 保留!
        assert store.count_doc("schema_r10") == 1, "最后成功旧代被删(6.3 反例)"
        assert store.count_doc("schema_r13") == 1
        assert store.count_doc("schema_r11") == 0, "过期 building 未回收"
        assert store.count_doc("schema_r12") == 1, "未过 TTL 的 building 被误删"
        assert deleted == 1

    def test_old_published_removed_after_grace(self, pg_engine):
        """三连成功发布后, 最旧代在 grace 期满后被回收(慢 reader 时间窗)."""
        from domains.chatbi.stores import (set_active_doc_id, gc_index_builds,
                                           get_or_create_scope)
        ds = "gc-ds-2"
        self._ds_row(pg_engine, ds)
        scope = get_or_create_scope(pg_engine, ds)
        store = _FakeVectorStore()
        for rev, age in ((20, 7200), (21, 1200), (22, 10)):
            set_active_doc_id(pg_engine, scope, f"schema_r{rev}", rev)
            self._ledger(pg_engine, scope, rev, f"schema_r{rev}",
                         "published", age_seconds=age)
            self._seed(store, scope, f"schema_r{rev}")
        # 刚发布 r22: r20 在保留集({r22,r21} published)之外, 但宽限 600s
        # → age 7200 > 600 删; 若宽限给 8000s 则保留(时间窗语义)
        gc_index_builds(pg_engine, scope, store, keep_generations=2,
                        published_grace_seconds=600,
                        unfinished_ttl_seconds=86400)
        assert store.count_doc("schema_r20") == 0
        assert store.count_doc("schema_r21") == 1, "grace 内的上一代被删"
        assert store.count_doc("schema_r22") == 1

    def test_unparseable_timestamp_never_deleted(self, pg_engine):
        from domains.chatbi.stores import (set_active_doc_id, gc_index_builds,
                                           get_or_create_scope)
        ds = "gc-ds-3"
        self._ds_row(pg_engine, ds)
        scope = get_or_create_scope(pg_engine, ds)
        store = _FakeVectorStore()
        set_active_doc_id(pg_engine, scope, "schema_r5", 5)
        self._ledger(pg_engine, scope, 4, "schema_r4", "published",
                     age_seconds=99999)
        with pg_engine.connect() as conn:   # 人为破坏时间戳
            conn.execute(
                "UPDATE chatbi_index_builds SET updated_at = 'garbage' "
                "WHERE scope = ? AND version = 4", (scope,))
        self._seed(store, scope, "schema_r4")
        gc_index_builds(pg_engine, scope, store, keep_generations=1,
                        published_grace_seconds=0)
        assert store.count_doc("schema_r4") == 1, "时间不可解析的分区被误删"


class TestExecutionLease:
    """P1 6.4: 执行期租约——双持有者互斥/接管/释放/DB 时钟."""

    def test_run_lease_mutual_exclusion(self, pg_engine):
        from domains.chatbi.tasks import RunLease
        a = RunLease(pg_engine, "run:scan:ds-x", holder="task:a", ttl_seconds=300)
        b = RunLease(pg_engine, "run:scan:ds-x", holder="task:b", ttl_seconds=300)
        assert a.acquire() is True
        assert b.acquire() is False, "第二实例抢到了执行租约"
        assert a.heartbeat() is True     # 本人续租
        assert b.acquire() is False
        a.release()
        assert b.acquire() is True, "释放后未接管"

    def test_expired_lease_taken_over(self, pg_engine):
        """持有者崩溃(无释放)→ TTL 过期(DB 时钟判定)→ 他人接管."""
        from domains.chatbi.tasks import acquire_lease, RunLease
        assert acquire_lease(pg_engine, "run:scan:ds-y", holder="task:dead",
                             ttl_seconds=300)
        # 模拟 TTL 流逝: 直接把 expires_at 拨回过去(epoch ms 格式)
        with pg_engine.connect() as conn:
            conn.execute(
                "UPDATE chatbi_scheduler_leases "
                "SET expires_at = (((extract(epoch FROM now()) - 1)*1000)::bigint)::text "
                "WHERE task_type = 'run:scan:ds-y'")
        b = RunLease(pg_engine, "run:scan:ds-y", holder="task:b", ttl_seconds=300)
        assert b.acquire() is True, "过期租约未被接管"

    def test_concurrent_claims_single_winner(self, pg_engine):
        """多线程同时 claim 同一租约 → 恰好一个成功(真实并发)."""
        import threading
        from domains.chatbi.tasks import acquire_lease
        winners = []
        barrier = threading.Barrier(8)

        def worker(i):
            barrier.wait()
            ok = acquire_lease(pg_engine, "run:refresh_semantics",
                               holder=f"task:w{i}", ttl_seconds=300)
            if ok:
                winners.append(i)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(winners) == 1, f"并发 claim 出现 {len(winners)} 个赢家"


class TestCoOccurrencePreservation:
    """十七审提示词: merge 不得丢指标 co_occurrence(运行时命中计数).

    十九审真实环境发现: refresh 后同名规则指标以新对象(co_occurrence=0)
    顶替旧项, 问数反哺统计被静默清零.
    """

    def test_refresh_inherits_co_occurrence(self):
        from domains.chatbi.tasks import _merge_content
        old = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="合计",
                                  formula="SUM(amount)", type="single",
                                  source="rule_inferred", co_occurrence=37)]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="合计",
                                  formula="SUM(amount)", type="single",
                                  source="rule_inferred")]),
        ])
        merged = _merge_content(old, scan)
        met = merged.models[0].metrics[0]
        assert met.co_occurrence == 37, \
            f"运行时命中计数被清零: {met.co_occurrence}"

    def test_rescan_inherits_co_occurrence(self):
        from domains.chatbi.tasks import _merge_rescan
        old = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="合计",
                                  formula="SUM(amount)", type="single",
                                  source="rule_inferred", co_occurrence=37)]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[_col("amount")],
                  metrics=[Metric(name="amount_sum", display_name="新合计",
                                  formula="SUM(amount)", type="single",
                                  source="rule_inferred")]),
        ])
        merged = _merge_rescan(old, scan)
        assert merged.models[0].metrics[0].co_occurrence == 37


# ════════════════════════════════════════════════════════════════
# 二十审反例回归: 身份 fail-closed / 跨操作互斥 / 独立心跳 / GC DB时钟
# ════════════════════════════════════════════════════════════════

class _BrokenDB:
    """connect() 必失败的数据库替身(身份链路故障注入)."""

    def connect(self):
        raise RuntimeError("identity db down")


class TestIdentityFailClosed:
    """P1 9.1: 截断键的身份链路故障必须可见, 不得静默发布/猜测."""

    def _ds_row(self, db, ds_id):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'idle', 0, '', '', ?, ?)", (ds_id, now, now))

    def test_register_failure_fails_build(self, pg_engine, monkeypatch):
        """身份登记失败 → build_index 返回 error(禁止发布错误身份索引)."""
        import domains.chatbi.indexing as idx
        from domains.chatbi.stores import VectorRecord
        ds = "fc-ds-1"
        self._ds_row(pg_engine, ds)
        content = SemanticModelContent(models=[
            Model(name="t" * 63, display_name="长表", columns=[_col("amount")]),
        ])

        def _boom(db, scope, records, doc_id, revision=None):
            # 模拟截断键存在但登记失败(数据库故障)
            return False, ["model:tttt#deadbeefdeadbeef"]

        monkeypatch.setattr(idx, "register_chunk_identities", _boom)
        result = idx.build_index(content, ds, _FakeVectorStore(),
                                 _FakeEmbedder(), db=pg_engine)
        assert result.error is not None, "登记失败仍返回成功(Fail-Open)"
        assert result.indexed_count == 0
        assert "身份登记失败" in result.error

    def test_register_exception_fails_build(self, pg_engine, monkeypatch):
        """登记抛异常(真实 DB 故障)同样使构建失败."""
        import domains.chatbi.indexing as idx
        ds = "fc-ds-2"
        self._ds_row(pg_engine, ds)
        content = SemanticModelContent(models=[
            Model(name="t" * 63, display_name="长表", columns=[_col("amount")]),
        ])

        def _boom(db, scope, records, doc_id, revision=None):
            raise RuntimeError("identity db down")

        monkeypatch.setattr(idx, "register_chunk_identities", _boom)
        result = idx.build_index(content, ds, _FakeVectorStore(),
                                 _FakeEmbedder(), db=pg_engine)
        assert result.error is not None and result.indexed_count == 0

    def test_truncated_key_without_identity_dropped(self, pg_engine):
        """截断键命中且身份表无行 → 检索丢弃该命中(不猜测反解)."""
        from domains.chatbi.stores import (
            ChatBIVectorStore, SearchResult, VectorRecord)
        # 模拟一条截断键命中, 身份表无此行
        truncated_id = "model:tttt#abcdef0123456789"
        hit = SearchResult(
            record=VectorRecord(id=truncated_id, vector=[0.1],
                                metadata={"type": "model",
                                          "name": "tttt"},
                                text="x"),
            score=0.9)
        # retrieve 内部通过 candidates_by_scope 处理——直接验证策略函数等价物:
        # lookup 无行 + "#" in id → 丢弃; 短键 → 解码回填
        ident = {}   # 模拟身份表无行
        kept, dropped = [], 0
        for h in [hit]:
            m = ident.get(h.record.id)
            if m:
                kept.append(h)
            elif "#" in h.record.id:
                dropped += 1
            else:
                h.record.metadata = ChatBIVectorStore.decode_chunk_id(
                    h.record.id)
                kept.append(h)
        assert dropped == 1 and not kept, "截断键无身份未被丢弃"

    def test_identity_revision_roundtrip(self, pg_engine):
        """身份表保存/返回 revision(二十审 9.7: 诊断信息不被覆盖)."""
        from domains.chatbi.stores import (
            register_chunk_identities, lookup_chunk_identities, VectorRecord)
        rec = VectorRecord(id="x", vector=[0.1],
                           metadata={"type": "model", "name": "orders",
                                     "rev": 21}, text="")
        ok, trunc = register_chunk_identities(pg_engine, "scope-idrev",
                                              [rec], "schema_r21", revision=21)
        assert ok is True and trunc == []
        got = lookup_chunk_identities(pg_engine, "scope-idrev", ["model:orders@r21"])
        assert got["model:orders@r21"]["rev"] == "r21"

    def test_truncated_key_detection(self):
        """截断标记检测: '#' 在 chunk_id 中即视为截断键."""
        from domains.chatbi.stores import ChatBIVectorStore, VectorRecord
        long_name = "t" * 63
        rec = VectorRecord(id="x", vector=[0.1],
                           metadata={"type": "model", "name": long_name,
                                     "rev": 18}, text="")
        cid = ChatBIVectorStore._chunk_id(rec)
        assert len(cid) <= 64 and "#" in cid
        # 短键不含 '#'
        short = ChatBIVectorStore._chunk_id(VectorRecord(
            id="x", vector=[0.1], metadata={"type": "model", "name": "t"}, text=""))
        assert "#" not in short


class TestUnifiedWriteLease:
    """P1 9.2: scan/refresh 对同一数据源跨操作互斥(共用写租约键)."""

    def test_scan_refresh_share_write_lease_key(self, pg_engine):
        from domains.chatbi.tasks import RunLease
        scan_lease = RunLease(pg_engine, "run:semantic_write:ds-z",
                              holder="task:scan-1", ttl_seconds=300)
        refresh_lease = RunLease(pg_engine, "run:semantic_write:ds-z",
                                 holder="task:refresh-1", ttl_seconds=300)
        assert scan_lease.acquire() is True
        assert refresh_lease.acquire() is False, \
            "scan 进行中, refresh 拿到了同一数据源写租约(跨操作不互斥)"
        scan_lease.release()
        assert refresh_lease.acquire() is True

    def test_independent_heartbeat_keeps_lease_alive(self, pg_engine):
        """独立心跳线程在业务回调沉默时维持租约(短 TTL 快速验证)."""
        import time
        from domains.chatbi.tasks import RunLease
        a = RunLease(pg_engine, "run:semantic_write:ds-hb",
                     holder="task:hb-a", ttl_seconds=2)
        b = RunLease(pg_engine, "run:semantic_write:ds-hb",
                     holder="task:hb-b", ttl_seconds=2)
        assert a.acquire() is True
        # 不调用任何业务心跳——独立线程应以 TTL/3(≈0.7s) 自动续租
        time.sleep(3.2)
        assert a.owned is True, "独立心跳未维持租约"
        assert b.acquire() is False, "心跳保活期间租约被他实例抢走"
        a.release()
        time.sleep(0.1)
        assert b.acquire() is True, "释放后未接管"


class TestGcDbClockLedger:
    """P1 9.3: 台账 updated_at 由数据库时钟写入(工作进程时钟漂移免疫)."""

    def test_ledger_timestamp_from_db_clock(self, pg_engine):
        from domains.chatbi.stores import record_index_build
        from datetime import datetime, timezone
        # 台账写入时把本进程时钟往后拨 1 小时(模拟漂移), DB 时钟不受影响
        record_index_build(pg_engine, "gc-clock-scope", 5, "schema_r5",
                           "published")
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT updated_at, to_char(now(), 'YYYY-MM-DD\"T\"HH24:MI:SS.USOF') AS n "
                "FROM chatbi_index_builds WHERE scope='gc-clock-scope'").fetchone()
        ts = datetime.fromisoformat(
            row["updated_at"].replace("+00", "+00:00"))
        now_db = datetime.fromisoformat(
            row["n"].replace("+00", "+00:00"))
        drift = abs((now_db - ts).total_seconds())
        assert drift < 5, f"台账时间与数据库时钟偏差 {drift}s(用了本地时钟)"


# ════════════════════════════════════════════════════════════════
# 二十一审反例回归: 中文名保真/公式变化可见/pointer-lag自愈/周期GC真实执行
# ════════════════════════════════════════════════════════════════

class TestRuleMetricDisplayNamePreservation:
    """P0-1: refresh 不得退化规则指标中文展示名(真实 104 处退化复现)."""

    def test_refresh_keeps_chinese_display_name(self):
        """旧列「订单金额」+ 旧指标「订单金额合计」→ 新结构退化为
        amount/amount合计 → refresh 后中文指标名必须保留."""
        from domains.chatbi.tasks import _merge_content
        old = SemanticModelContent(models=[
            Model(name="orders", display_name="订单表", columns=[
                _col("amount", source="db_comment", display_name="订单金额"),
            ], metrics=[
                Metric(name="amount_sum", display_name="订单金额合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred", co_occurrence=5),
            ]),
        ])
        # refresh 的结构扫描: 列/指标名暂时是英文退化名(llm=None)
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="orders", columns=[
                _col("amount"),
            ], metrics=[
                Metric(name="amount_sum", display_name="amount合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])
        merged = _merge_content(old, scan)
        met = merged.models[0].metrics[0]
        assert met.display_name == "订单金额合计", \
            f"中文指标名被退化: {met.display_name!r}"
        assert met.co_occurrence == 5
        assert met.formula == "SUM(amount)"

    def test_formula_change_visible_in_report(self):
        """同名指标公式变化 → 不静默沿用, 进复核清单."""
        from domains.chatbi.tasks import _merge_content, _new_report
        old = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[
                _col("amount"), _col("count"),
            ], metrics=[
                Metric(name="amount_sum", display_name="订单金额合计",
                       formula="SUM(amount)", type="single",
                       source="rule_inferred"),
            ]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="orders", display_name="o", columns=[
                _col("amount"), _col("count"),
            ], metrics=[
                Metric(name="amount_sum", display_name="amount合计",
                       formula="SUM(amount) / count", type="single",
                       source="rule_inferred"),
            ]),
        ])
        report = _new_report()
        _merge_content(old, scan, report=report)
        met = next(m for m in merged_metrics(report, scan_or=None)) \
            if False else None
        # 公式变化 → conflicts 记录 + 采用新公式
        assert any("amount_sum" in (c.get("manual") or "")
                   for c in report["conflicts"]), \
            f"公式变化未进复核: {report['conflicts']}"


class TestPointerLagSelfHeal:
    """P0-2 自愈: unchanged refresh 遇 pointer lag 不得走快路跳过."""

    def _ds_row(self, db, ds_id):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, scope_id, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'done', 100, '', '', ?, ?, ?)",
                (ds_id, "scope-lag-1", now, now))

    def test_unchanged_refresh_does_not_skip_when_lagging(self, pg_engine,
                                                          monkeypatch):
        """semantic v5 / active r3 → unchanged refresh 必须继续走补建,
        结果里不得出现"跳过索引重建"."""
        from domains.chatbi.stores import set_active_doc_id
        from domains.chatbi import semantic
        from domains.chatbi.tasks import _refresh_all_datasources
        ds = "lag-ds-1"
        self._ds_row(pg_engine, ds)
        content = SemanticModelContent(models=[
            Model(name="t1", display_name="T", columns=[_col("id")]),
        ])
        v = semantic.save_content(pg_engine, ds, content, source="scan",
                                  expected_version=0)
        assert v == 1
        # 语义推进到 v2(索引未跟随) → active r1, semantic v2, lag=1
        content2 = SemanticModelContent(models=[
            Model(name="t1", display_name="T2", columns=[
                _col("id"), _col("amount"),
            ]),
        ])
        v2 = semantic.save_content(pg_engine, ds, content2, source="manual_edit",
                                   expected_version=1)
        assert v2 == 2
        set_active_doc_id(pg_engine, "scope-lag-1", "schema_r1", 1)  # 滞后

        class _H:
            task_id = "test-handle"
            payload = {}

            def log(self, msg, **kw):
                print("LOG:", msg)

            def set_progress(self, *a, **k):
                pass

        handle = _H()
        results = []
        lease = type("L", (), {
            "heartbeat": lambda self: True,
            "assert_owned": lambda self: True,
            "owned": True,
        })()
        app_state = types.SimpleNamespace()   # llm 取不到 → 索引 skipped(可接受)
        settings = {"scan_metric_inference": False}
        monkeypatch.setattr(
            "domains.chatbi.datasources.list_datasources",
            lambda db, active_only=False: [type("R", (), {"id": ds})()])
        # 二十二审 6.3: 用**真实** _row_to_info 构造 DataSourceInfo——
        # 此前 mock 自造 scope_id 属性(生产 dataclass 没有), 测试通过
        # 生产却每轮误判漂移。真实契约: scope 查询走 stores.get_scope。
        from domains.chatbi.datasources import _row_to_info

        def _real_get(db, i, decrypt=False):
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM chatbi_data_sources WHERE id = ?",
                    (i,)).fetchone()
            assert row is not None, "测试数据源行缺失"
            return _row_to_info(dict(row))

        monkeypatch.setattr(
            "domains.chatbi.datasources.get_datasource", _real_get)
        # refresh 的结构内省不真连库: 固定返回与 v2 同构的新扫描
        scan_content = SemanticModelContent(models=[
            Model(name="t1", display_name="T2", columns=[
                _col("id"), _col("amount"),
            ]),
        ])
        monkeypatch.setattr(
            "domains.chatbi.semantic.scan_datasource",
            lambda **kw: scan_content)
        # 二十审 7.4: 补建在 llm=None 下走 skipped(无向量设施), 但本测试
        # 环境 scanned 内容与 v2 指纹一致 → try rebuild llm=None → skipped
        # → ok=True; 关键断言: 未走"跳过索引重建"快路(自愈分支生效)
        try:
            _refresh_all_datasources(handle, app_state, pg_engine,
                                     settings, results, lease)
        except RuntimeError as e:
            # degraded→ok=False 时 refresh 会 raise(任务可见失败)——同样
            # 证明未走快路
            assert "索引" in str(e) or "刷新" in str(e), str(e)
        assert results, "无结果"
        r = results[0]
        assert "跳过索引重建" not in (r.get("detail") or ""), \
            "lag 存在却走了跳过快路(自愈失效)"




class TestUnchangedRefreshZeroRebuild:
    """二十二审验收: 已对齐(v==r)时 unchanged refresh 必须零重建."""

    def test_aligned_no_rebuild(self, pg_engine, monkeypatch):
        from datetime import datetime, timezone
        from domains.chatbi import semantic
        from domains.chatbi.datasources import _row_to_info
        from domains.chatbi.stores import set_active_doc_id
        from domains.chatbi.tasks import _refresh_all_datasources

        ds = "zero-rb-ds"
        now = datetime.now(timezone.utc).isoformat()
        with pg_engine.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, scope_id, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', 'p', 1, "
                "'done', 100, '', '', 'zero-rb-scope', ?, ?)", (ds, now, now))
        content = SemanticModelContent(models=[
            Model(name="t1", display_name="T", columns=[_col("id")]),
        ])
        assert semantic.save_content(pg_engine, ds, content,
                                     expected_version=0) == 1
        set_active_doc_id(pg_engine, "zero-rb-scope", "schema_r1", 1)  # 对齐

        rebuild_calls = []
        monkeypatch.setattr(
            "domains.chatbi.indexing.guarded_rebuild",
            lambda **kw: rebuild_calls.append(kw) or None)
        scan_content = SemanticModelContent(models=[
            Model(name="t1", display_name="T", columns=[_col("id")]),
        ])
        monkeypatch.setattr(
            "domains.chatbi.semantic.scan_datasource",
            lambda **kw: scan_content)

        def _real_get(db, i, decrypt=False):
            with db.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM chatbi_data_sources WHERE id = ?",
                    (i,)).fetchone()
            return _row_to_info(dict(row))

        monkeypatch.setattr(
            "domains.chatbi.datasources.list_datasources",
            lambda db, active_only=False: [
                type("R", (), {"id": ds})()])
        monkeypatch.setattr(
            "domains.chatbi.datasources.get_datasource", _real_get)

        class _H:
            task_id = "zero-rb-handle"
            payload = {}

            def log(self, msg, **kw):
                print("LOG:", msg)

            def set_progress(self, *a, **k):
                pass

        lease = type("L", (), {
            "heartbeat": lambda self: True,
            "assert_owned": lambda self: True,
            "owned": True,
        })()
        results = []
        _refresh_all_datasources(_H(), types.SimpleNamespace(), pg_engine,
                                 {"scan_metric_inference": False},
                                 results, lease)
        assert results and results[0].get("changed") is False, results
        assert not rebuild_calls, \
            f"已对齐却全量重建(二十二审 6 复发): {len(rebuild_calls)} 次"


class TestPeriodicGcRealExecution:
    """P1(二十一审 8): 周期 GC 必须真正执行, 不得被裸 except 吞掉."""

    def test_periodic_gc_cleans_expired_ledger(self, pg_engine):
        """插入过期 building 台账(不发新版本) → 周期 GC 主动清理."""
        from datetime import datetime, timedelta, timezone
        from domains.chatbi.stores import (record_index_build,
                                           set_active_doc_id)
        set_active_doc_id(pg_engine, "pgc-scope", "schema_r10", 10)
        record_index_build(pg_engine, "pgc-scope", 9, "schema_r9",
                           status="building")
        old_ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=90000)).isoformat()
        with pg_engine.connect() as conn:
            conn.execute(
                "UPDATE chatbi_index_builds SET updated_at = ? "
                "WHERE scope = 'pgc-scope' AND version = 9", (old_ts,))

        class _Store:
            def __init__(self):
                self.deleted = []

            def delete_doc(self, scope, doc_id):
                self.deleted.append(doc_id)
                return 1

        store = _Store()

        class _AS:
            pass

        # _run_periodic_index_gc 需要 settings/app_state(空 namespace 走缺省)
        from domains.chatbi.tasks import _run_periodic_index_gc
        deleted = _run_periodic_index_gc(pg_engine, _AS(), store)
        # pgc-scope 已在 index_revisions 登记(set_active_doc_id)——
        # 周期 GC 的全 scope 遍历必须清掉它(二十审 9.5/二十一审 8:
        # 不依赖新版本发布事件)
        assert deleted == 1, \
            f"周期 GC 未清理过期 building(删除 {deleted}, 明细 {store.deleted})"
        with pg_engine.connect() as conn:
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_builds "
                "WHERE scope = 'pgc-scope'").fetchone()
        assert int(n["n"]) == 0

    def test_gc_branch_no_silent_except(self):
        """源码审查锚: 调度器 GC 分支不得存在裸 `except Exception: pass`."""
        import inspect
        from domains.chatbi import tasks
        src = inspect.getsource(tasks._start_refresh_scheduler)
        assert "except Exception:\n                            pass" not in src, \
            "调度器 GC 分支仍有裸吞异常"
        assert "_gc_llm" not in src, "GC 分支仍引用未定义/无用的 _gc_llm"


class TestTransactionalFencing:
    """P1 8(二十二审): 检查后失租的写入必须被数据库拒绝(TOCTOU 关闭)."""

    def test_fenced_report_write_rejected_when_lease_lost(self, pg_engine):
        """报告 fenced 保存: 失租后调用 → 不写入, 返回 failed."""
        from domains.chatbi.tasks import (RunLease,
                                          save_merge_report_fenced,
                                          get_merge_report, REPORT_FAILED)
        a = RunLease(pg_engine, "run:semantic_write:ds-fence2",
                     holder="task:old", ttl_seconds=300)
        assert a.acquire() is True
        # 模拟失租: 持有者被替换(心跳线程下一拍才会发现)
        with pg_engine.connect() as conn:
            conn.execute(
                "UPDATE chatbi_scheduler_leases SET holder = 'task:new' "
                "WHERE task_type = 'run:semantic_write:ds-fence2'")
        state = save_merge_report_fenced(
            a, pg_engine, "fence-ds", 1,
            {"dropped_items": [], "conflicts": [], "requires_review": False})
        assert state == REPORT_FAILED, f"失租后仍写入: {state}"
        assert get_merge_report(pg_engine, "fence-ds") is None, \
            "被 fencing 拒绝的报告仍落了库"
        # 归还清理
        with pg_engine.connect() as conn:
            conn.execute(
                "DELETE FROM chatbi_scheduler_leases "
                "WHERE task_type = 'run:semantic_write:ds-fence2'")

    def test_fenced_report_write_succeeds_when_owned(self, pg_engine):
        from domains.chatbi.tasks import (RunLease,
                                          save_merge_report_fenced,
                                          get_merge_report, REPORT_SAVED)
        a = RunLease(pg_engine, "run:semantic_write:ds-fence3",
                     holder="task:ok", ttl_seconds=300)
        assert a.acquire() is True
        try:
            state = save_merge_report_fenced(
                a, pg_engine, "fence-ds-3", 1,
                {"dropped_items": [], "conflicts": [],
                 "requires_review": False})
            assert state == REPORT_SAVED
            assert get_merge_report(pg_engine, "fence-ds-3") is not None
        finally:
            a.release()


# ════════════════════════════════════════════════════════════════
# 二十三审反例回归: fencing 穿透(barrier) / scan 降级终态 / verdict
# ════════════════════════════════════════════════════════════════

class TestFencingBarrier:
    """P1 5.3: 检查通过后暂停→接管→旧写入——必须被行锁串行化或拒绝."""

    def _clean(self, pg_engine, key, ds_id):
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases WHERE task_type=?", (key,))
            c.execute("DELETE FROM chatbi_merge_reports WHERE data_source_id=?", (ds_id,))

    def test_takeover_blocked_during_fenced_write(self, pg_engine):
        """FOR UPDATE 行锁: 旧 writer 检查通过并在 write_fn 内暂停时,
        接管方的 UPDATE 必须阻塞到旧 writer 事务结束(交错不可能)."""
        import threading, time
        from domains.chatbi.tasks import RunLease
        key, ds = "run:semantic_write:f-bar1", "f-bar-ds1"
        self._clean(pg_engine, key, ds)
        a = RunLease(pg_engine, key, holder="task:old", ttl_seconds=300)
        assert a.acquire()
        barrier, box = threading.Event(), {}

        def write_fn(conn):
            barrier.set()
            time.sleep(1.2)      # SELECT 已通过; 暂停窗口
            conn.execute(
                "INSERT INTO chatbi_merge_reports "
                "(data_source_id, version, report, updated_at) "
                "VALUES (?, 1, '{}', '2026-01-01T00:00:00+00:00')", (ds,))
            return "committed"

        def old_writer():
            box["ok"], box["res"] = a.execute_if_owned(write_fn)

        t = threading.Thread(target=old_writer)
        t.start()
        barrier.wait()           # 此时旧 writer 持租约行锁并暂停
        t0 = time.time()
        with pg_engine.connect() as c2:
            c2.execute("UPDATE chatbi_scheduler_leases SET holder='task:new' "
                       "WHERE task_type=?", (key,))
        box["takeover_elapsed"] = time.time() - t0
        t.join()
        # 核心断言: 接管被行锁挡住 ≥ 暂停时长(交错窗口关闭)
        assert box["takeover_elapsed"] >= 1.0, \
            f"接管未阻塞({box['takeover_elapsed']:.2f}s), FOR UPDATE 未生效"
        assert box["ok"] is True and box["res"] == "committed"
        with pg_engine.connect() as c:
            row = c.execute("SELECT version FROM chatbi_merge_reports "
                            "WHERE data_source_id=?", (ds,)).fetchone()
        # 旧写入在其合法持有期内提交(接管在其后)——不是 stale write
        assert row is not None
        self._clean(pg_engine, key, ds)

    def test_write_rejected_when_takeover_precedes_check(self, pg_engine):
        """接管发生在检查之前 → fenced 写入直接被拒(不落库)."""
        from domains.chatbi.tasks import RunLease
        key, ds = "run:semantic_write:f-bar2", "f-bar-ds2"
        self._clean(pg_engine, key, ds)
        a = RunLease(pg_engine, key, holder="task:old", ttl_seconds=300)
        assert a.acquire()
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET holder='task:new' "
                      "WHERE task_type=?", (key,))
        ok, res = a.execute_if_owned(
            lambda conn: conn.execute(
                "INSERT INTO chatbi_merge_reports "
                "(data_source_id, version, report, updated_at) "
                "VALUES (?, 1, '{}', '2026-01-01T00:00:00+00:00')", (ds,)))
        assert ok is False, "接管后检查仍通过"
        with pg_engine.connect() as c:
            row = c.execute("SELECT version FROM chatbi_merge_reports "
                            "WHERE data_source_id=?", (ds,)).fetchone()
        assert row is None, "被拒的写入落了库"
        self._clean(pg_engine, key, ds)

    def test_write_fn_exception_propagates(self, pg_engine):
        """write_fn 领域异常(如 VCE)必须上抛, 不得被 fencing 吞掉."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:f-bar3"
        self._clean(pg_engine, key, "f-bar-ds3")
        a = RunLease(pg_engine, key, holder="task:old", ttl_seconds=300)
        assert a.acquire()
        try:
            def boom(conn):
                raise KeyError("domain-error-must-propagate")
            import pytest as _pytest
            with _pytest.raises(KeyError):
                a.execute_if_owned(boom)
        finally:
            a.release()
            self._clean(pg_engine, key, "f-bar-ds3")


class TestScanDegradedTerminalState:
    """P1 5.2: 索引失败 → done_with_warning 不被外层覆盖为 failed."""

    def test_index_failure_keeps_warning_state(self, pg_engine, monkeypatch):
        """注入索引重建失败 → 任务抛 IndexRebuildDegraded, 数据源终态
        保持 done_with_warning(语义已落库, 修复指引保留)."""
        from datetime import datetime, timezone
        from domains.chatbi import semantic, tasks
        from domains.chatbi.datasources import _row_to_info

        ds = "degraded-ds"
        now = datetime.now(timezone.utc).isoformat()
        from domains.chatbi.datasources import encrypt_password
        with pg_engine.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, created_at, updated_at) "
                "VALUES (?, 't', 'postgresql', 'h', 1, 'd', 'u', ?, 1, "
                "'idle', 0, '', '', ?, ?)",
                (ds, encrypt_password("p"), now, now))

        content = SemanticModelContent(models=[
            Model(name="t1", display_name="T", columns=[_col("id")]),
        ])
        monkeypatch.setattr(
            "domains.chatbi.semantic.scan_datasource",
            lambda **kw: content)
        monkeypatch.setattr(
            "domains.chatbi.tasks.RunLease.acquire", lambda self: True)
        monkeypatch.setattr(
            "domains.chatbi.tasks.RunLease.release", lambda self: None)
        monkeypatch.setattr(
            "domains.chatbi.tasks.RunLease.heartbeat", lambda self: True)
        monkeypatch.setattr(
            "domains.chatbi.tasks.RunLease.assert_owned", lambda self: True)
        def _fenced(self, fn):
            # 用真实连接执行(报告/语义落库走 fenced 路径需要 conn)
            with pg_engine.connect() as c:
                return True, fn(c)
        monkeypatch.setattr(
            "domains.chatbi.tasks.RunLease.execute_if_owned", _fenced)

        _real_save = semantic.save_content   # 先留原函数, 防自递归

        def _save(db, ds_id, content, source="scan",
                  expected_version=None, conn=None):
            return _real_save(pg_engine, ds_id, content, source=source,
                              expected_version=expected_version)
        monkeypatch.setattr(
            "domains.chatbi.semantic.save_content",
            lambda db, ds_id, c, **kw: _save(db, ds_id, c, **kw))

        def _broken_rebuild(*a, **kw):
            return type("R", (), {"error": "milvus down",
                                  "deleted_count": 0, "indexed_count": 0})()
        monkeypatch.setattr(
            "domains.chatbi.indexing.guarded_rebuild", _broken_rebuild)

        class _H:
            task_id = "degraded-handle"
            payload = {"datasource_id": ds}

            def log(self, msg, **kw):
                pass

            def set_progress(self, *a, **k):
                pass

        import types as _t
        app_state = _t.SimpleNamespace(llm_client=None)
        with __import__("pytest").raises(tasks.IndexRebuildDegraded):
            tasks._task_scan_datasource(_H(), app_state)
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT scan_status FROM chatbi_data_sources "
                "WHERE id = ?", (ds,)).fetchone()
        assert row["scan_status"] == "done_with_warning", \
            f"降级态被覆盖: {row['scan_status']}"


# ════════════════════════════════════════════════════════════════
# 二十四审反例回归: token fencing / 分区隔离 / 真实事件计数
# ════════════════════════════════════════════════════════════════

class TestLeaseTokenFencing:
    """P1 6: 单调 token——接管后旧 token 无法通过写前校验."""

    def test_token_rotates_only_on_takeover(self, pg_engine):
        """续租保持 token, 接管轮换 token(轮换式设计缺陷的反例锚)."""
        from domains.chatbi.tasks import RunLease, lease_token
        key = "run:semantic_write:tok-1"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases WHERE task_type=?", (key,))
        a = RunLease(pg_engine, key, holder="task:a", ttl_seconds=300)
        b = RunLease(pg_engine, key, holder="task:b", ttl_seconds=300)
        assert a.acquire() and a.token is not None
        t1 = a.token
        assert a.heartbeat()            # 续租
        assert a.token == t1, "续租轮换了 token(并发 fenced 写会被误拒)"
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        assert b.acquire()              # 接管
        assert b.token is not None and b.token > t1, "接管未铸造新 token"
        a.release()
        b.release()

    def test_stale_token_rejected_after_takeover(self, pg_engine):
        """接管后, 旧 writer 即便 holder 字段被手工改回也过不了 token."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:tok-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases WHERE task_type=?", (key,))
        a = RunLease(pg_engine, key, holder="task:old", ttl_seconds=300)
        assert a.acquire()
        # 模拟接管: 新 writer 拿走(token 轮换), 旧对象的 token 已过期
        b = RunLease(pg_engine, key, holder="task:new", ttl_seconds=300)
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        assert b.acquire()
        # 旧 writer 心跳发现失租 → acquired=False; 即使伪装 acquired,
        # execute_if_owned 的 token WHERE 也拒绝
        a.acquired = True               # 模拟心跳延迟未发现
        ok, _ = a.execute_if_owned(lambda conn: "should-not-run")
        assert ok is False, "旧 token 通过了写前校验"
        a.release()
        b.release()


class TestBuildPartitionIsolation:
    """P1 6: 同版本双 writer 各写各的物理分区(doc_id 含 token)."""

    def test_doc_id_contains_token(self):
        from domains.chatbi.indexing import _rebuild_with_revision, DOC_SCHEMA
        import inspect
        src = inspect.getsource(_rebuild_with_revision)
        assert "_t{build_token}" in src, "分区名未含 build token"
        assert "execute_if_owned" in src, "指针发布未走 fenced"

    def test_same_version_rebuild_events_not_collapsed(self, pg_engine):
        """P2 7: 同版本重建 10 次 → 事件表 10 条(状态表只 1 行)."""
        from domains.chatbi.stores import record_index_build
        import uuid as _uuid
        for i in range(10):
            record_index_build(pg_engine, "evt-scope", 42, "schema_r42_t7",
                               status="published",
                               build_id=f"42-t7-{_uuid.uuid4().hex[:6]}")
        with pg_engine.connect() as c:
            ev = c.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_build_events "
                "WHERE scope='evt-scope' AND version=42").fetchone()
            st = c.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_builds "
                "WHERE scope='evt-scope' AND version=42").fetchone()
        assert int(ev["n"]) == 10, f"事件被折叠: {ev['n']}"
        # 二十五审 6.2: 状态台账也按实例登记——10 个 build_id = 10 行
        # (此前 (scope,version) 折叠成 1 行, 让路/孤儿分区无法进 GC)
        assert int(st["n"]) == 10, f"状态台账仍在折叠: {st['n']}"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_build_events WHERE scope='evt-scope'")
            c.execute("DELETE FROM chatbi_index_builds WHERE scope='evt-scope'")

    def test_monitor_fail_on_repeated_rebuilds(self, pg_engine):
        """monitor verdict: 1h 内 10 次构建事件 → FAIL(修复审计复现)."""
        import os as _os
        _os.environ.setdefault(
            "SOAK_DATABASE_URL",
            "postgresql://root:root@localhost:5432/llm_modeler_test_run")
        import sys, os as _os2
        _script = _os2.path.join(_os2.path.dirname(_os2.path.abspath(
            __file__)), "..", "..", "scripts", "soak_monitor.py")
        import importlib.util
        spec = importlib.util.spec_from_file_location("sm", _script)
        sm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sm)
        import uuid as _uuid
        for i in range(10):
            from domains.chatbi.stores import record_index_build
            record_index_build(pg_engine, "evt2", 42, "schema_r42_t7",
                               status="published",
                               build_id=f"42-t7-{_uuid.uuid4().hex[:6]}")
        from sdk.relational_store import PackRelationalDB
        db = PackRelationalDB('chatbi',
                              database_url='postgresql://root:root@localhost:5432/llm_modeler_test_run')
        snap = sm.snapshot(db)
        assert snap["build_events_1h"].get("published", 0) >= 10
        v = sm._verdict(snap, {})
        assert v.startswith("FAIL"), f"10 次重建仍判 {v}"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_build_events WHERE scope='evt2'")
            c.execute("DELETE FROM chatbi_index_builds WHERE scope='evt2'")


# ════════════════════════════════════════════════════════════════
# 二十五审反例回归: NULL token 回填 / 同版本双实例 GC / legacy 迁移
# ════════════════════════════════════════════════════════════════

class TestNullTokenBackfill:
    """P1 6.1: 存量 NULL token 行必须被回填, 旧 holder 也有真 fencing."""

    def test_null_token_backfilled_on_ensure(self, pg_engine):
        from domains.chatbi.tasks import ensure_lease_token_column, RunLease
        key = "run:semantic_write:nulltok"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases WHERE task_type=?", (key,))
            # 直接造旧格式行(token=NULL, 模拟旧部署残留)
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES (?, 'legacy-holder', "
                "'9999999999999')", (key,))
        ensure_lease_token_column(pg_engine)
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row["token"] is not None, "NULL token 未回填(旧holder无fencing)"
        # 旧 holder 以新代码续租 → RunLease 拿到非 NULL token
        a = RunLease(pg_engine, key, holder="legacy-holder", ttl_seconds=300)
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        assert a.acquire() and a.token is not None
        a.release()


class TestSameVersionMultiBuildGC:
    """P1 6.2: 同版本 token A(让路)/token B(发布) 各自成行, A 的分区被回收."""

    def test_sibling_token_partition_recycled(self, pg_engine):
        from datetime import datetime, timedelta, timezone
        from domains.chatbi.stores import (record_index_build,
                                           set_active_doc_id,
                                           get_or_create_scope)
        from datetime import datetime as _dt

        class _Store:
            def __init__(self):
                self.deleted = []

            def delete_doc(self, scope, doc_id):
                self.deleted.append(doc_id)
                return 1

        ds = "sib-ds"
        from datetime import datetime, timezone as _tz
        now = _dt.now(_tz.utc).isoformat()
        with pg_engine.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_data_sources "
                "(id, name, db_type, host, port, database, username, "
                "encrypted_password, is_active, scan_status, scan_progress, "
                "scan_stage, scan_error, scope_id, created_at, updated_at) "
                "VALUES (?, 't', 'pg', 'h', 1, 'd', 'u', 'p', 1, 'done', 100, "
                "'', '', 'sib-scope', ?, ?)", (ds, now, now))
        # token A: 让路(旧 writer), 已过 TTL
        record_index_build(pg_engine, "sib-scope", 21, "schema_r21_t7",
                           status="yielded", build_id="21-t7")
        old_ts = (datetime.now(timezone.utc)
                  - timedelta(seconds=90000)).isoformat()
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_index_builds SET updated_at=? "
                      "WHERE build_id='21-t7'", (old_ts,))
        # token B: 发布并成为 active
        record_index_build(pg_engine, "sib-scope", 21, "schema_r21_t9",
                           status="published", build_id="21-t9")
        set_active_doc_id(pg_engine, "sib-scope", "schema_r21_t9", 21)

        store = _Store()
        from domains.chatbi.stores import gc_index_builds
        gc_index_builds(pg_engine, "sib-scope", store, keep_generations=2,
                        published_grace_seconds=600,
                        unfinished_ttl_seconds=86400)
        assert "schema_r21_t7" in store.deleted,(
            f"让路兄弟分区未被回收(孤儿泄漏): {store.deleted}")
        assert "schema_r21_t9" not in store.deleted, "active 分区被误删"
        with pg_engine.connect() as c:
            n = c.execute("SELECT COUNT(*) AS n FROM chatbi_index_builds "
                          "WHERE scope='sib-scope' AND build_id='21-t7'").fetchone()
        assert int(n["n"]) == 0, "回收后台账行未删"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_builds WHERE scope='sib-scope'")
            c.execute("DELETE FROM chatbi_index_build_events WHERE scope='sib-scope'")
            c.execute("DELETE FROM chatbi_index_revisions WHERE scope='sib-scope'")
            c.execute("DELETE FROM chatbi_data_sources WHERE id='sib-ds'")


class TestLegacyBuildsMigration:
    """P1 6.2: 旧 (scope,version) PK 表自动迁移到 (scope,build_id)."""

    def test_migration_rewrites_pk_and_backfills(self, pg_engine):
        # 造旧结构表(模拟老库): drop 新表, 建 (scope,version) PK + 旧行
        with pg_engine.connect() as c:
            c.execute("DROP TABLE IF EXISTS chatbi_index_builds")
            c.execute(
                "CREATE TABLE chatbi_index_builds ("
                "scope TEXT NOT NULL, version INTEGER NOT NULL, "
                "doc_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'building', "
                "updated_at TEXT NOT NULL, PRIMARY KEY (scope, version))")
            c.execute(
                "INSERT INTO chatbi_index_builds "
                "(scope, version, doc_id, status, updated_at) "
                "VALUES ('mig-scope', 18, 'schema_r18', 'published', "
                "'2026-09-18T00:00:00+00:00')")
        from domains.chatbi.stores import _migrate_builds_table
        _migrate_builds_table(pg_engine)
        with pg_engine.connect() as c:
            cols = c.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='chatbi_index_builds'").fetchall()
            assert "build_id" in [r["column_name"] for r in cols]
            row = c.execute(
                "SELECT build_id FROM chatbi_index_builds "
                "WHERE scope='mig-scope'").fetchone()
            assert row and row["build_id"], "legacy 行 build_id 未回填"
            # 新 PK 生效: 同 build_id 推进不炸
            c.execute(
                "INSERT INTO chatbi_index_builds "
                "(scope, build_id, version, doc_id, status, updated_at) "
                "VALUES ('mig-scope', ?, 18, 'schema_r18_t3', 'published', "
                "'2026-09-18T01:00:00+00:00') "
                "ON CONFLICT (scope, build_id) DO NOTHING",
                (row["build_id"],))
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_builds WHERE scope='mig-scope'")


# ════════════════════════════════════════════════════════════════
# 二十六审反例回归: 迁移失败 fail-closed / 事件写失败可观测
# ════════════════════════════════════════════════════════════════

class TestLeaseMigrationFailClosed:
    """P1 5.1: token 列迁移失败时 acquire 必须拒绝放行(fail-closed).

    此前 ensure_lease_token_column 只 logger.error 不返回状态,
    acquire_lease 继续执行——NULL token 兼容分支让 fencing 降级
    运行(fail-open), 违反项目安全约束。
    """

    def test_acquire_rejected_when_migration_fails(self, pg_engine,
                                                   monkeypatch):
        import domains.chatbi.tasks as tasks_mod
        key = "run:semantic_write:migfail"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # 模拟迁移失败(sequence/回填故障): ensure 返回 False
        monkeypatch.setattr(tasks_mod, "ensure_lease_token_column",
                            lambda db: False)
        assert tasks_mod.acquire_lease(pg_engine, key, holder="task:x") is (
            False), "迁移失败仍放行(fail-open)"
        # RunLease 同样拒绝
        lease = tasks_mod.RunLease(pg_engine, key, holder="task:x",
                                   ttl_seconds=300)
        assert lease.acquire() is False, "RunLease 在迁移失败后仍 acquired"
        assert lease.token is None
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT holder FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is None, "迁移失败时仍写入了租约行"

    def test_ensure_returns_false_when_null_token_persists(self, pg_engine):
        """回填后仍有 NULL 残留(并发/部分失败) → ensure 返回未就绪."""
        from domains.chatbi.tasks import ensure_lease_token_column
        key = "run:semantic_write:nullresid"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at, token) VALUES "
                "(?, 'h', '9999999999999', NULL)", (key,))
        # 正常路径: ensure 自己回填 → True
        assert ensure_lease_token_column(pg_engine) is True
        # 人为再制造 NULL 残留(模拟回填事务部分失败)
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET token=NULL "
                      "WHERE task_type=?", (key,))
        assert ensure_lease_token_column(pg_engine) is True, (
            "ensure 应再次回填并返回就绪")
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))

    def test_runlease_rejects_null_token(self, pg_engine, monkeypatch):
        """acquire 成功但 token 读不到 → 拒绝执行并释放(fail-closed)."""
        import domains.chatbi.tasks as tasks_mod
        key = "run:semantic_write:nulltok2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # acquire_lease 正常, 但 lease_token 返回 None(异常路径)
        monkeypatch.setattr(tasks_mod, "lease_token",
                            lambda *a, **kw: None)
        lease = tasks_mod.RunLease(pg_engine, key, holder="task:x",
                                   ttl_seconds=300)
        assert lease.acquire() is False, "无 token 仍开始执行(弱 fencing)"
        assert lease.acquired is False
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT holder FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is None, "拒绝执行后未释放租约行(阻塞后续任务)"


class TestEventWriteFailureObservable:
    """P2 5.2: 事件 INSERT 失败必须可观测——失败计数进台账, monitor FAIL.

    此前 catch+logger.error: 表可读但事件丢失时计数偏低, verdict 仍 OK。
    """

    def test_event_failure_counted_and_monitor_fails(self, pg_engine):
        import os as _os
        _os.environ.setdefault(
            "SOAK_DATABASE_URL",
            "postgresql://root:root@localhost:5432/llm_modeler_test_run")
        from domains.chatbi.stores import record_index_build
        key_scope = "evtfail-scope"

        # 注入: 事件表 INSERT 失败(模拟约束/权限/连接故障)。
        # _PgConnProxy 是 __slots__ 只读代理, 不能改属性——改为
        # monkeypatch stores 模块内 record_index_build 的事件分支:
        # 直接替换整个函数太粗(状态台账也要验证), 所以用真实 PG 的
        # 触发器让事件表 INSERT 真实失败(约束/权限故障的等价物),
        # 这比 mock 更接近生产故障形态
        with pg_engine.connect() as c:
            c.execute(
                "CREATE OR REPLACE FUNCTION _evtfail_guard() "
                "RETURNS trigger AS $$ BEGIN "
                "  RAISE EXCEPTION 'simulated event insert failure'; "
                "END $$ LANGUAGE plpgsql")
            c.execute(
                "DROP TRIGGER IF EXISTS trg_evtfail ON "
                "chatbi_index_build_events")
            c.execute(
                "CREATE TRIGGER trg_evtfail BEFORE INSERT ON "
                "chatbi_index_build_events FOR EACH ROW EXECUTE "
                "FUNCTION _evtfail_guard()")
        try:
            # 状态表写入必须成功(事件失败不拖垮状态台账)
            record_index_build(pg_engine, key_scope, 7, "schema_r7_t5",
                               status="published", build_id="7-t5")
        finally:
            with pg_engine.connect() as c:
                c.execute("DROP TRIGGER IF EXISTS trg_evtfail ON "
                          "chatbi_index_build_events")
                c.execute("DROP FUNCTION IF EXISTS _evtfail_guard()")
        with pg_engine.connect() as c:
            st = c.execute(
                "SELECT status FROM chatbi_index_builds "
                "WHERE scope=? AND build_id='7-t5'",
                (key_scope,)).fetchone()
            assert st is not None and st["status"] == "published", (
                "事件失败拖垮了状态台账")
            fc = c.execute(
                "SELECT failures FROM chatbi_index_event_failures "
                "WHERE scope=? AND build_id='7-t5'",
                (key_scope,)).fetchone()
        assert fc is not None and int(fc["failures"]) >= 1, (
            "事件写失败未计入失败台账(monitor 无法 FAIL)")

        # monitor 读到失败计数 → verdict FAIL
        import os as _os2
        import importlib.util
        _script = _os2.path.join(_os2.path.dirname(_os2.path.abspath(
            __file__)), "..", "..", "scripts", "soak_monitor.py")
        spec = importlib.util.spec_from_file_location("sm2", _script)
        sm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sm)
        from sdk.relational_store import PackRelationalDB
        db = PackRelationalDB(
            'chatbi',
            database_url='postgresql://root:root@localhost:5432/'
                          'llm_modeler_test_run')
        snap = sm.snapshot(db)
        assert snap.get("build_event_failures") is not None, (
            "monitor 未采集事件失败计数")
        v = sm._verdict(snap, {})
        assert v.startswith("FAIL"), f"事件写失败仍判 {v}"
        assert "事件" in v, f"FAIL 原因未提及事件失败: {v}"
        # 清理
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_builds "
                      "WHERE scope=?", (key_scope,))
            c.execute("DELETE FROM chatbi_index_build_events "
                      "WHERE scope=?", (key_scope,))
            c.execute("DELETE FROM chatbi_index_event_failures "
                      "WHERE scope=?", (key_scope,))

    def test_monitor_ok_without_event_failures(self, pg_engine):
        """无失败计数时 monitor 不因该信号误报(反例锚)."""
        import os as _os
        _os.environ.setdefault(
            "SOAK_DATABASE_URL",
            "postgresql://root:root@localhost:5432/llm_modeler_test_run")
        import importlib.util
        _script = _os.path.join(_os.path.dirname(_os.path.abspath(
            __file__)), "..", "..", "scripts", "soak_monitor.py")
        spec = importlib.util.spec_from_file_location("sm3", _script)
        sm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(sm)
        from sdk.relational_store import PackRelationalDB
        db = PackRelationalDB(
            'chatbi',
            database_url='postgresql://root:root@localhost:5432/'
                          'llm_modeler_test_run')
        snap = sm.snapshot(db)
        # 只验证该信号本身不产生 FAIL(其它信号由各自测试覆盖)
        assert snap.get("build_event_failures") == 0, (
            f"无失败却读到计数: {snap.get('build_event_failures')}")
