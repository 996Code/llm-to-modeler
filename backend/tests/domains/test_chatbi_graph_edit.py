"""chatbi 图谱关系编辑测试 —— graph_edit 校验/增删/反向清理 + 请求模型 Literal。

复核报告(P0/P1)要求的覆盖:
  - ON 条件结构化校验: 格式非法/表不在这对关系/列不存在/多列 AND/引号风格;
  - 新增: 源表 404 / 目标表 404 / 自环 422 / 重复 409(含反向书写 ON) /
    版本递增 / 索引重建失败不谎报成功(index_rebuilt=False);
  - 删除: 正反向一起清理 / ON 反向书写匹配 / 无匹配 404;
  - GraphRelationshipIn: 非法 join_type/cardinality 被 pydantic 422 拒绝。

跑法(专属库):
  TEST_DATABASE_URL=postgresql://root:root@localhost:5432/chatbi_test_graph \\
      ./venv/bin/python -m pytest tests/domains/test_chatbi_graph_edit.py -q
"""
import pytest
from pydantic import ValidationError

from domains.chatbi.graph_edit import (
    GraphEditError,
    add_relationship,
    conditions_to_on,
    delete_relationship,
    parse_on_conditions,
)
from domains.chatbi.models import (
    Column,
    Model,
    Relationship,
    SemanticModelContent,
)


@pytest.fixture()
def db():
    from sdk.relational_store import PackRelationalDB
    from domains.chatbi.datasources import init_store, configure_encryption
    from cryptography.fernet import Fernet
    configure_encryption(Fernet.generate_key().decode())
    d = PackRelationalDB("chatbi")
    init_store(d)
    yield d


def _content() -> SemanticModelContent:
    """两表语义层: orders(id, user_id, city) / users(id, city)。"""
    orders = Model(name="orders", display_name="订单表", columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
        Column(name="user_id", display_name="用户ID", data_type="INTEGER"),
        Column(name="city", display_name="城市", data_type="TEXT"),
    ])
    users = Model(name="users", display_name="用户表", columns=[
        Column(name="id", display_name="ID", data_type="INTEGER"),
        Column(name="city", display_name="城市", data_type="TEXT"),
    ])
    return SemanticModelContent(models=[orders, users])


@pytest.fixture()
def ds(db):
    """建数据源 + 落一版语义层, 返回 ds_id。"""
    from domains.chatbi import datasources, semantic
    info = datasources.create_datasource(
        db, "图测数据源", "postgresql", "localhost", 5432, "x", "u", "p")
    semantic.save_content(db, info.id, _content(), source="scan")
    return info.id


# ══ ON 条件结构化校验 ════════════════════════════════════════

class TestParseOnConditions:
    def _c(self):
        return _content()

    def test_valid_single_and_multi(self):
        conds = parse_on_conditions(
            self._c(), "orders", "users", "orders.user_id = users.id")
        assert conds == [{"left_table": "orders", "left_column": "user_id",
                          "right_table": "users", "right_column": "id"}]
        # 多列 AND + 空格容错
        conds2 = parse_on_conditions(
            self._c(), "orders", "users",
            "orders.user_id = users.id AND orders.city = users.city")
        assert len(conds2) == 2

    def test_reversed_side_normalized(self):
        # 反向书写: left 归一化到 from 一侧
        conds = parse_on_conditions(
            self._c(), "orders", "users", "users.id = orders.user_id")
        assert conds[0]["left_table"] == "orders"
        assert conds[0]["left_column"] == "user_id"

    def test_quoted_style(self):
        conds = parse_on_conditions(
            self._c(), "orders", "users", '`orders`."user_id" = users.id')
        assert conds[0]["left_column"] == "user_id"

    def test_malformed(self):
        with pytest.raises(GraphEditError) as e:
            parse_on_conditions(self._c(), "orders", "users", "随便写写")
        assert e.value.status == 422

    def test_foreign_table_rejected(self):
        with pytest.raises(GraphEditError) as e:
            parse_on_conditions(
                self._c(), "orders", "users", "orders.user_id = other.id")
        assert "other" in e.value.message

    def test_missing_column_rejected(self):
        with pytest.raises(GraphEditError) as e:
            parse_on_conditions(
                self._c(), "orders", "users", "orders.no_col = users.id")
        assert "no_col" in e.value.message

    def test_same_table_both_sides_rejected(self):
        with pytest.raises(GraphEditError):
            parse_on_conditions(
                self._c(), "orders", "users", "orders.id = orders.user_id")

    def test_roundtrip_with_conditions_to_on(self):
        on = "orders.user_id = users.id AND orders.city = users.city"
        conds = parse_on_conditions(self._c(), "orders", "users", on)
        assert conditions_to_on(conds) == on


# ══ 新增关系 ═════════════════════════════════════════════════

class TestAddRelationship:
    def test_add_success_version_increments(self, db, ds):
        from domains.chatbi import semantic
        r = add_relationship(
            db, ds, from_table="orders", target_table="users",
            join_type="LEFT", on="orders.user_id = users.id",
            cardinality="N:1")
        assert r["version"] == 2          # scan v1 → manual v2
        assert r["index_rebuilt"] is True  # 无 rebuilder = 视为不需要
        content = semantic.load_current_content(db, ds)
        rel = content.models[0].relationships[0]
        assert rel.target_model == "users" and rel.source == "manual"
        assert rel.on == "orders.user_id = users.id"

    def test_source_table_missing_404(self, db, ds):
        with pytest.raises(GraphEditError) as e:
            add_relationship(db, ds, from_table="nope", target_table="users",
                             join_type="LEFT", on="nope.id = users.id",
                             cardinality="N:1")
        assert e.value.status == 404 and "源表" in e.value.message

    def test_target_table_missing_404(self, db, ds):
        # 复核报告: 此前只校验源表, 不存在的目标表可写入语义层
        with pytest.raises(GraphEditError) as e:
            add_relationship(db, ds, from_table="orders", target_table="ghost",
                             join_type="LEFT", on="orders.user_id = ghost.id",
                             cardinality="N:1")
        assert e.value.status == 404 and "目标表" in e.value.message

    def test_self_loop_422(self, db, ds):
        with pytest.raises(GraphEditError) as e:
            add_relationship(db, ds, from_table="orders", target_table="orders",
                             join_type="LEFT", on="orders.id = orders.user_id",
                             cardinality="1:1")
        assert e.value.status == 422

    def test_duplicate_409_including_reversed_on(self, db, ds):
        add_relationship(db, ds, from_table="orders", target_table="users",
                         join_type="LEFT", on="orders.user_id = users.id",
                         cardinality="N:1")
        # 完全相同
        with pytest.raises(GraphEditError) as e:
            add_relationship(db, ds, from_table="orders", target_table="users",
                             join_type="INNER", on="orders.user_id = users.id",
                             cardinality="N:1")
        assert e.value.status == 409
        # 反向书写的同列对也算重复(方向无关)
        with pytest.raises(GraphEditError) as e2:
            add_relationship(db, ds, from_table="orders", target_table="users",
                             join_type="LEFT", on="users.id = orders.user_id",
                             cardinality="N:1")
        assert e2.value.status == 409

    def test_invalid_on_422(self, db, ds):
        with pytest.raises(GraphEditError) as e:
            add_relationship(db, ds, from_table="orders", target_table="users",
                             join_type="LEFT", on="orders.ghost = users.id",
                             cardinality="N:1")
        assert e.value.status == 422 and "ghost" in e.value.message

    def test_index_rebuild_failure_reported_not_raised(self, db, ds):
        # 重建回调抛错 → 降级标志, 不抛异常不谎报成功
        def _boom():
            raise RuntimeError("vector store down")
        r = add_relationship(
            db, ds, from_table="orders", target_table="users",
            join_type="LEFT", on="orders.user_id = users.id",
            cardinality="N:1", index_rebuilder=_boom)
        assert r["index_rebuilt"] is False
        assert "vector store down" in r["warning"]
        # 版本仍然落库成功
        from domains.chatbi import semantic
        assert semantic.load_current_content(db, ds).models[0].relationships


# ══ 删除关系 ═════════════════════════════════════════════════

class TestDeleteRelationship:
    def _seed_reverse(self, content: SemanticModelContent):
        """在 users 侧补一条反向关系(源 ChatBI 双向标注形态)。"""
        users = next(m for m in content.models if m.name == "users")
        users.relationships.append(Relationship(
            name="rel_users_orders", target_model="orders", join_type="LEFT",
            on="users.id = orders.user_id", type="1:N",
            source="fk", confidence=0.9))

    def test_delete_forward_and_reverse_together(self, db, ds):
        from domains.chatbi import semantic
        content = semantic.load_current_content(db, ds)
        orders = next(m for m in content.models if m.name == "orders")
        orders.relationships.append(Relationship(
            name="rel_o_u", target_model="users", join_type="LEFT",
            on="orders.user_id = users.id", type="N:1", source="manual",
            confidence=1.0))
        self._seed_reverse(content)
        semantic.save_content(db, ds, content, source="manual")

        r = delete_relationship(db, ds, from_table="orders", target_table="users",
                                on="orders.user_id = users.id")
        assert r["removed_forward"] == 1 and r["removed_reverse"] == 1
        assert r["version"] >= 3
        after = semantic.load_current_content(db, ds)
        assert all(not rel for rel in (m.relationships for m in after.models))

    def test_reverse_written_on_matched(self, db, ds):
        # 删除时 ON 用反向书写形式, 正向(标准书写)关系也应被匹配删除
        from domains.chatbi import semantic
        content = semantic.load_current_content(db, ds)
        orders = next(m for m in content.models if m.name == "orders")
        orders.relationships.append(Relationship(
            name="rel_o_u", target_model="users", join_type="LEFT",
            on="orders.user_id = users.id", type="N:1", source="manual",
            confidence=1.0))
        semantic.save_content(db, ds, content, source="manual")
        r = delete_relationship(db, ds, from_table="orders", target_table="users",
                                on="users.id = orders.user_id")
        assert r["removed_forward"] == 1

    def test_no_match_404(self, db, ds):
        with pytest.raises(GraphEditError) as e:
            delete_relationship(db, ds, from_table="orders", target_table="users")
        assert e.value.status == 404

    def test_delete_without_on_removes_all_directions(self, db, ds):
        from domains.chatbi import semantic
        content = semantic.load_current_content(db, ds)
        orders = next(m for m in content.models if m.name == "orders")
        orders.relationships.append(Relationship(
            name="r1", target_model="users", join_type="LEFT",
            on="orders.user_id = users.id", type="N:1", source="manual",
            confidence=1.0))
        orders.relationships.append(Relationship(
            name="r2", target_model="users", join_type="LEFT",
            on="orders.city = users.city", type="N:1", source="manual",
            confidence=1.0))
        semantic.save_content(db, ds, content, source="manual")
        r = delete_relationship(db, ds, from_table="orders", target_table="users")
        assert r["removed_forward"] == 2


# ══ 请求模型 Literal 校验 ════════════════════════════════════

class TestRequestModelLiteral:
    def test_invalid_join_type_rejected(self):
        from domains.chatbi.api import GraphRelationshipIn
        with pytest.raises(ValidationError):
            GraphRelationshipIn(from_table="a", target_table="b",
                                join_type="CROSS", on="a.x = b.y")

    def test_invalid_cardinality_rejected(self):
        from domains.chatbi.api import GraphRelationshipIn
        with pytest.raises(ValidationError):
            GraphRelationshipIn(from_table="a", target_table="b",
                                cardinality="M:N", on="a.x = b.y")

    def test_valid_defaults(self):
        from domains.chatbi.api import GraphRelationshipIn
        m = GraphRelationshipIn(from_table="a", target_table="b", on="a.x = b.y")
        assert m.join_type == "LEFT" and m.cardinality == "N:1"
