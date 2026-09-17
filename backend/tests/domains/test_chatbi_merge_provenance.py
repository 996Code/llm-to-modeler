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
    """P1 7.4: calculated_fields 不被扫描/刷新清空."""

    def test_calculated_fields_survive_rescan(self):
        from domains.chatbi.tasks import _merge_rescan, _merge_content

        cf = CalculatedField(name="profit", display_name="利润",
                             formula="revenue - cost")
        old = SemanticModelContent(models=[
            Model(name="t", display_name="T", columns=[_col("id")],
                  calculated_fields=[cf]),
        ])
        scan = SemanticModelContent(models=[
            Model(name="t", display_name="t", columns=[_col("id")]),
        ])
        # rescan 后保留
        r1 = _merge_rescan(old, scan)
        assert len(r1.models[0].calculated_fields) == 1
        assert r1.models[0].calculated_fields[0].name == "profit"
        # refresh 后也保留
        r2 = _merge_content(old, scan)
        assert len(r2.models[0].calculated_fields) == 1


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
