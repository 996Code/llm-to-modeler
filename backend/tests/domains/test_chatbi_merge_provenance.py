"""十五审验收测试: merge provenance + 集合去重 + 校验 + 空状态 CAS.

关键约束: fixture 模拟真实扫描来源——无数据库注释的表/列是
auto_inferred(不依赖模型默认 manual), 这是十四审测试掩盖边界的根因.
"""
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
        assert save_merge_report(pg_engine, "oo-ds", 12, empty) is True
        assert save_merge_report(pg_engine, "oo-ds", 11, warning) is False, \
            "v11 报告覆盖了 v12(版本守卫缺失)"
        saved = get_merge_report(pg_engine, "oo-ds")
        assert saved["version"] == 12
        assert saved["report"]["requires_review"] is False, "过期告警复活"

    def test_same_version_overwrite_allowed(self, pg_engine):
        from domains.chatbi.tasks import save_merge_report, get_merge_report
        r1 = {"dropped_items": [{"kind": "metric", "table": "t", "name": "a",
                                 "reason": "x"}], "conflicts": [],
              "requires_review": True}
        assert save_merge_report(pg_engine, "oo-ds-2", 5, r1) is True
        # 同版本重试(修正后的报告)允许覆盖
        assert save_merge_report(pg_engine, "oo-ds-2", 5,
                                 {"dropped_items": [], "conflicts": [],
                                  "requires_review": False}) is True
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
        """直接写台账(时间往回拨 age_seconds, 绕过真实构建)."""
        from datetime import datetime, timedelta, timezone
        ts = (datetime.now(timezone.utc)
              - timedelta(seconds=age_seconds)).isoformat()
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO chatbi_index_builds "
                "(scope, version, doc_id, status, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT (scope, version) DO UPDATE SET "
                "doc_id = EXCLUDED.doc_id, status = EXCLUDED.status, "
                "updated_at = EXCLUDED.updated_at",
                (scope, version, doc, status, ts))

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
                "SET expires_at = ((extract(epoch FROM now()) - 1)*1000)::text "
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
