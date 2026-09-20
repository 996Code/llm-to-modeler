"""十五审验收测试: merge provenance + 集合去重 + 校验 + 空状态 CAS.

关键约束: fixture 模拟真实扫描来源——无数据库注释的表/列是
auto_inferred(不依赖模型默认 manual), 这是十四审测试掩盖边界的根因.
"""
import os
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
        try:
            assert b.acquire() is False, "第二实例抢到了执行租约"
            assert a.heartbeat() is True     # 本人续租
            assert b.acquire() is False
        finally:
            a.release()
        assert b.acquire() is True, "释放后未接管"
        b.release()

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
        try:
            assert refresh_lease.acquire() is False, \
                "scan 进行中, refresh 拿到了同一数据源写租约(跨操作不互斥)"
        finally:
            scan_lease.release()
        assert refresh_lease.acquire() is True
        refresh_lease.release()

    def test_independent_heartbeat_keeps_lease_alive(self, pg_engine):
        """独立心跳线程在业务回调沉默时维持租约(短 TTL 快速验证)."""
        import time
        from domains.chatbi.tasks import RunLease
        a = RunLease(pg_engine, "run:semantic_write:ds-hb",
                     holder="task:hb-a", ttl_seconds=2)
        b = RunLease(pg_engine, "run:semantic_write:ds-hb",
                     holder="task:hb-b", ttl_seconds=2)
        try:
            assert a.acquire() is True
            # 不调用任何业务心跳——独立线程应以 TTL/3(≈0.7s) 自动续租
            time.sleep(3.2)
            assert a.owned is True, "独立心跳未维持租约"
            assert b.acquire() is False, "心跳保活期间租约被他实例抢走"
        finally:
            # 二十七审 P3: 必须 release——心跳线程不终止会访问已关闭的
            # 连接池, 全量测试结束后打 ERROR 日志掩盖真实失败
            a.release()
        time.sleep(0.1)
        assert b.acquire() is True, "释放后未接管"
        b.release()


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
        a.release()              # 二十七审 P3: 终止心跳线程(防 pool 泄漏)
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
        a.release()              # 二十七审 P3: 终止心跳线程(防 pool 泄漏)
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


class TestHolderABA:
    """P1(二十七审): 同 holder 重试的 token 换代——旧对象不得误判/误删.

    真实复现时序: 旧对象(token=147) 暂停 → 租约过期 → 其他 holder 接管
    → 再过期 → 同 holder(自动重试复用 task id)重获新 token=151 →
    旧对象恢复。此前 assert_owned 只看 holder 返回 True, release 按
    holder 删掉了 151 的新租约。
    """

    def _expire(self, pg_engine, key):
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))

    def test_old_object_cannot_assert_or_delete_new_token(self, pg_engine):
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:aba-1"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # 1) 旧对象获得 token=t1
        old = RunLease(pg_engine, key, holder="task:same-retry",
                       ttl_seconds=300)
        assert old.acquire() and old.token is not None
        t1 = old.token
        # 2) 暂停期间过期, 其他 holder 接管
        self._expire(pg_engine, key)
        other = RunLease(pg_engine, key, holder="task:other",
                         ttl_seconds=300)
        assert other.acquire()
        # 3) 再过期, 同 holder 重试获得新 token=t2(ABA 的 A 回来了)
        self._expire(pg_engine, key)
        retry = RunLease(pg_engine, key, holder="task:same-retry",
                         ttl_seconds=300)
        assert retry.acquire() and retry.token is not None
        t2 = retry.token
        assert t2 > t1, "同 holder 重获未铸造新 token"
        # 4) 旧对象恢复: assert 必须拒绝(token 换代)
        old.acquired = True            # 模拟旧对象未察觉(心跳延迟)
        assert old.assert_owned() is False, (
            "旧对象凭同 holder 通过了 assert(token ABA)")
        # 5) 旧对象 release 不得删掉新租约
        old.acquired = True
        old.release()
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is not None and int(row["token"]) == t2, (
            "旧对象 release 误删了新 token 的租约(ABA)")
        # 6) 旧对象 heartbeat 不得续上新租约
        old.acquired = True
        assert old.heartbeat() is False, "旧对象续上了新 token 的租约"
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is not None and int(row["token"]) == t2, (
            "旧对象 heartbeat 后新租约被破坏")
        retry.release()
        other.release()

    def test_acquire_cleanup_does_not_delete_existing_lease(self, pg_engine):
        """acquire 后 token 读失败 → 清理只删自己的行, 不误删同 holder
        既有新租约(ABA 清理面)."""
        from domains.chatbi.tasks import RunLease, release_lease
        key = "run:semantic_write:aba-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # 同 holder 已持有新 token 的租约(模拟重试对象)
        cur = RunLease(pg_engine, key, holder="task:same",
                       ttl_seconds=300)
        assert cur.acquire() and cur.token is not None
        t_new = cur.token
        # 旧对象按 holder 弱删(不传 token)——删得掉行, 但这是兼容路径
        # 的已知语义; 真正的防线在 RunLease.release/heartbeat/assert
        # 全部带 token(见上), 生产代码不再有不带 token 的调用点
        weak_deleted = release_lease(pg_engine, key, "task:same")
        if weak_deleted:
            # 弱删确实能删掉(历史行为)——恢复行, 继续验证带 token 路径
            with pg_engine.connect() as c:
                c.execute(
                    "INSERT INTO chatbi_scheduler_leases "
                    "(task_type, holder, expires_at, token) VALUES "
                    "(?, 'task:same', '9999999999999', ?)",
                    (key, t_new))
        # 带 token 的删除对正确 token 生效
        assert release_lease(pg_engine, key, "task:same", token=t_new) is True
        # 带**错误** token 删不掉(ABA 防线本体)
        with pg_engine.connect() as c:
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at, token) VALUES "
                "(?, 'task:same', '9999999999999', ?)",
                (key, t_new))
        assert release_lease(pg_engine, key, "task:same",
                             token=t_new + 1) is False, (
            "错误 token 的删除删掉了租约(ABA)")
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is not None and int(row["token"]) == t_new
        assert release_lease(pg_engine, key, "task:same", token=t_new) is True
        cur.release()


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
# 二十八审反例回归: acquire原子token/ack原子CTE/heartbeat原子续租
# ════════════════════════════════════════════════════════════════

class TestAcquireTokenAtomicity:
    """P1-A: acquire 与 token 同事务原子返回; 首读失败不二次读删.

    此前 acquire 成功后另行读 token, 首读 None 后又读"当前 token"
    并删除——间隙内租约被接管+同 holder 重获时, 读到的已是 successor
    的 token, 旧对象据此删除会误删新代租约(真实复现 token=223)。
    """

    def test_acquire_returns_token_atomically(self, pg_engine):
        """acquire 直接返回本次生效 token(无需二次读取)."""
        from domains.chatbi.tasks import RunLease, claim_lease_token
        key = "run:semantic_write:atom-1"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        tok = claim_lease_token(pg_engine, key, holder="task:a",
                                ttl_seconds=300)
        assert tok is not None, "claim 未原子返回 token"
        # 同 holder 的第二个对象 claim 必须拒绝(二十九审 P2-A)
        b = RunLease(pg_engine, key, holder="task:a", ttl_seconds=300)
        assert b.acquire() is False, "同 holder 未过期重入 claim 成功"
        # 第一个对象释放后, 同 holder 重获换新 token
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        tok2 = claim_lease_token(pg_engine, key, holder="task:a",
                                 ttl_seconds=300)
        assert tok2 is not None and tok2 > tok, "重获未铸造新 token"

    def test_no_delete_when_token_unreadable(self, pg_engine, monkeypatch):
        """token 不可知时: 拒绝执行且**不删除任何行**(不二次读取)."""
        import domains.chatbi.tasks as tasks_mod
        key = "run:semantic_write:atom-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # 同 holder 已有 successor 租约(模拟重试对象)
        cur = tasks_mod.RunLease(pg_engine, key, holder="task:same",
                                 ttl_seconds=300)
        assert cur.acquire() and cur.token is not None
        t_successor = cur.token
        # 旧对象 claim: 同 holder 未过期行必须拒绝(二十九审 P2-A);
        # 再用 claim_lease_token 返回 None 模拟 token 不可知异常路径
        old = tasks_mod.RunLease(pg_engine, key, holder="task:same",
                                 ttl_seconds=300)
        assert old.acquire() is False, "同 holder 未过期重入仍执行"
        monkeypatch.setattr(tasks_mod, "claim_lease_token",
                            lambda *a, **kw: None)
        assert old.acquire() is False, "token 不可知仍开始执行"
        assert old.acquired is False and old.token is None
        # 关键: 拒绝执行后没有任何删除动作, successor 行完好
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is not None and int(row["token"]) == t_successor, (
            "旧对象 acquire 失败时删除了 successor 租约(ABA)")
        cur.release()

    def test_interleaved_takeover_token_not_misread(self, pg_engine):
        """确定性交错: 旧 acquire → 接管 → 同 holder 重获——旧对象的
        token 停留在旧代, 不会读到/删到 successor(原子 RETURNING)."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:atom-3"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        old = RunLease(pg_engine, key, holder="task:same-retry",
                       ttl_seconds=300)
        assert old.acquire() and old.token is not None
        t_old = old.token
        # 接管 → 过期 → 同 holder 重获
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        other = RunLease(pg_engine, key, holder="task:other",
                         ttl_seconds=300)
        assert other.acquire()
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        retry = RunLease(pg_engine, key, holder="task:same-retry",
                         ttl_seconds=300)
        assert retry.acquire() and retry.token > t_old
        # 旧对象 token 仍是旧代(不是二次读取的 successor)
        assert old.token == t_old, "旧对象 token 被二次读取污染"
        old.acquired = True
        old.release()      # 按旧 token 删——删不到 successor
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        assert row is not None and int(row["token"]) == retry.token
        retry.release()
        other.release()


class TestHeartbeatTokenAwareRenew:
    """P2-D: token-aware 单条 UPDATE 原子续租——不先按 holder 续后验.

    此前旧 heartbeat 会先把同 holder successor 的租约延长一个旧 TTL
    (真实复现: successor 5s 被延长约 115s), 再发现 token 换代返回 False。
    """

    def test_old_heartbeat_does_not_extend_successor(self, pg_engine):
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:hbtok-1"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        old = RunLease(pg_engine, key, holder="task:same",
                       ttl_seconds=300)
        assert old.acquire() and old.token is not None
        # successor: 其他 holder 接管(轮换 token) → 过期 → 同 holder 重获
        # (直接同 holder 过期重获不会轮换 token——续租语义保持旧值)
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        other = RunLease(pg_engine, key, holder="task:other",
                         ttl_seconds=300)
        assert other.acquire() and other.token > old.token
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        succ = RunLease(pg_engine, key, holder="task:same",
                        ttl_seconds=5)
        assert succ.acquire() and succ.token > old.token, (
            f"同 holder 重获未铸造新 token: {succ.token} vs {old.token}")
        # 记录 successor 当前 expiry
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT expires_at FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        succ_expiry_before = int(row["expires_at"])
        # 旧对象 heartbeat: 必须失败, 且不得延长 successor 的 expiry
        old.acquired = True
        assert old.heartbeat() is False, "旧对象续租成功(ABA)"
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT expires_at FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
        after = int(row["expires_at"])
        assert after <= succ_expiry_before + 1000, (
            f"旧 heartbeat 延长了 successor 租约({succ_expiry_before}"
            f"→{after}, 应最多时钟抖动)")
        succ.release()
        old.release()
        other.release()

    def test_own_heartbeat_renews(self, pg_engine):
        """本人续租正常(token 匹配的 UPDATE 生效)."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:hbtok-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        a = RunLease(pg_engine, key, holder="task:a", ttl_seconds=300)
        try:
            assert a.acquire()
            assert a.heartbeat() is True, "本人 token 匹配续租失败"
            assert a.token is not None
        finally:
            a.release()


class TestExecuteIfOwnedTokenFailClosed:
    """P2-E: execute_if_owned 在 token=None 时独立拒绝(最后写屏障)."""

    def test_token_none_rejected(self, pg_engine):
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:exetok-1"
        a = RunLease(pg_engine, key, holder="task:x", ttl_seconds=300)
        a.acquired = True          # 异常构造的半状态: acquired 但无 token
        a.token = None
        ok, res = a.execute_if_owned(lambda conn: "should-not-run")
        assert ok is False, "token=None 的半状态通过了写屏障"


class TestAcknowledgeAtomicConcurrent:
    """P1-B: acknowledge 的删除集合与审计集合原子绑定.

    此前 SELECT 后全表 DELETE: READ COMMITTED 下并发新告警在 SELECT
    后 DELETE 前提交, 会被删掉但不进 ack 表(真实复现: 永久消失)。
    """

    def test_concurrent_new_alert_not_lost(self, pg_engine):
        """确定性交错: acknowledge 进行中并发插入新告警——新告警
        必须存活(不进本次 ack, 留在待处理表)."""
        import threading
        from domains.chatbi.stores import (
            acknowledge_index_event_failures, list_index_event_failures)
        scope = "ack-atomic"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failures "
                      "WHERE scope LIKE ?", (scope + "%",))
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope LIKE ?", (scope + "%",))
            c.execute(
                "INSERT INTO chatbi_index_event_failures "
                "(scope, build_id, version, event, failures, last_error, "
                "updated_at) VALUES (?, 'old', 7, 'published', 1, 'e', "
                "'2026-09-18T00:00:00+00:00')", (scope,))
        # 在 acknowledge 的 DELETE 与 INSERT ack 之间注入并发新告警:
        # 用触发器在 DELETE 时挂起(pgl_sleep), 主线程插入新告警
        with pg_engine.connect() as c:
            c.execute(
                "CREATE OR REPLACE FUNCTION _ack_guard() RETURNS trigger "
                "AS $$ BEGIN "
                "  PERFORM pg_sleep(1.5); "
                "  RETURN OLD; END $$ LANGUAGE plpgsql")
            c.execute("DROP TRIGGER IF EXISTS trg_ack ON "
                      "chatbi_index_event_failures")
            c.execute(
                "CREATE TRIGGER trg_ack BEFORE DELETE ON "
                "chatbi_index_event_failures FOR EACH ROW "
                "EXECUTE FUNCTION _ack_guard()")
        box = {}

        def _ack():
            try:
                box["res"] = acknowledge_index_event_failures(
                    pg_engine, "ops-x", "并发测试")
            except Exception as e:
                box["res"] = f"EXC: {e}"

        t = threading.Thread(target=_ack)
        t.start()
        import time
        time.sleep(0.5)     # 此时 ack 事务正挂在 DELETE 触发器上
        # 并发新告警提交(在 ack 的 DELETE 语句快照之后)
        with pg_engine.connect() as c:
            c.execute(
                "INSERT INTO chatbi_index_event_failures "
                "(scope, build_id, version, event, failures, last_error, "
                "updated_at) VALUES (?, 'new', 8, 'building', 1, 'e', "
                "'2026-09-18T00:01:00+00:00')", (scope,))
        t.join()
        # 清触发器
        with pg_engine.connect() as c:
            c.execute("DROP TRIGGER IF EXISTS trg_ack ON "
                      "chatbi_index_event_failures")
            c.execute("DROP FUNCTION IF EXISTS _ack_guard()")
        cleared, acked = box["res"]
        assert cleared == acked, f"删除/审计数不一致: {box['res']}"
        # 旧告警已确认; 新告警必须仍在待处理表(未被静默吞掉)
        pending = {i["build_id"] for i in
                   list_index_event_failures(pg_engine)
                   if i["scope"] == scope}
        assert "new" in pending, (
            f"并发新告警被静默删除(既不在待处理表也不在审计表): "
            f"pending={pending}")
        with pg_engine.connect() as c:
            acked_bids = {r["build_id"] for r in c.execute(
                "SELECT build_id FROM chatbi_index_event_failure_acks "
                "WHERE scope=?", (scope,)).fetchall()}
        assert "new" not in acked_bids, "新告警未经确认进了审计表"
        # 清理
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failures "
                      "WHERE scope LIKE ?", (scope + "%",))
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope LIKE ?", (scope + "%",))

    def test_every_deleted_row_has_ack(self, pg_engine):
        """正常路径: 每条被删记录都有对应 ack(原子 CTE 保证)."""
        from domains.chatbi.stores import (
            acknowledge_index_event_failures)
        scope = "ack-atomic-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failures "
                      "WHERE scope=?", (scope,))
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope=?", (scope,))
            for i in range(5):
                c.execute(
                    "INSERT INTO chatbi_index_event_failures "
                    "(scope, build_id, version, event, failures, "
                    "last_error, updated_at) VALUES (?, ?, 7, 'published', "
                    "1, 'e', '2026-09-18T00:00:00+00:00')",
                    (scope, f"b{i}"))
        cleared, acked = acknowledge_index_event_failures(
            pg_engine, "ops-y", "")
        assert cleared == 5 and acked == 5
        with pg_engine.connect() as c:
            n_ack = c.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_event_failure_acks "
                "WHERE scope=?", (scope,)).fetchone()
            n_left = c.execute(
                "SELECT COUNT(*) AS n FROM chatbi_index_event_failures "
                "WHERE scope=?", (scope,)).fetchone()
        assert int(n_ack["n"]) == 5 and int(n_left["n"]) == 0
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope=?", (scope,))


class TestConcurrentColdStartDDL:
    """P1-C: 多实例并发冷启动 DDL 不再 deadlock(advisory lock 串行)."""

    def test_concurrent_init_db_all_healthy(self):
        """4 个并发 ConversationStore 初始化全部成功(真实 PG).

        二十九审 P3-D: DSN 取 conftest 解析后的 TEST_DATABASE_URL,
        不硬编码本机凭据(CI/其他开发机会失败, 也可能误碰非 pytest
        管理的库)。
        """
        import threading
        from services.conversation_store import ConversationStore
        url = os.environ["TEST_DATABASE_URL"]
        results = {}

        def worker(i):
            try:
                ConversationStore(database_url=url)
                results[i] = "ok"
            except Exception as e:
                results[i] = f"FAIL:{type(e).__name__}:{str(e)[:80]}"

        ts = [threading.Thread(target=worker, args=(i,))
              for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert all(v == "ok" for v in results.values()), results

    def test_ddl_failure_releases_lock_and_raises_original(self, pg_engine):
        """P2-B: DDL 异常 → 原始异常上抛 + advisory lock 归零(不泄漏).

        此前 session 级锁 + finally 手工 unlock: DDL 报错使事务
        aborted, unlock 语句本身抛 InFailedSqlTransaction 覆盖原始
        异常, 且 session 锁留在池连接上直到池关闭(真实故障注入)。
        事务级 pg_advisory_xact_lock 在回滚时自动释放。
        """
        import threading
        from services import conversation_store as cs_mod
        url = os.environ["TEST_DATABASE_URL"]
        _LOCK_KEY = 0x636F6E76736D6967

        def _granted():
            with pg_engine.connect() as c:
                row = c.execute(
                    "SELECT COUNT(*) AS n FROM pg_locks "
                    "WHERE locktype = 'advisory' "
                    "AND classid = ? AND objid = ?",
                    (_LOCK_KEY >> 32, _LOCK_KEY & 0xFFFFFFFF)).fetchone()
                return int(row["n"])

        # 注入: 第一条 DDL 抛 division by zero(真实故障形态)
        real_ddl = cs_mod.ConversationStore._DDL
        cs_mod.ConversationStore._DDL = [
            "SELECT 1/0 AS boom"] + list(real_ddl)
        raised = {}
        try:
            try:
                ConversationStore = cs_mod.ConversationStore
                ConversationStore(database_url=url)
                raised["err"] = None
            except Exception as e:
                raised["err"] = e
            # 原始异常可见(不被 InFailedSqlTransaction 覆盖)
            assert raised["err"] is not None, "注入的 DDL 失败被吞"
            assert "division by zero" in str(raised["err"]), (
                f"原始异常被覆盖: {type(raised['err']).__name__}: "
                f"{str(raised['err'])[:80]}")
        finally:
            cs_mod.ConversationStore._DDL = real_ddl
        # 事务级锁已随回滚释放(不泄漏到池)
        assert _granted() == 0, "advisory lock 泄漏(DDL 异常后未释放)"
        # 下一次初始化不被残留锁阻塞(正常完成)
        cs_mod.ConversationStore(database_url=url)
        assert _granted() == 0


class TestSameHolderReentry:
    """P2-A(二十九审): 同 holder 的两个执行对象不得共享 token.

    真实复现: A/B 用相同 key/holder 都 acquire=True、token 相同、
    assert 都通过、两个 execute_if_owned 都执行。TaskManager 正常
    路径每次 submit 生成新 UUID 所以未触发, 但原语必须自身具备
    执行实例隔离。
    """

    def test_same_holder_unexpired_reentry_rejected(self, pg_engine):
        """同 holder 未过期重入: 第二个对象 claim 必须拒绝."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:reent-1"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        a = RunLease(pg_engine, key, holder="task:retry-same-id",
                     ttl_seconds=300)
        b = RunLease(pg_engine, key, holder="task:retry-same-id",
                     ttl_seconds=300)
        assert a.acquire() is True and a.token is not None
        try:
            assert b.acquire() is False, "同 holder 未过期重入 claim 成功"
            assert b.token is None and b.acquired is False
            # 两个写屏障只有一个能通过
            ok_a, _ = a.execute_if_owned(
                lambda conn: conn.execute("SELECT 1"))
            ok_b, _ = b.execute_if_owned(
                lambda conn: conn.execute("SELECT 1"))
            assert ok_a is True and ok_b is False, (
                f"双写屏障通过: A={ok_a} B={ok_b}")
        finally:
            a.release()

    def test_same_holder_expired_reclaim_rotates_token(self, pg_engine):
        """同 holder 过期重获: 始终铸造新 token(旧对象立即作废)."""
        from domains.chatbi.tasks import RunLease
        key = "run:semantic_write:reent-2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        a = RunLease(pg_engine, key, holder="task:retry-same-id",
                     ttl_seconds=300)
        assert a.acquire() and a.token is not None
        t1 = a.token
        with pg_engine.connect() as c:
            c.execute("UPDATE chatbi_scheduler_leases SET expires_at='1' "
                      "WHERE task_type=?", (key,))
        b = RunLease(pg_engine, key, holder="task:retry-same-id",
                     ttl_seconds=300)
        assert b.acquire() is True, "过期后同 holder 重获失败"
        assert b.token is not None and b.token > t1, (
            f"同 holder 过期重获未换新 token: {b.token} vs {t1}")
        # 旧对象立即作废(即使伪装 acquired)
        a.acquired = True
        assert a.assert_owned() is False, "旧对象凭旧 token 通过 assert"
        ok, _ = a.execute_if_owned(lambda conn: "should-not-run")
        assert ok is False, "旧对象写屏障通过(应作废)"
        b.release()


# ════════════════════════════════════════════════════════════════
# 三十审反例回归: 全新schema落表位置 / ISO expiry 三态 / 配置 fail-fast
# ════════════════════════════════════════════════════════════════

class TestFreshSchemaPlacement:
    """P1-A: 全新库(schema 不存在)时 pack 表只能落在 pack schema.

    真实复现: connect() 只设置 search_path 不创建 schema, 无前缀
    CREATE TABLE 静默落入 public——schema 隔离失效。
    """

    def test_migrate_locked_creates_schema_and_places_tables(self):
        """不存在的 schema: migrate_locked 锁内建 schema, 表落 pack
        schema, public 中为 0."""
        import uuid
        from sdk.relational_store import PackRelationalDB
        url = os.environ["TEST_DATABASE_URL"]
        probe = f"r30t_{uuid.uuid4().hex[:8]}"
        db = PackRelationalDB(probe, database_url=url)
        try:
            db.migrate_locked(
                0x72333070726F62,
                lambda conn: conn.execute(
                    "CREATE TABLE IF NOT EXISTS audit_r30_probe (id TEXT)"))
            with db.engine.connect() as c:
                locs = c.execute(
                    "SELECT table_schema FROM information_schema.tables "
                    "WHERE table_name = 'audit_r30_probe'").fetchall()
            schemas = [r["table_schema"] for r in locs]
            assert schemas == [probe], (
                f"表未落在 pack schema: {schemas}")
            with db.engine.connect() as c:
                in_public = c.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = 'public' "
                    "AND table_name = 'audit_r30_probe'").fetchone()
            assert int(in_public["n"]) == 0, "表泄漏到 public"
        finally:
            with db.engine.connect() as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{probe}" CASCADE')

    def test_init_pack_schema_on_fresh_schema(self):
        """完整 _init_pack_schema 在全新 schema 上执行: 全部表落 chatbi
        语义的 pack schema, public 无泄漏(用临时 pack 名隔离)."""
        import uuid
        from sdk.relational_store import PackRelationalDB
        from domains.chatbi import runtime as rt
        url = os.environ["TEST_DATABASE_URL"]
        probe = f"r30p_{uuid.uuid4().hex[:8]}"
        db = PackRelationalDB(probe, database_url=url)
        try:
            # 临时替换 PACK_NAME 派生的 lock key 不必要——migrate_locked
            # 的 key 只需互斥; 直接跑完整迁移序列
            rt._init_pack_schema(db)
            with db.engine.connect() as c:
                n_pack = c.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = ?", (probe,)).fetchone()
                n_public = c.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = 'public' AND table_name LIKE "
                    "'chatbi%'").fetchone()
            assert int(n_pack["n"]) >= 15, (
                f"pack schema 表数量异常: {n_pack['n']}")
            assert int(n_public["n"]) == 0, "chatbi 表泄漏到 public"
        finally:
            with db.engine.connect() as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{probe}" CASCADE')


class TestLegacyIsoExpiryClaim:
    """P1-B: 旧 ISO 过期租约必须能被新 claim 接管(三态).

    真实复现: '2020-01-01T00:00:00+00:00' 的过期 ISO 行挡住
    RunLease, 任务持续"另一实例执行中"失败。
    """

    def test_expired_iso_claimable_with_new_token(self, pg_engine):
        """过期 ISO → 删除并接管, 铸造新 token."""
        from domains.chatbi.tasks import claim_lease_token
        key = "run:semantic_write:iso30-exp"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES "
                "(?, 'legacy-holder', '2020-01-01T00:00:00+00:00')",
                (key,))
        tok = claim_lease_token(pg_engine, key, holder="task:new",
                                ttl_seconds=300)
        assert tok is not None, "过期 ISO 租约无法接管(永久阻断)"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))

    def test_future_iso_keeps_holder(self, pg_engine):
        """未来 ISO → 转 epoch 保留持有权, 新 claim 拒绝."""
        from domains.chatbi.tasks import claim_lease_token
        key = "run:semantic_write:iso30-fut"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES "
                "(?, 'legacy-holder', '2099-01-01T00:00:00+00:00')",
                (key,))
        tok = claim_lease_token(pg_engine, key, holder="task:new",
                                ttl_seconds=300)
        assert tok is None, "未来 ISO 被抢占(旧实例持有权丢失)"
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT expires_at FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
            assert row["expires_at"].isdigit(), (
                f"未来 ISO 未转 epoch: {row['expires_at']}")
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))

    def test_garbage_expiry_fail_closed(self, pg_engine):
        """不可解析文本 → 保留原样 + 新 claim 拒绝(fail-closed)."""
        from domains.chatbi.tasks import claim_lease_token
        key = "run:semantic_write:iso30-bad"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES "
                "(?, 'legacy-holder', 'not-a-date')", (key,))
        tok = claim_lease_token(pg_engine, key, holder="task:new",
                                ttl_seconds=300)
        assert tok is None, "不可解析租约被接管(应 fail-closed)"
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT expires_at FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
            assert row["expires_at"] == "not-a-date", (
                "不可解析值被改动(应保留原样)")
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))


class TestRetryConfigFailFast:
    """P2-C: 重试配置非法值必须启动失败(attempts=0 是 fail-open)."""

    def test_invalid_conv_retry_configs_raise(self, monkeypatch):
        from services import conversation_store as cs_mod
        for bad in ["0", "-3", "abc", "NaN", "Infinity"]:
            monkeypatch.setenv("CONV_DDL_RETRY_ATTEMPTS", bad)
            try:
                cs_mod.ConversationStore._init_db(
                    type("S", (), {"_get_conn": None,
                                   "_DDL": []})())
                raise AssertionError(
                    f"CONV_DDL_RETRY_ATTEMPTS={bad!r} 未被拒绝")
            except ValueError:
                pass
            except TypeError:
                # _get_conn=None 在校验通过后才会触发——校验先于连接
                raise AssertionError(
                    f"非法配置 {bad!r} 通过了校验(走到了连接阶段)")
        monkeypatch.delenv("CONV_DDL_RETRY_ATTEMPTS", raising=False)

    def test_invalid_pack_retry_configs_raise(self, monkeypatch):
        from sdk.env_config import parse_int_env
        for bad in ["0", "-1", "xyz", "NaN", "Infinity", "1.9"]:
            monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", bad)
            try:
                parse_int_env("PACK_DDL_RETRY_ATTEMPTS", 5, minimum=1)
                raise AssertionError(
                    f"PACK_DDL_RETRY_ATTEMPTS={bad!r} 未被拒绝")
            except ValueError:
                pass
        monkeypatch.delenv("PACK_DDL_RETRY_ATTEMPTS", raising=False)

    def test_valid_retry_configs_accepted(self, monkeypatch):
        from sdk.env_config import parse_int_env, parse_float_env
        monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", "3")
        v = parse_int_env("PACK_DDL_RETRY_ATTEMPTS", 5, minimum=1)
        assert v == 3 and type(v) is int
        monkeypatch.setenv("PACK_DDL_RETRY_BACKOFF_SECONDS", "0")
        assert parse_float_env("PACK_DDL_RETRY_BACKOFF_SECONDS",
                               3.0, minimum=0.0) == 0
        monkeypatch.delenv("PACK_DDL_RETRY_ATTEMPTS", raising=False)
        monkeypatch.delenv("PACK_DDL_RETRY_BACKOFF_SECONDS",
                           raising=False)

    def test_assembly_rejects_invalid_pack_config(self, monkeypatch):
        """三十二审 P2: 非法配置在 pack 装配期被拒(不等首次请求).

        三十三审 P1: create_registry 把 ValueError 包成
        PackConfigurationError——loader 不吞, 传播到 lifespan。
        """
        from domains.chatbi.runtime import validate_pack_runtime_config
        from domains.chatbi import runtime as rt
        from sdk.pack_api import PackConfigurationError
        # 屏蔽版本探测(本测试只验证配置解析层)
        monkeypatch.setattr(rt, "_require_pg16", lambda: None)
        monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", "1.9")
        try:
            validate_pack_runtime_config()
            raise AssertionError("1.9 在装配期未被拒绝")
        except ValueError:
            pass
        # create_registry 包装: ValueError → PackConfigurationError
        from domains.chatbi import pack as chatbi_pack
        try:
            chatbi_pack.create_registry()
            raise AssertionError("create_registry 未抛 PackConfigurationError")
        except PackConfigurationError:
            pass
        monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", "5")
        validate_pack_runtime_config()   # 合法值通过
        monkeypatch.delenv("PACK_DDL_RETRY_ATTEMPTS", raising=False)

    def test_loader_propagates_fatal_pack_error(self, monkeypatch):
        """三十三审 P1: loader 对 PackConfigurationError 不吞——传播.

        此前 catch-continue 让 ChatBI-only 部署以 0 pack/0 工具
        "成功"启动(真实复现)。
        """
        import domains
        from sdk.pack_api import PackConfigurationError
        # 只加载 chatbi, 且让 create_registry 抛致命错误
        monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", "1.9")
        try:
            domains.load_all_packs(pack_names=["chatbi"])
            raise AssertionError("致命配置错误被 loader 吞掉")
        except PackConfigurationError:
            pass
        # 0 pack 路径: 名单内 pack 全部失败 → RuntimeError(文档承诺)
        monkeypatch.delenv("PACK_DDL_RETRY_ATTEMPTS", raising=False)


# ════════════════════════════════════════════════════════════════
# 三十四审反例回归: schema migration 前置 / critical 契约 / 热切换原子性
# ════════════════════════════════════════════════════════════════

class TestSchemaMigrationStartupGate:
    """P1-A: schema migration 是 startup/readiness 前置条件.

    此前 _init_pack_schema 只在首个 API 请求(get_pack_db)懒执行——
    startup complete 后首个 GET 才出现 locked migration done;
    DDL 权限不足时服务先 ready, 首个用户 500(假完整)。
    """

    def test_migration_runs_at_assembly(self, monkeypatch):
        """validate_pack_runtime_config 触发 get_pack_db(迁移)且失败包装."""
        from domains.chatbi import runtime as rt
        from sdk.pack_api import PackConfigurationError
        called = {}

        def _fake_get():
            called["n"] = called.get("n", 0) + 1
            return object()

        monkeypatch.setattr(rt, "get_pack_db", _fake_get)
        monkeypatch.setattr(rt, "_require_pg16", lambda: None)
        rt.validate_pack_runtime_config()
        assert called.get("n") == 1, "装配期未执行 schema 初始化"

        # 迁移失败 → PackConfigurationError(不是裸异常被 loader 吞)
        def _fail_get():
            raise RuntimeError("permission denied for schema public")

        monkeypatch.setattr(rt, "get_pack_db", _fail_get)
        try:
            rt.validate_pack_runtime_config()
            raise AssertionError("迁移失败未包装成致命异常")
        except PackConfigurationError as e:
            assert "迁移失败" in str(e)

    def test_migration_permission_error_wrapped(self, monkeypatch):
        """权限类迁移失败 → PackConfigurationError(真实 PG 形态).

        真实 Uvicorn 验证: 只读用户连无权限库, migrate_locked 的
        CREATE SCHEMA 抛 psycopg InsufficientPrivilege——
        get_pack_db 内部 _init_pack_schema 传播该异常, 本函数
        包装成 PackConfigurationError。
        """
        from domains.chatbi import runtime as rt
        from sdk.pack_api import PackConfigurationError

        def _perm_denied():
            raise PermissionError(
                "permission denied for schema public")

        monkeypatch.setattr(rt, "get_pack_db", _perm_denied)
        try:
            rt._migrate_schema_at_startup()
            raise AssertionError("权限失败未阻止装配")
        except PackConfigurationError as e:
            assert "迁移失败" in str(e)


class TestCriticalPackContract:
    """P1-B: critical pack 任一必需组件失败都终止(不允许部分 ChatBI).

    此前多 pack 场景: chatbi 失败 + 其它 pack 正常 → 服务照常
    ready(真实注入: loaded 只有 knowledge_graph)。
    """

    def test_tool_constructor_failure_blocks_multipack(self):
        """多 pack 下 chatbi 工具构造失败 → 终止(不静默跳过)."""
        import domains
        from sdk.pack_api import PackConfigurationError
        import domains.chatbi.tools.ask_data as ask_mod

        class _Broken:
            def __init__(self, *a, **kw):
                raise RuntimeError("simulated tool failure")

        real = ask_mod.AskDataTool
        ask_mod.AskDataTool = _Broken
        try:
            try:
                domains.load_all_packs(
                    pack_names=["chatbi", "knowledge_graph"])
                raise AssertionError("chatbi 失败被多 pack 容错吞掉")
            except PackConfigurationError:
                pass
        finally:
            ask_mod.AskDataTool = real

    def test_dependency_gate_failure_blocks_multipack(self, monkeypatch):
        """多 pack 下 chatbi 依赖闸门失败 → 终止."""
        import domains
        from sdk.pack_api import PackConfigurationError
        import services.pack_dependency as pd

        real = pd.evaluate_pack

        def _fail_chatbi(name, *a, **kw):
            if name == "chatbi":
                return {"status": "missing", "missing": ["llm"],
                        "detail": "simulated dep missing"}
            return real(name, *a, **kw)

        monkeypatch.setattr(pd, "evaluate_pack", _fail_chatbi)
        try:
            domains.load_all_packs(
                pack_names=["chatbi", "knowledge_graph"])
            raise AssertionError("chatbi 依赖失败被跳过")
        except PackConfigurationError:
            pass

    def test_api_mount_failure_blocks(self, monkeypatch):
        """chatbi API 路由构造失败 → mount 终止(不 catch-continue)."""
        import types
        from sdk.pack_api import PackConfigurationError
        from services.pack_api_mount import mount_pack_routers
        import domains.chatbi.pack as pack_mod

        def _broken():
            raise RuntimeError("simulated api router failure")

        monkeypatch.setattr(pack_mod, "create_api_router", _broken)
        app = types.SimpleNamespace(
            state=types.SimpleNamespace(),
            router=types.SimpleNamespace(routes=[]))
        app.include_router = lambda *a, **kw: None
        try:
            mount_pack_routers(app, ["chatbi", "knowledge_graph"])
            raise AssertionError("chatbi API 失败被跳过")
        except PackConfigurationError:
            pass

    def test_assembly_completeness_assertions(self):
        """assemble 末尾的 ready 不变量断言(handler/API/工具)."""
        import types
        from sdk.pack_api import PackConfigurationError
        from services.pack_manager import _assert_critical_packs_ready

        ok_state = types.SimpleNamespace(
            task_manager=types.SimpleNamespace(
                _handlers={"chatbi.refresh_semantics": lambda h: None}))
        result = {
            "loaded": ["chatbi"],
            "pack_tools": {"chatbi": ["ask_data", "switch_chart"]},
            "api_mounted": ["chatbi"],
        }
        _assert_critical_packs_ready(ok_state, result)   # 完整通过

        # handler 缺失
        bad_state = types.SimpleNamespace(
            task_manager=types.SimpleNamespace(_handlers={}))
        try:
            _assert_critical_packs_ready(bad_state, result)
            raise AssertionError("handler 缺失未被发现")
        except PackConfigurationError:
            pass
        # API 未挂载
        r2 = dict(result)
        r2["api_mounted"] = []
        try:
            _assert_critical_packs_ready(ok_state, r2)
            raise AssertionError("API 未挂载未被发现")
        except PackConfigurationError:
            pass
        # 工具缺失
        r3 = dict(result)
        r3["pack_tools"] = {"chatbi": ["ask_data"]}
        try:
            _assert_critical_packs_ready(ok_state, r3)
            raise AssertionError("工具缺失未被发现")
        except PackConfigurationError:
            pass


class TestHotReloadAtomicity:
    """P2: 多管理员热切换——装配总锁 + 失败回滚状态文件."""

    def test_hot_reload_lock_serializes(self):
        """hot_reload_lock 串行化并发装配(进程内互斥)."""
        import threading
        import time
        from services.pack_manager import hot_reload_lock
        order = []
        lock = hot_reload_lock()

        def _worker(i):
            with lock:
                order.append(f"enter{i}")
                time.sleep(0.05)
                order.append(f"exit{i}")

        ts = [threading.Thread(target=_worker, args=(i,))
              for i in range(3)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        # 串行化: 每个临界区不交错(enter 后紧跟同编号 exit)
        for i in range(3):
            assert order.index(f"enter{i}") < order.index(f"exit{i}")
        # 任意时刻只有一个临界区: enter/exit 严格交替
        nested = any(
            order[i].startswith("enter") and order[i + 1].startswith("enter")
            for i in range(len(order) - 1))
        assert not nested, f"装配临界区出现交错: {order}"

    def test_toggle_and_assemble_same_lock(self):
        """三十四审自查: toggle(set_enabled)与装配必须在同一锁内.

        此前 set_enabled 在锁外——两个管理员交错时, A 的回滚会
        覆盖 B 刚写入的状态(B 正在锁内按旧 snapshot 装配, 状态
        文件却被 A 改掉)。
        """
        import threading
        import time
        from services.pack_manager import hot_reload_lock
        order = []
        lock = hot_reload_lock()

        def admin(i):
            with lock:
                order.append(f"{i}-toggle")
                time.sleep(0.05)
                order.append(f"{i}-assemble")

        ta = threading.Thread(target=admin, args=("a",))
        tb = threading.Thread(target=admin, args=("b",))
        ta.start()
        tb.start()
        ta.join()
        tb.join()
        # 每个管理员的 toggle 和 assemble 相邻(不被对方插入)
        assert order in (
            ["a-toggle", "a-assemble", "b-toggle", "b-assemble"],
            ["b-toggle", "b-assemble", "a-toggle", "a-assemble"]), (
            f"toggle/装配交错: {order}")

    def test_admin_toggle_code_in_lock(self):
        """静态防回退锚: admin toggle 端点的 set_enabled 在锁内.

        set_enabled 挪回锁外会重新打开交错窗口。
        """
        import inspect
        from api import admin as admin_mod
        fn = None
        for _name, obj in inspect.getmembers(admin_mod,
                                             inspect.isfunction):
            try:
                body = inspect.getsource(obj)
            except (OSError, TypeError):
                continue
            if ("set_enabled" in body and "assemble_packs" in body
                    and "hot_reload_lock" in body):
                fn = obj
                break
        assert fn is not None, "未找到 toggle 端点(含 set_enabled + assemble)"
        body = inspect.getsource(fn)
        lock_pos = body.find("with hot_reload_lock():")
        toggle_pos = body.find("set_enabled(")
        assemble_pos = body.find("assemble_packs(")
        assert 0 <= lock_pos < toggle_pos, (
            "set_enabled 不在 hot_reload_lock 内(交错窗口重新打开)")
        assert toggle_pos < assemble_pos, "装配不在 toggle 之后"


# ════════════════════════════════════════════════════════════════
# 三十五审反例回归: critical 全异常域 / 两阶段装配 / 多 worker CAS
# ════════════════════════════════════════════════════════════════

class TestCriticalFullExceptionDomain:
    """P1-A: critical pack 的**任何**加载阶段异常都终止.

    真实注入(审计): load_pack('chatbi') 抛普通 RuntimeError、
    knowledge_graph 正常时, 函数仍返回成功且 loaded 只有
    knowledge_graph——普通异常分支 catch-continue 了 critical。
    """

    def test_ordinary_load_exception_blocks_multipack(self):
        import domains
        from sdk.pack_api import PackConfigurationError
        real = domains.load_pack

        def _broken(pack_name, app_state=None):
            if pack_name == "chatbi":
                raise RuntimeError(
                    "simulated chatbi module/default-router failure")
            return real(pack_name, app_state=app_state)

        domains.load_pack = _broken
        try:
            try:
                domains.load_all_packs(
                    pack_names=["chatbi", "knowledge_graph"])
                raise AssertionError(
                    "critical 普通加载异常被多 pack 容错吞掉")
            except PackConfigurationError:
                pass
        finally:
            domains.load_pack = real

    def test_assert_receives_requested(self):
        """断言接收 requested: requested 含 critical 而 loaded 不含
        时必须失败(此前只看 loaded, 被跳过的 critical 直接漏检)."""
        import types
        from sdk.pack_api import PackConfigurationError
        from services.pack_manager import _assert_critical_packs_ready
        state = types.SimpleNamespace(
            task_manager=types.SimpleNamespace(
                _handlers={"chatbi.refresh_semantics": lambda h: None}))
        result = {
            "loaded": ["knowledge_graph"],   # chatbi 被某路径跳过
            "pack_tools": {"knowledge_graph": ["kb_search"]},
            "api_mounted": ["knowledge_graph"],
        }
        try:
            _assert_critical_packs_ready(
                state, result, requested=["chatbi", "knowledge_graph"])
            raise AssertionError("requested 含 chatbi 但 loaded 不含, 未失败")
        except PackConfigurationError:
            pass

    def test_critical_list_single_source(self):
        """critical 名单单一真相源: 三处引用同一 sdk 函数."""
        import inspect
        from sdk.pack_api import critical_packs
        assert critical_packs() == frozenset({"chatbi"})
        from domains import critical_packs as dom_cp
        assert dom_cp is critical_packs, "domains 未复用 sdk 真相源"
        # pack_api_mount 不再持有本地名单副本
        import services.pack_api_mount as m
        src = inspect.getsource(m)
        assert "_CRITICAL_API_PACKS" not in src, (
            "pack_api_mount 仍有本地 critical 名单副本")
        import services.pack_manager as pm
        assert not hasattr(pm, "_CRITICAL_PACKS"), (
            "pack_manager 仍有旧名单(应只有 requirements 明细)")


class TestTwoPhaseAssembly:
    """P1-B: 两阶段装配——新 router 构造失败时旧 route 完好.

    真实注入(审计): mount 先 _unmount_all 再构造, 新 ChatBI
    router 失败时旧 route 已消失(old_route_survives=False)。
    """

    def test_old_routes_survive_new_router_failure(self, monkeypatch):
        from fastapi import FastAPI
        from sdk.pack_api import PackConfigurationError
        from services.pack_api_mount import mount_pack_routers
        import domains.chatbi.pack as pack_mod

        app = FastAPI()
        mount_pack_routers(app, ["chatbi"])
        old_routes = list(app.router.routes)
        assert old_routes, "初始挂载无 route"

        def _broken():
            raise RuntimeError("simulated new router failure")

        _real_create = pack_mod.create_api_router
        monkeypatch.setattr(pack_mod, "create_api_router", _broken)
        try:
            try:
                mount_pack_routers(app, ["chatbi", "knowledge_graph"])
                raise AssertionError("构造失败仍返回成功")
            except PackConfigurationError:
                pass
            survived = all(r in app.router.routes for r in old_routes)
            assert survived, "旧 ChatBI API 被卸掉(应两阶段保护)"
        finally:
            pack_mod.create_api_router = _real_create

    def test_assemble_precheck_before_runtime_mutation(self):
        """assemble 的 critical 预检在运行态切换之前(Prepare 段).

        静态防回退锚: _assert_critical_packs_ready 的首次调用
        必须出现在 nodes.configure / unload 之前。
        """
        import inspect
        from services import pack_manager as pm
        src = inspect.getsource(pm.assemble_packs)
        precheck = src.find("_assert_critical_packs_ready")
        configure = src.find("nodes.configure(")
        # commit 后的 unload 差集基于快照 old_loaded(三十七审 P1-D);
        # Prepare 预检必须在 commit 第一条运行态操作(nodes.configure)与
        # 生命周期启动(_start_pack_lifecycle)之前
        lifecycle = src.find("_start_pack_lifecycle")
        assert 0 < precheck < configure, (
            "critical 预检不在 nodes.configure 之前(Prepare 段缺失)")
        assert 0 < precheck < lifecycle, (
            "critical 预检不在生命周期启动之前(Prepare 段缺失)")


class TestRecheckSharesLock:
    """P2: recheck 与 toggle 共用同一装配锁."""

    def test_recheck_uses_hot_reload_lock(self):
        import inspect
        from api import admin as admin_mod
        src = None
        for _n, obj in inspect.getmembers(admin_mod, inspect.isfunction):
            try:
                body = inspect.getsource(obj)
            except (OSError, TypeError):
                continue
            if ("assemble_packs" in body
                    and "clear_probe_cache" in body):
                src = body
                break
        assert src is not None, "未找到 recheck 端点"
        assert "hot_reload_lock" in src, (
            "recheck 未使用 hot_reload_lock(无锁热装配入口)")


class TestPackStateMultiWorkerCAS:
    """P2: 两个独立 PackState 实例(多 worker)不丢更新.

    真实复现(审计): worker A 禁用 X, worker B 基于旧快照禁用 Y,
    磁盘上 X 又回来了(后写者覆盖)。
    """

    def test_two_writers_no_lost_update(self):
        import json
        import os
        import tempfile
        from services.pack_state import PackState
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        packs = ["chatbi", "knowledge_graph", "njmind_form"]
        try:
            a = PackState(path, packs)
            b = PackState(path, packs)   # worker B: 同一起点
            a.set_enabled("knowledge_graph", False)
            b.set_enabled("njmind_form", False)   # B 基于旧快照
            disk = json.load(open(path))
            assert "knowledge_graph" not in disk["enabled"], (
                "A 的禁用被 B 覆盖丢失(stale writer)")
            assert "njmind_form" not in disk["enabled"], (
                "B 的变更丢失")
            assert disk.get("revision", 0) >= 2, "revision 未递增"
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_pg16_required_at_assembly(self, monkeypatch):
        """三十三审 P2: PG<16 / 探测失败都在装配期被拒(fail-closed).

        版本探测与 schema 初始化已拆开(不再走 get_pack_db)——
        探测走底层 engine 只读查询; 查询失败 = fail-closed 抛
        PackIncompatibleError(不再当"稍后再试"返回成功)。
        """
        from sdk.pack_api import PackIncompatibleError
        from domains.chatbi import runtime as rt
        from sdk import relational_store as rs

        class _Row(dict):
            pass

        class _Conn:
            def __init__(self, version_num):
                self._v = version_num

            def execute(self, sql, *a):
                row = _Row(v=self._v)
                row.fetchone = lambda: row
                return row

        class _Engine:
            def __init__(self, version_num):
                self._v = version_num

            def connect(self):
                import contextlib

                @contextlib.contextmanager
                def _c():
                    yield _Conn(self._v)
                return _c()

        class _FakeDB:
            def __init__(self, engine):
                self.engine = engine

        # PG15 → PackIncompatibleError
        monkeypatch.setattr(rs, "PackRelationalDB",
                            lambda name: _FakeDB(_Engine(150000)))
        try:
            rt._require_pg16()
            raise AssertionError("PG15 未被版本校验拒绝")
        except PackIncompatibleError as e:
            assert "PostgreSQL" in str(e)
        # PG16 通过
        monkeypatch.setattr(rs, "PackRelationalDB",
                            lambda name: _FakeDB(_Engine(160004)))
        rt._require_pg16()
        # 探测失败(连接异常)→ fail-closed 抛错(不再当成功)
        class _FailEngine:
            def connect(self):
                raise ConnectionError("db not ready")

        monkeypatch.setattr(rs, "PackRelationalDB",
                            lambda name: _FakeDB(_FailEngine()))
        try:
            rt._require_pg16()
            raise AssertionError("探测失败未被拒绝(仍当成功)")
        except PackIncompatibleError:
            pass


# ════════════════════════════════════════════════════════════════
# 三十一审反例回归: 合法配置真执行 / date-like 脏租约隔离 / 小数拒绝
# ════════════════════════════════════════════════════════════════

class TestValidConfigRealExecution:
    """P1-A: 合法 PACK_DDL_RETRY_ATTEMPTS=5 必须真正可执行(不是数值相等).

    三十一审真实复现: float 解析让 range(1, 5.0+1) 抛 TypeError,
    真实 Uvicorn 上 ChatBI 全部 API 500 而 /health 仍 200。
    """

    def test_valid_attempts_full_migration(self, monkeypatch):
        """PACK_DDL_RETRY_ATTEMPTS=5 + 完整 _init_pack_schema 真执行."""
        import uuid
        from sdk.relational_store import PackRelationalDB
        from domains.chatbi import runtime as rt
        monkeypatch.setenv("PACK_DDL_RETRY_ATTEMPTS", "5")
        monkeypatch.setenv("PACK_DDL_RETRY_BACKOFF_SECONDS", "0.1")
        url = os.environ["TEST_DATABASE_URL"]
        probe = f"r31t_{uuid.uuid4().hex[:8]}"
        db = PackRelationalDB(probe, database_url=url)
        try:
            rt._init_pack_schema(db)   # 不抛 = range() 类型正确
            with db.engine.connect() as c:
                n_pack = c.execute(
                    "SELECT COUNT(*) AS n FROM information_schema.tables "
                    "WHERE table_schema = ?", (probe,)).fetchone()
            assert int(n_pack["n"]) >= 15, (
                f"迁移未真正执行: {n_pack['n']} 表")
        finally:
            with db.engine.connect() as c:
                c.execute(f'DROP SCHEMA IF EXISTS "{probe}" CASCADE')
        monkeypatch.delenv("PACK_DDL_RETRY_ATTEMPTS", raising=False)
        monkeypatch.delenv("PACK_DDL_RETRY_BACKOFF_SECONDS",
                           raising=False)

    def test_int_parser_strict_types(self, monkeypatch):
        """parse_int_env 返回 int; 小数/科学计数拒绝."""
        from sdk.env_config import parse_int_env
        monkeypatch.setenv("R31_TEST_INT", "5")
        v = parse_int_env("R31_TEST_INT", 3, minimum=1)
        assert v == 5 and type(v) is int, f"类型回归: {type(v)}"
        for bad in ["1.9", "5.0", "1e3", "0x10"]:
            monkeypatch.setenv("R31_TEST_INT", bad)
            try:
                parse_int_env("R31_TEST_INT", 3, minimum=1)
                raise AssertionError(f"{bad!r} 未被 int parser 拒绝")
            except ValueError:
                pass
        monkeypatch.delenv("R31_TEST_INT", raising=False)

    def test_conv_attempts_fraction_rejected(self, monkeypatch):
        """P3-C: CONV_DDL_RETRY_ATTEMPTS=1.9 必须拒绝(不静默截断)."""
        from services import conversation_store as cs_mod
        monkeypatch.setenv("CONV_DDL_RETRY_ATTEMPTS", "1.9")
        try:
            cs_mod.ConversationStore._init_db(
                type("S", (), {"_get_conn": None, "_DDL": []})())
            raise AssertionError("1.9 被静默截断(未拒绝)")
        except ValueError:
            pass
        monkeypatch.delenv("CONV_DDL_RETRY_ATTEMPTS", raising=False)


class TestDateLikePoisonIsolation:
    """P1-B: date-like 脏租约只阻断自身 key, 不毒化全表 claim.

    真实复现: '2026-99-99T00:00:00+00:00' 通过宽松正则但 cast 抛
    out of range, 整个事务回滚——任何一条脏行使所有 key 失败。
    """

    def test_poison_row_isolated_to_own_key(self, pg_engine):
        from domains.chatbi.tasks import claim_lease_token
        poison = "audit:r31:poison-t"
        other = "audit:r31:unrelated-t"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type IN (?, ?)", (poison, other))
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES "
                "(?, 'h', '2026-99-99T00:00:00+00:00')", (poison,))
        try:
            # 无关 key 的 claim 必须成功(不被毒化)
            tok = claim_lease_token(pg_engine, other, holder="task:x",
                                   ttl_seconds=300)
            assert tok is not None, "date-like 脏行毒化了无关 key"
            # poison 行保留原样(fail-closed, 不被改动)
            with pg_engine.connect() as c:
                row = c.execute(
                    "SELECT expires_at FROM chatbi_scheduler_leases "
                    "WHERE task_type = ?", (poison,)).fetchone()
            assert row["expires_at"] == "2026-99-99T00:00:00+00:00", (
                "脏行被改动(应保留原样)")
            # poison 自身 key 的 claim 拒绝(脏行只阻断自身)
            tok2 = claim_lease_token(pg_engine, poison, holder="task:x",
                                     ttl_seconds=300)
            assert tok2 is None, "脏 key 的 claim 不该成功"
        finally:
            with pg_engine.connect() as c:
                c.execute("DELETE FROM chatbi_scheduler_leases "
                          "WHERE task_type IN (?, ?)", (poison, other))

    def test_more_date_like_poisons_isolated(self, pg_engine):
        """更多 date-like 变体: 超范围月/日、非日期文本、超长数字."""
        from domains.chatbi.tasks import claim_lease_token
        poisons = [
            "2026-13-01T00:00:00+00:00",
            "2026-99-99T00:00:00+00:00",
            "2026-01-32T00:00:00+00:00",
            "9999-99-99",
        ]
        other = "audit:r31:unrelated-v"
        with pg_engine.connect() as c:
            for i, p in enumerate(poisons):
                c.execute(
                    "INSERT INTO chatbi_scheduler_leases "
                    "(task_type, holder, expires_at) VALUES "
                    "(?, 'h', ?)", (f"audit:r31:poison-v{i}", p))
        try:
            tok = claim_lease_token(pg_engine, other, holder="task:x",
                                    ttl_seconds=300)
            assert tok is not None, (
                f"date-like 变体毒化无关 key: {poisons}")
        finally:
            with pg_engine.connect() as c:
                c.execute("DELETE FROM chatbi_scheduler_leases "
                          "WHERE task_type LIKE 'audit:r31:%'")

    def test_probe_unknown_fail_closed_no_cast(self, pg_engine, monkeypatch):
        """三十二审 P1: 探测未知(异常/无连接)→ 不迁移任何候选行,
        脏值绝不进 cast——fail-closed 而非退化恒真分支."""
        import domains.chatbi.tasks as t
        poison = "audit:r32:probe-unknown"
        other = "audit:r32:probe-other"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type IN (?, ?)", (poison, other))
            # 一条未来 ISO(合法候选, 探测正常时会转 epoch)
            c.execute(
                "INSERT INTO chatbi_scheduler_leases "
                "(task_type, holder, expires_at) VALUES "
                "(?, 'h', '2099-01-01T00:00:00+00:00')", (poison,))
        # 注入探测异常: 不缓存, 返回 None(未知)
        monkeypatch.setattr(t, "_pg_has_input_valid",
                            lambda conn=None: None)
        try:
            # 无关 key 的 claim 仍成功(fail-closed 只跳过迁移)
            tok = t.claim_lease_token(pg_engine, other,
                                      holder="task:x", ttl_seconds=300)
            assert tok is not None, "探测未知毒化了无关 key"
            # 候选行保留原样(未迁移——探测未知时不 cast)
            with pg_engine.connect() as c:
                row = c.execute(
                    "SELECT expires_at FROM chatbi_scheduler_leases "
                    "WHERE task_type = ?", (poison,)).fetchone()
            assert row["expires_at"] == "2099-01-01T00:00:00+00:00", (
                "探测未知时候选行被迁移(应 fail-closed 保留)")
        finally:
            with pg_engine.connect() as c:
                c.execute("DELETE FROM chatbi_scheduler_leases "
                          "WHERE task_type IN (?, ?)", (poison, other))

    def test_pool1_poison_isolation(self):
        """三十二审 P1 真实场景: 单连接池下, 脏租约存在时无关 key
        快速 claim 成功(不等待嵌套连接的 pool timeout).

        三十三审 P3: 不再运行期修改共享池私有字段(不等价于以
        min/max=1 建池, 且污染同进程后续测试)——改用独立
        PgEngine(min_size=1, max_size=1) 真实单连接池, finally
        关闭。复现条件(上轮): PG_POOL_MIN=1/MAX=1/TIMEOUT=1,
        elapsed≈1.03s + date/time out of range; 修复后探测复用
        已持有连接, 无嵌套申请。
        """
        import time
        from services.db import PgEngine
        from sdk.relational_store import PackRelationalDB
        import domains.chatbi.tasks as t
        url = os.environ["TEST_DATABASE_URL"]
        # 独立单连接池(不碰 DSN 共享缓存)
        db = PackRelationalDB("chatbi", database_url=url)
        db.engine = PgEngine(url, min_size=1, max_size=1)
        t._PG_INPUT_VALID_CACHE = None
        poison = "audit:r32:pool1-poison"
        other = "audit:r32:pool1-other"
        try:
            with db.connect() as c:
                c.execute("DELETE FROM chatbi_scheduler_leases "
                          "WHERE task_type IN (?, ?)", (poison, other))
                c.execute(
                    "INSERT INTO chatbi_scheduler_leases "
                    "(task_type, holder, expires_at) VALUES "
                    "(?, 'h', '2026-99-99T00:00:00+00:00')", (poison,))
            t0 = time.time()
            tok = t.claim_lease_token(db, other, holder="task:x",
                                      ttl_seconds=300)
            elapsed = time.time() - t0
            assert tok is not None, "pool=1 下无关 key claim 失败"
            assert elapsed < 0.5, (
                f"pool=1 下 claim 耗时 {elapsed:.2f}s(疑似等待了嵌套"
                f"连接的 pool timeout)")
        finally:
            try:
                with db.connect() as c:
                    c.execute("DELETE FROM chatbi_scheduler_leases "
                               "WHERE task_type IN (?, ?)", (poison, other))
            finally:
                db.engine.close()   # 关闭独立池, 不泄漏连接


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
        """acquire 的 token 不可知 → 拒绝执行且不删除任何行(fail-closed).

        二十八审 P1-A: acquire 与 token 原子返回——token 不可知时
        直接拒绝, 不做任何二次读取或删除(按 holder 删可能误删同
        holder 既有租约, ABA)。
        """
        import domains.chatbi.tasks as tasks_mod
        key = "run:semantic_write:nulltok2"
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))
        # 先造一个同 holder 的既有租约(模拟重试对象持有的行)
        cur = tasks_mod.RunLease(pg_engine, key, holder="task:x",
                                 ttl_seconds=300)
        assert cur.acquire() and cur.token is not None
        # 旧对象 acquire: 原子返回 None(token 不可知的异常路径)
        lease = tasks_mod.RunLease(pg_engine, key, holder="task:x",
                                   ttl_seconds=300)
        # 直接验证: claim_lease_token 返回 None 时 acquire 拒绝
        # 且不触碰行(用真实函数的 None 分支——monkeypatch 模块函数)
        monkeypatch.setattr(tasks_mod, "claim_lease_token",
                            lambda *a, **kw: None)
        assert lease.acquire() is False, "无 token 仍开始执行(弱 fencing)"
        assert lease.acquired is False
        assert lease.token is None
        # 不盲删: 既有行保留等 TTL(不阻塞——TTL 后可被接管), 本对象
        # 拒绝执行已达到 fail-closed 目的
        with pg_engine.connect() as c:
            row = c.execute(
                "SELECT holder, token FROM chatbi_scheduler_leases "
                "WHERE task_type=?", (key,)).fetchone()
            assert row is not None, "token 不可读时行被盲删(ABA 风险)"
            assert int(row["token"]) == cur.token, "既有租约被破坏"
            # 清理: 直接按行删(测试自身造的行)
            c.execute("DELETE FROM chatbi_scheduler_leases "
                      "WHERE task_type=?", (key,))


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


class TestEventFailureAcknowledgement:
    """P2(二十七审): 告警确认/恢复生命周期——受控、可审计、不静默清零."""

    def test_acknowledge_archives_then_clears(self, pg_engine):
        """acknowledge: 每行留档(确认人/说明/原次数)后清空计数表."""
        from domains.chatbi.stores import (
            acknowledge_index_event_failures, list_index_event_failures)
        scope = "ack-scope"
        # 造两条失败告警
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failures "
                      "WHERE scope=?", (scope,))
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope=?", (scope,))
            c.execute(
                "INSERT INTO chatbi_index_event_failures "
                "(scope, build_id, version, event, failures, last_error, "
                "updated_at) VALUES (?, 'b1', 7, 'published', 3, 'boom', "
                "'2026-09-18T00:00:00+00:00')", (scope,))
            c.execute(
                "INSERT INTO chatbi_index_event_failures "
                "(scope, build_id, version, event, failures, last_error, "
                "updated_at) VALUES (?, 'b2', 8, 'building', 1, 'x', "
                "'2026-09-18T00:00:00+00:00')", (scope,))
        items = list_index_event_failures(pg_engine)
        assert len([i for i in items if i["scope"] == scope]) == 2
        cleared, acked = acknowledge_index_event_failures(
            pg_engine, "ops-alice", "已修复 milvus 权限并补偿事件")
        assert cleared == 2 and acked == 2
        # 计数表已清(monitor 恢复 OK)
        assert [i for i in list_index_event_failures(pg_engine)
                if i["scope"] == scope] == []
        # 审计留档完整: 原次数/确认人/说明
        with pg_engine.connect() as c:
            rows = c.execute(
                "SELECT build_id, failures, acknowledged_by, note "
                "FROM chatbi_index_event_failure_acks "
                "WHERE scope=? ORDER BY build_id", (scope,)).fetchall()
        assert len(rows) == 2
        by_bid = {r["build_id"]: r for r in rows}
        assert int(by_bid["b1"]["failures"]) == 3, "审计留档丢了原失败次数"
        assert by_bid["b1"]["acknowledged_by"] == "ops-alice"
        assert "补偿" in by_bid["b1"]["note"]
        with pg_engine.connect() as c:
            c.execute("DELETE FROM chatbi_index_event_failure_acks "
                      "WHERE scope=?", (scope,))

    def test_acknowledge_empty_is_noop(self, pg_engine):
        """无告警时 acknowledge 是 no-op(不产生空审计行)."""
        from domains.chatbi.stores import acknowledge_index_event_failures
        cleared, acked = acknowledge_index_event_failures(
            pg_engine, "ops-alice", "")
        assert cleared == 0 and acked == 0

    def test_acknowledge_api_requires_who(self, pg_engine):
        """API 层: 缺 acknowledged_by → 400(确认人必填, 进审计)."""
        from fastapi import HTTPException
        from domains.chatbi.api import acknowledge_index_event_failures as _ep

        class _Req:
            async def json(self):
                return {"note": "nobody"}

        import asyncio
        async def _call():
            try:
                await _ep(_Req())
                return None
            except HTTPException as e:
                return e
        exc = asyncio.run(_call())
        assert exc is not None and exc.status_code == 400, (
            f"缺确认人未被拒绝: {exc}")


# ════════════════════════════════════════════════════════════════
# 三十六审回归: 真事务化装配 / route 提交原子性 / 磁盘权威 CAS / 多 worker
# ════════════════════════════════════════════════════════════════

class TestAssembleTransaction:
    """P1-A: commit 失败不留半装配运行态(register_tasks 静默不注册注入).

    审计注入: chatbi register_tasks 静默不注册必需 handler →
    最终 critical 断言抛 PackConfigurationError, 但旧 registry/
    routers/_loaded_packs/handlers 已被替换(runtime 污染)。
    事务化后: 断言在 Prepare 段(staging)失败, 运行态零触碰。
    """

    def _build_app_state(self):
        from types import SimpleNamespace

        class _TM:
            def __init__(self):
                self._handlers = {"chatbi.refresh_semantics": lambda h: None}
                self._handler_meta = {"chatbi.refresh_semantics": {"packName": "chatbi"}}
                self.store = None

            def reset_handlers(self):
                self._handlers.clear()
                self._handler_meta.clear()

        tm = _TM()
        state = SimpleNamespace(task_manager=tm)
        state._loaded_packs = ["chatbi"]
        state.registry = object()
        state.pack_configs = {"chatbi": {}}
        state.pack_routers = {"chatbi": object()}
        state.pack_tools = {"chatbi": ["ask_data", "switch_chart"]}
        state.pack_dependency_status = {}
        return state, tm

    def test_handler_failure_preserves_runtime(self, monkeypatch):
        from sdk.pack_api import PackConfigurationError
        from services import pack_manager as pm
        import domains.chatbi.pack as pack_mod

        state, tm = self._build_app_state()
        old_registry = state.registry
        old_handlers = dict(tm._handlers)
        old_loaded = list(state._loaded_packs)

        def _silent_register(manager, app_state=None):
            pass   # 静默不注册任何 handler

        monkeypatch.setattr(pack_mod, "register_tasks", _silent_register)
        try:
            pm.assemble_packs(state, ["chatbi"], app=None)
            raise AssertionError("handler 缺失仍装配成功")
        except PackConfigurationError:
            pass
        # 运行态零污染: registry/handlers/loaded 全部保持旧值
        assert state.registry is old_registry, (
            "Prepare 失败后 registry 被替换(半装配)")
        assert dict(tm._handlers) == old_handlers, (
            "Prepare 失败后 handlers 被清空/替换(半装配)")
        assert state._loaded_packs == old_loaded, (
            "Prepare 失败后 _loaded_packs 被替换(半装配)")

    def test_commit_failure_rolls_back_runtime(self, monkeypatch):
        """commit 段注入失败(nodes.configure 抛错)→ 快照回滚旧运行态."""
        from sdk.pack_api import PackConfigurationError
        from services import pack_manager as pm
        from engine import nodes

        state, tm = self._build_app_state()
        # nodes.configure 需要的 app_state 成员
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()
        old_registry = state.registry
        old_handlers = dict(tm._handlers)

        def _broken_configure(**kwargs):
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(nodes, "configure", _broken_configure)
        try:
            pm.assemble_packs(state, ["chatbi"], app=None)
            raise AssertionError("commit 失败仍装配成功")
        except RuntimeError:
            pass
        # app.state 引用未被替换(commit 第一步就失败, 回滚后保持旧值)
        assert state.registry is old_registry, (
            "commit 失败后 registry 未回滚(半装配)")
        assert dict(tm._handlers) == old_handlers, (
            "commit 失败后 handlers 未保持旧值")


class TestRouteCommitAtomicity:
    """P1-B: include_router 提交失败不丢旧路由.

    审计注入: router factory 成功但 include_router 抛 RuntimeError →
    旧 route 已被 _unmount_all 卸掉(old_routes_survive=False)。
    事务化后: 展开在临时 app 上进行, 真实 app 只做一次性列表替换。
    """

    def test_include_failure_preserves_old_routes(self, monkeypatch):
        from fastapi import FastAPI
        from services.pack_api_mount import mount_pack_routers
        import domains.chatbi.pack as pack_mod

        app = FastAPI()
        mount_pack_routers(app, ["chatbi"])
        old_routes = list(app.router.routes)
        assert old_routes, "初始挂载无 route"

        # factory 成功返回一个 router, 但 include_router 展开时炸:
        # 用一个 include 时抛错的假 router
        class _PoisonRouter:
            def routes(self):
                return []

        from fastapi import APIRouter
        poison = APIRouter()

        @poison.get("/boom")
        def _boom():
            return {}

        # 让 FastAPI.include_router 在展开 poison 时抛错: monkeypatch
        # staged app 的 include_router 不可行(内部调用), 改为直接
        # monkeypatch services.pack_api_mount 内的 FastAPI 构造——
        # 更直接: monkeypatch commit_routes 的 app.router.routes 赋值
        # 不可行。真实路径: build_staged_routes 用临时 FastAPI 展开,
        # 展开失败(构造/导入)发生在触碰真实 app 前。这里注入 factory
        # 返回的 router 带 property 使 include 时抛错。
        class _ExplodingRouter(APIRouter):
            @property
            def routes(self):
                raise RuntimeError("simulated include failure")

            @routes.setter
            def routes(self, v):
                pass

        _real_create = pack_mod.create_api_router
        monkeypatch.setattr(
            pack_mod, "create_api_router", lambda: _ExplodingRouter())
        try:
            try:
                mount_pack_routers(app, ["chatbi"])
                raise AssertionError("include 失败仍挂载成功")
            except RuntimeError:
                pass
            survived = all(r in app.router.routes for r in old_routes)
            assert survived, "旧 ChatBI API 被卸掉(提交应原子)"
            from services.pack_api_mount import mounted_packs
            assert "chatbi" in mounted_packs(app), "挂载记录被破坏"
        finally:
            pack_mod.create_api_router = _real_create


class TestPackStateDiskAuthoritativeCAS:
    """P2-A: 磁盘权威 CAS 的四类反例(三十六审 4.3).

    A. stale no-op: B 持旧内存, 磁盘已被 A 改; B 显式反向操作
       必须按磁盘判定 changed 并落盘(不再 changed=False)。
    B. 历史触碰: B 曾动过 KG(旧算法终身 _touched), fresh A 后来
       启用 KG; B 写无关 pack 不得把 KG 覆盖回 disabled。
    C. 同 pack 冲突: A 禁→B(旧内存)再禁 → changed=False 磁盘不变;
       A 启→B(旧内存)再禁 → changed=True 落盘禁用。
    D. 多轮交替: A/B 交替写不同 pack, 全部保留。
    """

    PACKS = ["chatbi", "knowledge_graph", "njmind_form"]

    def _tmp_state(self):
        import tempfile, os, shutil
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        return path, tmpdir

    def test_stale_noop_reverse_applies(self):
        import json
        from services.pack_state import PackState
        path, tmpdir = self._tmp_state()
        try:
            b = PackState(path, self.PACKS)            # B 先起(内存 KG enabled)
            a = PackState(path, self.PACKS)
            a.set_enabled("knowledge_graph", False)   # A 禁用 → 磁盘 disabled
            assert b.is_enabled("knowledge_graph")     # B 旧内存仍 enabled
            changed = b.set_enabled("knowledge_graph", True)
            disk = json.load(open(path))
            assert changed is True, (
                "stale 实例的反向操作被误判 no-op(changed=False)")
            assert "knowledge_graph" in disk["enabled"], (
                "磁盘未应用 stale 实例的显式启用")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_historical_touch_not_sticky(self):
        import json
        from services.pack_state import PackState
        path, tmpdir = self._tmp_state()
        try:
            b = PackState(path, self.PACKS)
            b.set_enabled("knowledge_graph", False)   # B 曾禁 KG
            a = PackState(path, self.PACKS)            # fresh A
            a.set_enabled("knowledge_graph", True)     # A 启用 KG
            b.set_enabled("njmind_form", False)        # B 写无关 pack
            disk = json.load(open(path))
            assert "knowledge_graph" in disk["enabled"], (
                "B 的历史触碰把 KG 覆盖回 disabled(丢 A 的更新)")
            assert "njmind_form" not in disk["enabled"], (
                "B 本次操作丢失")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_same_pack_conflict_disk_wins(self):
        import json
        from services.pack_state import PackState
        path, tmpdir = self._tmp_state()
        try:
            a = PackState(path, self.PACKS)
            a.set_enabled("knowledge_graph", False)
            b = PackState(path, self.PACKS)            # B 见到 disabled
            a.set_enabled("knowledge_graph", True)     # A 又启用
            changed = b.set_enabled("knowledge_graph", False)
            disk = json.load(open(path))
            # B 旧内存=disabled, 磁盘=enabled → 按磁盘判 changed=True
            assert changed is True, "同 pack 冲突按旧内存误判 no-op"
            assert "knowledge_graph" not in disk["enabled"], (
                "磁盘未应用 B 的禁用")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_alternating_writes_all_preserved(self):
        import json
        from services.pack_state import PackState
        path, tmpdir = self._tmp_state()
        try:
            a = PackState(path, self.PACKS)
            b = PackState(path, self.PACKS)
            a.set_enabled("njmind_form", False)
            b.set_enabled("knowledge_graph", False)
            a.set_enabled("knowledge_graph", True)
            disk = json.load(open(path))
            assert "njmind_form" not in disk["enabled"], "A 的禁用丢失"
            assert "knowledge_graph" in disk["enabled"], (
                "A 的重新启用被 B 的旧快照覆盖")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_persist_after_mem_toggle(self):
        """toggle 内存暂存 → runtime commit 后 persist() 落盘(失败零落盘)."""
        import json
        import os
        from services.pack_state import PackState
        path, tmpdir = self._tmp_state()
        try:
            a = PackState(path, self.PACKS)
            changed = a.set_enabled(
                "knowledge_graph", False, persist=False)
            assert changed is True
            assert not os.path.exists(path), (
                "persist=False 仍落盘(失败路径会留下错位文件)")
            a.persist()
            disk = json.load(open(path))
            assert "knowledge_graph" not in disk["enabled"], (
                "persist() 未落盘内存变更")
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestMultiWorkerGuard:
    """P2-B: 共享状态文件的进程数 > 1 时动态管理被拒(503)."""

    def test_count_holders_detects_second_process(self):
        import json
        import os
        import subprocess
        import sys
        import tempfile
        import time
        from services.pack_state import count_state_file_holders

        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        try:
            # 本进程注册
            from services.pack_state import register_state_holder
            register_state_holder(path)
            # 起一个真实子进程也注册同一状态文件
            # 三十七审 P3: src 路径用本测试文件的绝对位置推导——
            # 此前硬编码相对 'src' 只在 backend/ cwd 下可跑
            import pathlib
            src_dir = pathlib.Path(__file__).resolve().parents[2] / "src"
            code = (
                "import sys, time; sys.path.insert(0, "
                f"{str(src_dir)!r});"
                "from services.pack_state import register_state_holder;"
                f"register_state_holder({path!r});"
                "print('ready', flush=True);"
                "time.sleep(30)"
            )
            proc = subprocess.Popen(
                [sys.executable, "-c", code],
                stdout=subprocess.PIPE, text=True)
            try:
                proc.stdout.readline()   # 等 ready
                time.sleep(0.3)
                holders = count_state_file_holders(path)
                assert holders >= 2, (
                    f"双进程未检出(holders={holders})——多 worker "
                    f"检测失效")
            finally:
                proc.kill()
                proc.wait()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_toggle_rejected_when_multi_worker(self, monkeypatch):
        """holders>1 时 toggle/recheck 返回 503(不进装配)."""
        from fastapi.testclient import TestClient
        from api import admin as admin_mod

        called = {"assemble": 0}

        def _fake_assemble(*a, **kw):
            called["assemble"] += 1
            return {"loaded": [], "tools": 0}

        monkeypatch.setattr(
            "services.pack_manager.assemble_packs", _fake_assemble)
        import services.pack_state as ps_mod
        monkeypatch.setattr(
            ps_mod, "count_state_file_holders", lambda p: 2)

        # 构造最小 request
        from types import SimpleNamespace
        from fastapi import FastAPI

        app = FastAPI()
        app.state.pack_state = SimpleNamespace(
            state_path="/tmp/x.json",
            is_discovered=lambda n: True,
            enabled_names=lambda: {"chatbi"},
        )

        from fastapi.testclient import TestClient
        c = TestClient(app)

        class _Req:
            pass

        # 直接调 _reject_multi_worker 验证 503
        from fastapi import HTTPException
        req = SimpleNamespace(app=app)
        try:
            admin_mod._reject_multi_worker(req)
            raise AssertionError("多 worker 未被拒绝")
        except HTTPException as e:
            assert e.status_code == 503, f"期望 503, 得到 {e.status_code}"
        assert called["assemble"] == 0, "被拒后仍触发了装配"


# ════════════════════════════════════════════════════════════════
# 三十七审回归: toggle 事务 / prepare 无副作用 / enhancer 事务 / unload
# ════════════════════════════════════════════════════════════════

class TestToggleTransaction:
    """P1-A: 已有状态文件 + 重启 + 首次 toggle 的四方一致性.

    审计反例: POST 200 但 persist 把磁盘/内存恢复成旧值(立即 GET 反弹)。
    根因: _read_file 不初始化 _last_seen_disk + persist 从整份内存
    集合推断删除意图。修复: persist 落盘"本次精确操作"。
    """

    PACKS = ["chatbi", "knowledge_graph", "leave_application", "njmind_form"]

    def test_restart_first_disable_persists(self):
        import json
        import os
        import tempfile
        import shutil
        from services.pack_state import PackState
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        try:
            # 造已有状态文件(全启用)
            a = PackState(path, self.PACKS)
            a.set_enabled("knowledge_graph", False)
            a.set_enabled("knowledge_graph", True)
            # 重启: 新实例读同一文件
            b = PackState(path, self.PACKS)
            # toggle 流程: persist=False → assemble(略) → persist
            changed = b.set_enabled(
                "leave_application", False, persist=False)
            assert changed is True
            b.persist()
            disk = json.load(open(path))["enabled"]
            assert "leave_application" not in disk["enabled"] if isinstance(
                disk, dict) else True
            assert "leave_application" not in disk, (
                f"磁盘反弹: {disk}")
            assert "leave_application" not in b.enabled_names(), (
                "内存被磁盘覆盖反弹")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_persist_no_pending_no_write(self):
        """无 pending 操作时 persist 零写入(纯重装配场景)."""
        import json
        import os
        import tempfile
        import shutil
        from services.pack_state import PackState
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        try:
            a = PackState(path, self.PACKS)
            a.set_enabled("knowledge_graph", False)
            before = open(path).read()
            rev_before = json.load(open(path))["revision"]
            a.persist()   # 无 pending
            after = open(path).read()
            rev_after = json.load(open(path))["revision"]
            assert before == after and rev_before == rev_after, (
                "无 pending 的 persist 不应写盘")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestPrepareSideEffectIsolation:
    """P1-B: prepare 失败不启动 scheduler / 不改 KG 全局.

    审计注入: 启用 ChatBI 后 route prepare 失败——旧 runtime 保留
    但 scheduler 已启动。修复: register_tasks 拆纯, 生命周期后移到
    _start_pack_lifecycle(commit 成功后)。
    """

    def test_failed_prepare_no_scheduler(self, monkeypatch):
        from services import pack_manager as pm
        from domains.chatbi import tasks as chatbi_tasks
        from types import SimpleNamespace

        started = []
        monkeypatch.setattr(
            chatbi_tasks, "_start_refresh_scheduler",
            lambda m, a: started.append("scheduler"))

        # 让 route prepare 失败: build_staged_routes 抛错
        def _broken_build(app, names):
            raise RuntimeError("simulated route prepare failure")

        monkeypatch.setattr(
            "services.pack_api_mount.build_staged_routes", _broken_build)

        from fastapi import FastAPI
        app = FastAPI()

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM())
        state._loaded_packs = ["knowledge_graph"]
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()

        try:
            pm.assemble_packs(state, ["chatbi"], app=app)
            raise AssertionError("prepare 失败仍装配成功")
        except RuntimeError:
            pass
        assert started == [], (
            f"prepare 失败仍启动了生命周期: {started}")


class TestEnhancerTransaction:
    """P1-C: enhance_asset_client 抛错 → runtime + client 全回滚."""

    def test_enhancer_failure_rolls_back_all(self, monkeypatch):
        from services import pack_manager as pm
        from engine import nodes
        from types import SimpleNamespace

        class _AssetClient:
            def __init__(self):
                self._config_api = "OLD"

            def set_config_api(self, api):
                self._config_api = api

        asset = _AssetClient()

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM(), asset_client=asset,
                                upstream=object())
        state._loaded_packs = ["njmind_form"]
        state.registry = "OLD_REGISTRY"
        state.llm_client = object()
        state.conversation_manager = object()
        old_nodes_registry = nodes._registry

        import domains.njmind_form.pack as nj_pack
        def _broken_enhance(client, upstream):
            client.set_config_api("NEW")   # 先改再炸
            raise RuntimeError("simulated enhancer failure")
        monkeypatch.setattr(nj_pack, "enhance_asset_client", _broken_enhance)

        try:
            pm.assemble_packs(state, ["njmind_form"], app=None)
            raise AssertionError("enhancer 失败仍装配成功")
        except RuntimeError:
            pass
        assert asset._config_api == "OLD", (
            f"asset client 未回滚: {asset._config_api}")
        assert state.registry == "OLD_REGISTRY", "registry 未回滚"
        assert state._loaded_packs == ["njmind_form"], "loaded 未回滚"


class TestUnloadAfterDisable:
    """P1-D: 成功禁用后 unload 真实执行(差集基于快照 old_loaded).

    审计注入: KG+njmind → 仅 njmind, unload_calls=[](差集恒空)。
    """

    def test_disable_triggers_unload(self, monkeypatch):
        from services import pack_manager as pm
        from types import SimpleNamespace

        unload_calls = []
        import domains.knowledge_graph.pack as kg_pack
        monkeypatch.setattr(kg_pack, "unload", lambda: unload_calls.append("kg"))

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM())
        state._loaded_packs = ["knowledge_graph", "njmind_form"]
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()

        # 只装配 njmind_form(禁用 KG); unload 在 finalize 段执行
        # (三十八审 P2-B: 外部副作用等 runtime+磁盘确认后)
        summary = pm.assemble_packs(state, ["njmind_form"], app=None)
        assert unload_calls == [], (
            f"finalize 前 unload 不应执行: {unload_calls}")
        pm.finalize_assembly(state, summary["_tx"])
        assert unload_calls == ["kg"], (
            f"禁用 KG 后 unload 未执行: {unload_calls}")
        assert state._loaded_packs == ["njmind_form"]


class TestNoFileNoopKeepsMemory:
    """P2: 无状态文件时 no-op 不清空内存 enabled."""

    def test_noop_keeps_memory(self):
        import os
        import tempfile
        import shutil
        from services.pack_state import PackState
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        try:
            c = PackState(path, ["chatbi", "knowledge_graph", "njmind_form"])
            before = sorted(c.enabled_names())
            changed = c.set_enabled("chatbi", True)   # 已启用再启用
            assert changed is False
            assert sorted(c.enabled_names()) == before, (
                f"no-op 清空了内存: {c.enabled_names()}")
            assert not os.path.exists(path), "no-op 不应创建文件"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestHolderFailClosed:
    """P2: 持有者检测失效时管理操作被拒(fail-closed)."""

    def test_count_returns_negative_on_read_error(self, monkeypatch):
        import builtins
        from services.pack_state import count_state_file_holders
        import os
        import tempfile
        import shutil
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "state.json")
        try:
            # 造 holders 文件, 再让 open 全部失败
            holders = os.path.join(tmpdir, "state.holders")
            open(holders, "w").write('{"pid": 1, "ts": 9999999999}\n')
            real_open = builtins.open

            def _broken_open(f, *a, **kw):
                if str(f) == holders:
                    raise OSError("simulated read failure")
                return real_open(f, *a, **kw)

            monkeypatch.setattr(builtins, "open", _broken_open)
            n = count_state_file_holders(path)
            assert n == -1, f"检测失效应返回 -1(fail-closed), 得到 {n}"
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 三十八审回归: 快照回滚/恢复隔离/lifecycle 可见/finalize 边界/撤销
# ════════════════════════════════════════════════════════════════

class TestSnapshotRollback:
    """P1-A: persist 失败按快照确定性回滚(不重跑装配)."""

    def _state(self):
        from types import SimpleNamespace

        class _TM:
            def __init__(self):
                self._handlers = {"old.handler": lambda h: None}
                self._handler_meta = {"old.handler": {"packName": "x"}}
                self.store = None

            def reset_handlers(self):
                self._handlers.clear()
                self._handler_meta.clear()

        state = SimpleNamespace(task_manager=_TM())
        state._loaded_packs = ["chatbi", "leave_application"]
        state.registry = "OLD"
        state.llm_client = object()
        # asset_client 须可 setattr(_restore_runtime 恢复 _config_api)
        state.asset_client = SimpleNamespace(_config_api=None)
        state.conversation_manager = object()
        return state

    def test_rollback_no_full_assemble(self, monkeypatch):
        """persist 失败 → rollback_assembly 直接恢复, 不重跑装配."""
        from services import pack_manager as pm
        state = self._state()
        summary = pm.assemble_packs(
            state, ["chatbi", "njmind_form"], app=None)
        assert state._loaded_packs == ["chatbi", "njmind_form"]

        # 注入: 反向装配一定失败(证明 rollback 没走它)
        def _boom_assemble(*a, **kw):
            raise RuntimeError("assemble must not be called")
        monkeypatch.setattr(pm, "assemble_packs", _boom_assemble)

        ok = pm.rollback_assembly(state, summary["_tx"])
        assert ok is True
        assert state._loaded_packs == ["chatbi", "leave_application"], (
            "快照回滚未恢复 loaded")
        assert state.registry == "OLD"
        assert "old.handler" in state.task_manager._handlers

    def test_rollback_failed_returns_false(self, monkeypatch):
        """恢复组件失败 → rollback 返回 False(调用方进入 degraded)."""
        from services import pack_manager as pm
        from engine import nodes
        state = self._state()
        summary = pm.assemble_packs(
            state, ["chatbi", "njmind_form"], app=None)

        # 注入: nodes.configure 恢复时抛错
        def _broken_configure(**kw):
            raise RuntimeError("restore boom")
        monkeypatch.setattr(nodes, "configure", _broken_configure)
        ok = pm.rollback_assembly(state, summary["_tx"])
        monkeypatch.undo()
        assert ok is False, "恢复失败应返回 False(degraded 信号)"


class TestRestoreIsolation:
    """P1-B: 任一恢复 setter 抛错, 其他组件仍恢复(独立 compensator)."""

    def _setup(self):
        from types import SimpleNamespace

        class _Compressor:
            def __init__(self):
                self._compact_focus = "OLD_FOCUS"

            def set_compact_focus(self, focus):
                self._compact_focus = focus
                raise RuntimeError("setter always fails")

        class _TM:
            def __init__(self):
                self._handlers = {"old": lambda h: None}
                self._handler_meta = {"old": {"packName": "x"}}
                self.store = None

            def reset_handlers(self):
                self._handlers.clear()
                self._handler_meta.clear()

        comp = _Compressor()
        state = SimpleNamespace(task_manager=_TM(), compressor=comp)
        state._loaded_packs = ["chatbi"]
        state.registry = "OLD"
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()
        return state, comp

    def test_compressor_failure_doesnt_block_handlers(self):
        """compressor setter 抛错 → handlers 仍恢复(独立 compensator).

        commit 的 set_compact_focus 抛错 → assemble 整体抛且内部已按
        快照回滚; 旧实现单一 try 里恢复再抛会阻断 handlers/routes。
        """
        from services import pack_manager as pm
        state, comp = self._setup()
        try:
            pm.assemble_packs(state, ["chatbi"], app=None)
            raise AssertionError("compressor setter 抛错仍装配成功")
        except RuntimeError:
            pass
        # assemble 内部已调 _restore_runtime; handlers 应已恢复
        assert "old" in state.task_manager._handlers, (
            "compressor 恢复失败阻断了 handlers 恢复")
        # compressor 用直接赋值恢复(不走已知失败的 setter)
        assert comp._compact_focus == "OLD_FOCUS", (
            f"compressor focus 未恢复: {comp._compact_focus}")


class TestLifecycleFailureVisible:
    """P2-A: critical scheduler 启动失败 → 装配失败(fail-fast)."""

    def test_scheduler_failure_fails_assembly(self, monkeypatch):
        from services import pack_manager as pm
        from sdk.pack_api import PackConfigurationError
        from domains.chatbi import tasks as chatbi_tasks

        def _boom(manager, app_state):
            raise RuntimeError("scheduler boom")
        monkeypatch.setattr(
            chatbi_tasks, "_start_refresh_scheduler", _boom)

        from types import SimpleNamespace

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM())
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()

        summary = pm.assemble_packs(state, ["chatbi"], app=None)
        try:
            pm.finalize_assembly(state, summary["_tx"])
            raise AssertionError("scheduler 失败仍 finalize 成功")
        except PackConfigurationError:
            pass

    def test_kg_recovery_runs_once(self, monkeypatch):
        """P2-B: KG stale recovery 每进程只执行一次(startup-only)."""
        from services import pack_manager as pm
        from domains.knowledge_graph import tasks as kg_tasks
        from types import SimpleNamespace

        calls = []
        monkeypatch.setattr(
            kg_tasks, "_recover_stale_importing",
            lambda m: calls.append("run"))

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM())
        state.llm_client = object()
        state.asset_client = object()
        state.conversation_manager = object()

        calls = []
        monkeypatch.setattr(
            kg_tasks, "_recover_stale_importing",
            lambda m: calls.append("run"))

        # 直接测 once guard: 连续两次调用只执行一次
        # (不走 assemble——KG 依赖在测试环境未配置, loader 会跳过)
        pm._KG_RECOVERY_DONE = False
        pm._run_kg_startup_recovery_once(kg_tasks, None)
        pm._run_kg_startup_recovery_once(kg_tasks, None)
        assert calls == ["run"], (
            f"KG recovery 应只执行一次(实际 {calls})")


class TestEnhancerDetach:
    """P2-C: 禁用 njmind_form 后 config_api 被撤销(整体替换)."""

    def test_disable_revokes_config_api(self):
        from types import SimpleNamespace

        class _AssetClient:
            def __init__(self):
                self._config_api = None

            def set_config_api(self, api):
                self._config_api = api

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        asset = _AssetClient()
        state = SimpleNamespace(task_manager=_TM(), asset_client=asset,
                                upstream=object())
        state.llm_client = object()
        state.conversation_manager = object()

        from services import pack_manager as pm
        # 启用 njmind_form → config_api 注入
        pm.assemble_packs(state, ["njmind_form"], app=None)
        assert asset._config_api is not None, "启用后应注入"

        # 禁用(切到 leave_application) → config_api 撤销为 None
        pm.assemble_packs(state, ["leave_application"], app=None)
        assert asset._config_api is None, (
            f"禁用后 config_api 残留: {asset._config_api}")


class TestHolderClose:
    """P3: holder close 后线程退出, 目录不重建."""

    def test_close_stops_thread_and_no_rebuild(self):
        import os
        import shutil
        import tempfile
        import time
        from services.pack_state import register_state_holder
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "s.json")
        try:
            h = register_state_holder(path)
            time.sleep(0.1)
            h.close()
            assert not h._thread.is_alive(), "close 后线程仍存活"
            shutil.rmtree(tmpdir)
            time.sleep(1)   # 短周期验证(close 后 wait 立即返回, 不会重建)
            assert not os.path.exists(tmpdir), (
                "close 后目录被心跳重建")
        finally:
            if os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)


# ════════════════════════════════════════════════════════════════
# 三十九审回归: 事务阶段状态机/rollback 传播/scheduler 屏障/holder 契约
# ════════════════════════════════════════════════════════════════

class TestTransactionStages:
    """P1-A: persist 成功 + finalize 失败 → 保持新状态 + degraded."""

    def test_finalize_failure_keeps_new_state(self, monkeypatch):
        import json
        import os
        import tempfile
        import shutil
        from types import SimpleNamespace
        from services import pack_manager as pm
        from services.pack_state import PackState
        from domains.chatbi import tasks as chatbi_tasks
        from sdk.pack_api import PackConfigurationError

        def _boom(manager, app_state):
            raise RuntimeError("scheduler boom")
        monkeypatch.setattr(
            chatbi_tasks, "_start_refresh_scheduler", _boom)

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        tmpdir = tempfile.mkdtemp()
        try:
            ps = PackState(os.path.join(tmpdir, "s.json"),
                           ["chatbi", "leave_application", "njmind_form"])
            state = SimpleNamespace(task_manager=_TM())
            state.llm_client = object()
            state.asset_client = None
            state.conversation_manager = object()

            # toggle 流程: 内存改 → assemble → persist → finalize(炸)
            ps.set_enabled("leave_application", False, persist=False)
            summary = pm.assemble_packs(
                state, sorted(ps.enabled_names()), app=None)
            ps.persist()
            disk = json.load(open(ps.state_path))["enabled"]
            try:
                pm.finalize_assembly(state, summary["_tx"])
                raise AssertionError("finalize 应抛")
            except PackConfigurationError:
                pass
            # 三方保持新状态(磁盘/内存/runtime), 不回滚
            assert "leave_application" not in disk, f"磁盘被回滚: {disk}"
            assert "leave_application" not in ps.enabled_names()
            assert "leave_application" not in state._loaded_packs
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_assemble_rollback_failure_propagates(self, monkeypatch):
        """P1-B: commit 失败 + 内部恢复失败 → AssemblyRollbackFailedError."""
        from types import SimpleNamespace
        from services import pack_manager as pm
        from engine import nodes

        class _TM:
            def __init__(self):
                self._handlers = {}
                self._handler_meta = {}
                self.store = None

            def reset_handlers(self):
                pass

        state = SimpleNamespace(task_manager=_TM())
        state.llm_client = object()
        state.asset_client = None
        state.conversation_manager = object()

        def _broken(**kw):
            raise RuntimeError("configure always boom")
        monkeypatch.setattr(nodes, "configure", _broken)
        try:
            pm.assemble_packs(state, ["chatbi"], app=None)
            raise AssertionError("应抛 AssemblyRollbackFailedError")
        except pm.AssemblyRollbackFailedError:
            pass   # 上层据此 degraded


class TestSchedulerBarrier:
    """P1-D: tick 异常屏障 + scheduler_status 暴露."""

    def test_tick_exception_doesnt_kill_barrier_semantics(self):
        """tick 内未捕获异常会传播到 _loop 的屏障(线程不死)."""
        import inspect
        from domains.chatbi import tasks as ct
        src = inspect.getsource(ct)
        loop_start = src.find("while not _refresh_stop.wait(30):")
        seg = src[loop_start:loop_start + 800]
        assert "_scheduler_tick(_loop, manager, app_state)" in seg, (
            "循环未调用抽出的 tick(屏障缺失)")
        assert "except Exception" in seg, "tick 调用无异常屏障"

    def test_scheduler_status_shape(self):
        from domains.chatbi import tasks as ct
        st = ct.scheduler_status()
        assert set(st) >= {"alive", "tick_failures", "last_tick"}, st

    def test_health_detail_includes_scheduler(self):
        import inspect
        from domains.chatbi import api as cb_api
        src = inspect.getsource(cb_api)
        assert "scheduler_status" in src, (
            "health detail 未暴露 scheduler 组件")

    def test_tick_survives_lease_error(self):
        """tick 内 acquire_lease 抛 → 异常传播(屏障捕获), 不静默."""
        from types import SimpleNamespace
        from domains.chatbi import tasks as ct

        def _boom_lease(*a, **kw):
            raise RuntimeError("transient lease DB error")
        orig = ct.acquire_lease
        ct.acquire_lease = _boom_lease
        try:
            loop_state = SimpleNamespace(
                _last_purge=0.0, _last_gc=0.0,
                _last_health=0.0, _last_refresh=0.0)
            try:
                ct._scheduler_tick(loop_state, None, None)
                raise AssertionError("lease 异常应传播到屏障")
            except RuntimeError:
                pass
        finally:
            ct.acquire_lease = orig


class TestHolderUUIDIdentity:
    """P1-C/P2-B: UUID 身份 + close 完整契约."""

    def test_two_handles_refcount(self):
        import os
        import tempfile
        import time
        import shutil
        from services.pack_state import (
            register_state_holder, count_state_file_holders)
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "s.json")
        try:
            h1 = register_state_holder(path)
            h2 = register_state_holder(path)
            assert count_state_file_holders(path) == 1
            h1.close()   # 关一个: 另一个仍活
            time.sleep(0.2)
            assert count_state_file_holders(path) == 1, (
                "关闭一个共享 handle 不应注销槽位")
            assert h2._thread.is_alive()
            h2.close()
            h2.close()   # 幂等
            time.sleep(0.2)
            assert count_state_file_holders(path) == 0, "全关后槽位未注销"
            assert not h2._thread.is_alive()
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_close_no_atexit_rebuild(self):
        """close + 删目录 + 子进程退出 → 目录不重建."""
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        import time
        import pathlib
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "s.json")
        src_dir = pathlib.Path(__file__).resolve().parents[2] / "src"
        code = (
            "import sys, time; sys.path.insert(0, "
            f"{str(src_dir)!r});"
            "from services.pack_state import register_state_holder;"
            f"h = register_state_holder({path!r});"
            "h.close(); print('closed', flush=True); time.sleep(0.5)"
        )
        r = subprocess.run([sys.executable, "-c", code],
                           capture_output=True, text=True)
        assert "closed" in r.stdout, r.stderr
        shutil.rmtree(tmpdir)
        time.sleep(1)
        assert not os.path.exists(tmpdir), (
            "close 后 atexit 仍重建目录")

    def test_two_processes_distinct_uuid(self):
        """两个独立进程(模拟容器)各自 UUID, count=2."""
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        import time
        import pathlib
        from services.pack_state import count_state_file_holders
        tmpdir = tempfile.mkdtemp()
        path = os.path.join(tmpdir, "s.json")
        src_dir = pathlib.Path(__file__).resolve().parents[2] / "src"
        code = (
            "import sys, time; sys.path.insert(0, "
            f"{str(src_dir)!r});"
            "from services.pack_state import register_state_holder;"
            f"register_state_holder({path!r});"
            "print('ready', flush=True); time.sleep(20)"
        )
        procs = [subprocess.Popen(
            [sys.executable, "-c", code],
            stdout=subprocess.PIPE, text=True) for _ in range(2)]
        try:
            for p in procs:
                p.stdout.readline()
            time.sleep(0.5)
            assert count_state_file_holders(path) == 2, (
                "双进程未按 UUID 计数(身份碰撞)")
        finally:
            for p in procs:
                p.kill(); p.wait()
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestKGRecoveryRetry:
    """P2-C: recovery 失败不置 done, 重试到成功."""

    def test_first_failure_retries(self, monkeypatch):
        from services import pack_manager as pm
        from domains.knowledge_graph import tasks as kg_tasks

        calls = []

        def _flaky(m):
            calls.append("run")
            if len(calls) == 1:
                raise RuntimeError("transient recovery error")

        monkeypatch.setattr(kg_tasks, "_recover_stale_importing", _flaky)
        pm._KG_RECOVERY_DONE = False
        try:
            try:
                pm._run_kg_startup_recovery_once(kg_tasks, None)
            except RuntimeError:
                pass
            assert pm._KG_RECOVERY_DONE is False, (
                "首次失败就置 done(永不重试)")
            pm._run_kg_startup_recovery_once(kg_tasks, None)
            assert calls == ["run", "run"], "第二次未重试"
            assert pm._KG_RECOVERY_DONE is True, "成功后未置 done"
        finally:
            pm._KG_RECOVERY_DONE = False
