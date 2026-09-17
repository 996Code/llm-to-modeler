"""十五审验收测试: merge provenance + 集合去重 + 校验 + 空状态 CAS.

关键约束: fixture 模拟真实扫描来源——无数据库注释的表/列是
auto_inferred(不依赖模型默认 manual), 这是十四审测试掩盖边界的根因.
"""
import pytest
from domains.chatbi.models import (
    SemanticModelContent, Model, Column, Metric, Relationship,
    CalculatedField,
)


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


# ── 索引 revision namespace(十七审 7.7)───────────────────────────

class _FakeVectorStore:
    """ChatBIVectorStore 替身: 记录 doc_id 分区内容, 支持按分区删/查."""

    def __init__(self):
        self.partitions: dict[str, list] = {}   # doc_id → records
        self.fail_upsert = False

    def upsert_records(self, scope, records, doc_id):
        if self.fail_upsert:
            raise RuntimeError("milvus down")
        self.partitions.setdefault(doc_id, [])
        self.partitions[doc_id].extend(records)
        return len(records)

    def delete_doc(self, scope, doc_id):
        n = len(self.partitions.get(doc_id, []))
        self.partitions.pop(doc_id, None)
        return n

    def search_records(self, scope, query_vector, top_k=5,
                       score_threshold=0.0, doc_id=None):
        return list(self.partitions.get(doc_id, []))[:top_k]


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
        assert "schema_r10" in store.partitions

        # v11 发布 → r10 分区被延迟清理
        r2 = rebuild_index(_idx_content("b"), ds, store, _FakeEmbedder(),
                           db=pg_engine, revision=11)
        assert r2.error is None
        with pg_engine.connect() as conn:
            row = conn.execute(
                "SELECT active_doc_id FROM chatbi_index_revisions").fetchone()
        assert row["active_doc_id"] == "schema_r11"
        assert "schema_r10" not in store.partitions, "旧分区未清理"
        assert "schema_r11" in store.partitions

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
        store.fail_upsert = True
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
