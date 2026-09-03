"""生产事故回归：'帮我加两个下拉框' 暴露的三个存量脆弱点。

事故链（conv 6b980368，2026-09-03 17:28）：
  LLM 偶发输出字符串类型码 "4" → add_field 无同类型可克隆且 loader 的
  isinstance 挡字符串误报「模板不可用」×3 → 增量耗尽升格全量 →
  modify.j2 渲染时 keyword_hints.j2 对 guide 缺 key 做点访问，StrictUndefined
  直接抛 'dict object' has no attribute 'keywordIndex' → 工具整体失败。

三个修复各配对应用例：模板 get()/类型码归一/异常 guide 降级。
"""
import pytest
from pathlib import Path

from domains.njmind_form.tools._config_loader import sanitize_guide
from domains.njmind_form.tools._incremental_ops import apply_ops
from domains.njmind_form.keys import FIELDS


class TestKeywordHintsMissingKey:
    """真实模板渲染：guide 缺 keywordIndex 不再崩（StrictUndefined 下点访问曾直接抛错）。"""

    @pytest.fixture(scope="class")
    def loader(self):
        from engine.prompt_loader import PromptLoader
        # 真实 pack 模板（backend/src/domains）
        src_root = Path(__file__).resolve().parents[3] / "src"
        return PromptLoader(packs_root=src_root / "domains")

    def test_modify_render_guide_without_keyword_index(self, loader):
        out = loader.render(
            "njmind_form", "modify",
            config={"formCode": "t", FIELDS: []},
            guide={"fieldTypes": [{"code": 4, "name": "SELECT"}]},  # 有 fieldTypes 无 keywordIndex
        )
        assert "修改器" in out
        assert "keywordIndex" not in out  # 缺 key 时该段静默跳过

    def test_parse_render_empty_guide(self, loader):
        out = loader.render("njmind_form", "parse", guide={})
        assert "需求分析器" in out

    def test_parse_render_guide_with_keyword_index(self, loader):
        out = loader.render(
            "njmind_form", "parse",
            guide={"fieldTypes": [], "keywordIndex": {"请假": [1, 2]}},
        )
        assert "请假" in out  # 有数据时正常渲染关键词段


class TestStringTypeCodeNormalized:
    """add_field 的字符串类型码 "4"：归一后走模板/克隆，且产物里是 int。"""

    def test_string_code_uses_template_and_normalizes(self):
        tmpl = {"fieldTitleKey": "tpl", "formFieldType": 4, "stub": True}
        cfg = {"formCode": "t", FIELDS: [
            {"fieldTitleKey": "xingming", "fieldTitleText": "姓名", "formFieldType": 0},
        ]}
        ar = apply_ops(cfg, [{
            "op": "add_field", "after": "xingming",
            "field": {"fieldTitleKey": "xiala1", "fieldTitleText": "下拉1",
                      "formFieldType": "4"},  # 字符串类型码（事故形态）
        }], template_loader=lambda code: dict(tmpl) if code == 4 else None)
        assert ar.ok, ar.failures
        added = [f for f in ar.new_config[FIELDS] if f["fieldTitleKey"] == "xiala1"]
        assert added and added[0]["formFieldType"] == 4  # 产物里必须是 int
        # loader 收到的是归一后的 int（字符串码曾让 isinstance 检查静默失败）

    def test_string_code_clones_same_type(self):
        cfg = {"formCode": "t", FIELDS: [
            {"fieldTitleKey": "a", "fieldTitleText": "旧下拉", "formFieldType": 4},
        ]}
        ar = apply_ops(cfg, [{
            "op": "add_field",
            "field": {"fieldTitleKey": "b", "fieldTitleText": "新下拉",
                      "formFieldType": "4"},
        }], template_loader=lambda code: (_ for _ in ()).throw(AssertionError("应克隆而非拉模板")))
        assert ar.ok
        assert len(ar.new_config[FIELDS]) == 2


class TestSanitizeGuide:
    def test_invalid_guide_downgraded(self):
        """None / 非 dict / 缺 fieldTypes → 空 dict（不再让下游崩）。"""
        assert sanitize_guide(None) == {}
        assert sanitize_guide({"timestamp": "2026", "status": 500}) == {}
        assert sanitize_guide({"fieldTypes": []}) == {}

    def test_valid_guide_passthrough(self):
        g = {"fieldTypes": [{"code": 4, "name": "SELECT"}], "keywordIndex": {}}
        assert sanitize_guide(g) is g
