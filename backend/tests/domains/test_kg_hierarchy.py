"""知识库层级树（用户 10-15 需求:左侧需要是有层级的树）——目录+子库+树查询。

- kind 列: 'kb'(默认,存量兼容) | 'folder'(目录,不存文档只做分组)
- parent_id: NULL=顶层;指向 folder id=子项
- 循环引用防护:parent 必须是 folder 且不得形成环
"""
import pytest

from domains.knowledge_graph.store import KGStore


class TestKbHierarchy:

    def test_create_folder(self):
        s = KGStore()
        f = s.create_kb("法务目录", kind="folder")
        assert f["kind"] == "folder"
        assert f["parentId"] is None

    def test_create_kb_under_folder(self):
        s = KGStore()
        f = s.create_kb("目录甲", kind="folder")
        kb = s.create_kb("劳动法库", parent_id=f["id"])
        assert kb["parentId"] == f["id"]
        assert kb["kind"] == "kb"

    def test_kb_tree_returns_nested(self):
        s = KGStore()
        f = s.create_kb("树目录", kind="folder")
        s.create_kb("子库一", parent_id=f["id"])
        s.create_kb("子库二", parent_id=f["id"])
        s.create_kb("顶层散库")
        tree = s.list_kb_tree()
        # 顶层含目录与散库;目录下挂两个子库
        top_names = {t["name"] for t in tree}
        assert "树目录" in top_names and "顶层散库" in top_names
        folder = next(t for t in tree if t["name"] == "树目录")
        assert {c["name"] for c in folder["children"]} == {"子库一", "子库二"}

    def test_parent_must_be_folder(self):
        s = KGStore()
        kb = s.create_kb("普通库")
        with pytest.raises(ValueError, match="folder"):
            s.create_kb("挂错爹", parent_id=kb["id"])

    def test_cycle_rejected(self):
        s = KGStore()
        f1 = s.create_kb("目录一", kind="folder")
        f2 = s.create_kb("目录二", kind="folder", parent_id=f1["id"])
        # 把 f1 挪到 f2 下面 → 环
        with pytest.raises(ValueError, match="环|cycle"):
            s.move_kb(f1["id"], parent_id=f2["id"])

    def test_move_kb_between_folders(self):
        s = KGStore()
        fa = s.create_kb("目录A", kind="folder")
        fb = s.create_kb("目录B", kind="folder")
        kb = s.create_kb("搬家库", parent_id=fa["id"])
        s.move_kb(kb["id"], parent_id=fb["id"])
        assert s.get_kb(kb["id"])["parentId"] == fb["id"]

    def test_legacy_kb_defaults(self):
        """存量库无 kind/parent_id → kind='kb', parentId=None(兼容)。"""
        s = KGStore()
        kb = s.create_kb("存量风格库")   # 不传新参
        assert kb["kind"] == "kb" and kb["parentId"] is None
