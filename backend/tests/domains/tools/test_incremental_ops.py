"""增量指令集测试：目录构建 + 确定性合并器（纯逻辑，零 LLM/零网络）。"""
import pytest

from domains.njmind_form.tools._incremental_ops import (
    build_catalog, apply_ops, format_failures,
)


def _config():
    """最小可用表单：3 字段 + 2 组按钮，字段结构带真实键名。"""
    return {
        "formName": "请假表单",
        "formTitle": "$xingming$",
        "formColumnsNumber": 4,
        "serverKey": "/njmind-modeler",
        "formFieldConfigVos": [
            {"fieldTitleKey": "xingming", "fieldTitleText": "姓名",
             "formFieldType": 0, "isRequiredField": 1, "fieldWidth": 12,
             "fieldConditionDisplays": [], "isShowFieldTitle": 1},
            {"fieldTitleKey": "zhuangtai", "fieldTitleText": "状态",
             "formFieldType": 4, "isRequiredField": 0,
             "optionSettings": {
                 "optionFields": [{"optionLabel": "待审", "optionValue": 1},
                                  {"optionLabel": "已审", "optionValue": 2}],
                 "optionalRangeType": 0},
             "selectMode": 1, "fieldWidth": 12},
            {"fieldTitleKey": "kaishiriqi", "fieldTitleText": "开始日期",
             "formFieldType": 2, "isRequiredField": 1, "fieldWidth": 12},
        ],
        "topButtons": [{"buttonName": "返回", "buttonType": "primary"}],
        "bottomButtons": [{"buttonName": "保存", "buttonType": "primary"},
                          {"buttonName": "提交", "buttonType": "primary"}],
    }


class TestBuildCatalog:
    def test_catalog_lines_and_keys(self):
        c = build_catalog(_config(), type_names={0: "TEXT", 4: "SELECT", 2: "DATE"})
        lines = c.text.splitlines()
        assert lines[0].startswith("## 字段目录")
        assert any("xingming | 姓名 | TEXT | 必填" in l for l in lines)
        assert any("zhuangtai | 状态 | SELECT | 可选 | 选项:" in l
                   and "待审" in l and "optionalRangeType" in l for l in lines)
        assert c.keys == ["xingming", "zhuangtai", "kaishiriqi"]
        # 表单属性 + 按钮清单
        assert any("表单属性:" in l and "formColumnsNumber" in l for l in lines)
        assert any("按钮:" in l and "保存" in l for l in lines)

    def test_catalog_size_far_below_full_config(self):
        import json
        cfg = _config()
        c = build_catalog(cfg, {0: "TEXT", 4: "SELECT", 2: "DATE"})
        assert len(c.text) < len(json.dumps(cfg, ensure_ascii=False)) / 2


class TestUpdateField:
    def test_update_by_key_shallow_merge(self):
        ops = [{"op": "update_field", "key": "xingming",
                "patch": {"isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        f = ar.new_config["formFieldConfigVos"][0]
        assert f["isRequiredField"] == 0          # patch 生效
        assert f["fieldTitleText"] == "姓名"       # 未提及的属性保留
        assert f["fieldWidth"] == 12

    def test_update_option_settings_full_replacement(self):
        """选项修改：optionSettings 整体替换（真实结构 optionFields）。"""
        new_os = {"optionFields": [{"optionLabel": "待审", "optionValue": 1},
                                   {"optionLabel": "已审", "optionValue": 2},
                                   {"optionLabel": "已归档", "optionValue": 3}],
                  "optionalRangeType": 0}
        ops = [{"op": "update_field", "key": "zhuangtai",
                "patch": {"optionSettings": new_os}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        assert ar.new_config["formFieldConfigVos"][1]["optionSettings"] == new_os

    def test_title_fallback_anchor(self):
        """key 抄错时降级 title 精确匹配（双锚）。"""
        ops = [{"op": "update_field", "key": "xming", "title": "姓名",
                "patch": {"isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        assert ar.new_config["formFieldConfigVos"][0]["isRequiredField"] == 0

    def test_anchor_miss_reports_and_keeps_original(self):
        ops = [{"op": "update_field", "key": "bucunzai", "patch": {"isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert not ar.ok
        assert ar.new_config is None
        assert any("锚点未命中" in f for f in ar.failures)


class TestAddField:
    def test_add_with_same_type_clone_skeleton(self):
        """新增 DATE 字段：克隆表内已有 DATE 字段的结构。"""
        ops = [{"op": "add_field", "after": "kaishiriqi",
                "field": {"fieldTitleKey": "jieshuriqi", "fieldTitleText": "结束日期",
                          "formFieldType": 2, "isRequiredField": 1}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        fields = ar.new_config["formFieldConfigVos"]
        assert len(fields) == 4
        new = fields[3]
        assert new["fieldTitleKey"] == "jieshuriqi"
        assert new["fieldTitleText"] == "结束日期"
        # 骨架继承：同类型字段的非身份属性带过来
        assert new["fieldWidth"] == 12
        # 身份属性不继承：默认值是旧字段的业务语义，必须剥掉
        assert "fieldDefaultValue" not in new

    def test_add_duplicate_key_fails(self):
        ops = [{"op": "add_field",
                "field": {"fieldTitleKey": "xingming", "fieldTitleText": "姓名2",
                          "formFieldType": 0, "isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert not ar.ok
        assert any("重复" in f for f in ar.failures)

    def test_add_no_same_type_no_loader_fails(self):
        """无同类型字段且无模板加载器 → fail-closed（不静默造结构）。"""
        ops = [{"op": "add_field",
                "field": {"fieldTitleKey": "fujian", "fieldTitleText": "附件",
                          "formFieldType": 3, "isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert not ar.ok
        assert any("克隆" in f or "模板" in f for f in ar.failures)

    def test_add_falls_back_to_template_loader(self):
        ops = [{"op": "add_field",
                "field": {"fieldTitleKey": "fujian", "fieldTitleText": "附件",
                          "formFieldType": 3, "isRequiredField": 0}}]
        loader = lambda code: {"fieldTitleKey": "", "formFieldType": 3,
                               "isShowFieldTitle": 1, "fieldWidth": 6}
        ar = apply_ops(_config(), ops, template_loader=loader)
        assert ar.ok
        new = ar.new_config["formFieldConfigVos"][3]
        assert new["fieldTitleKey"] == "fujian"
        assert new["fieldWidth"] == 6  # 模板骨架 + LLM 属性覆盖


class TestIdentifierProtection:
    """【硬约束】数据库标识禁改：字段 fieldTitleKey / 表单 formCode。"""

    def test_update_field_cannot_rename_key(self):
        """update_field 试图改 fieldTitleKey → 静默剥离，key 保持原值。"""
        ops = [{"op": "update_field", "key": "xingming",
                "patch": {"fieldTitleKey": "yuangong", "fieldTitleText": "员工"}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        f = ar.new_config["formFieldConfigVos"][0]
        assert f["fieldTitleKey"] == "xingming"      # key 未被改
        assert f["fieldTitleText"] == "员工"           # 中文名正常改
        assert any("禁止修改" in a for a in ar.applied)  # 剥离留痕

    def test_update_field_key_echo_allowed(self):
        """patch 里原样回显 key（未改）→ 不剥离不报错。"""
        ops = [{"op": "update_field", "key": "xingming",
                "patch": {"fieldTitleKey": "xingming", "isRequiredField": 0}}]
        ar = apply_ops(_config(), ops)
        assert ar.ok
        assert ar.new_config["formFieldConfigVos"][0]["isRequiredField"] == 0

    def test_update_form_cannot_touch_form_code(self):
        """update_form 改 formCode → 白名单拒绝（增量层防线）。"""
        ops = [{"op": "update_form", "patch": {"formCode": "hack"}}]
        ar = apply_ops(_config(), ops)
        assert not ar.ok
        assert any("系统属性" in f for f in ar.failures)


class TestRemoveAndFormAndButton:
    def test_remove_field(self):
        ar = apply_ops(_config(), [{"op": "remove_field", "key": "zhuangtai"}])
        assert ar.ok
        keys = [f["fieldTitleKey"] for f in ar.new_config["formFieldConfigVos"]]
        assert keys == ["xingming", "kaishiriqi"]

    def test_update_form_allowed_prop(self):
        ar = apply_ops(_config(), [{"op": "update_form",
                                    "patch": {"formColumnsNumber": 2}}])
        assert ar.ok
        assert ar.new_config["formColumnsNumber"] == 2

    def test_update_form_rejects_system_props(self):
        """系统属性（formConfigId 等）禁止改——目录没展示的就不该动。"""
        ar = apply_ops(_config(), [{"op": "update_form",
                                    "patch": {"formConfigId": "hack"}}])
        assert not ar.ok
        assert any("系统属性" in f for f in ar.failures)

    def test_update_button(self):
        ar = apply_ops(_config(), [{"op": "update_button", "name": "提交",
                                    "patch": {"buttonName": "提交并通知"}}])
        assert ar.ok
        names = [b["buttonName"] for b in ar.new_config["bottomButtons"]]
        assert "提交并通知" in names

    def test_update_button_missing(self):
        ar = apply_ops(_config(), [{"op": "update_button", "name": "不存在的按钮",
                                    "patch": {"buttonName": "x"}}])
        assert not ar.ok


class TestRestoreUntouched:
    """postprocess 归一化不得影响未提及字段（真实反馈：改 A 误伤 B 的观感来源）。"""

    def test_untouched_field_byte_identical(self):
        from domains.njmind_form.tools._incremental_ops import restore_untouched
        from domains.njmind_form.tools._postprocess import postprocess_config
        import json as _json
        cfg = _config()
        # B（zhuangtai）带 null 属性 + 布尔 + 前端字段，指令只改 A（xingming）
        cfg["formFieldConfigVos"][1].update(
            {"fieldDefaultValue": None, "someFlag": True, "intro": "x"})
        b_before = _json.dumps(cfg["formFieldConfigVos"][1], ensure_ascii=False,
                               sort_keys=True)
        ops = [{"op": "update_field", "key": "xingming",
                "patch": {"isRequiredField": 0}}]
        ar = apply_ops(cfg, ops)
        assert ar.ok and ar.touched_keys == {"xingming"}
        merged = restore_untouched(postprocess_config(ar.new_config), cfg, ar)
        b_after = _json.dumps(
            merged["formFieldConfigVos"][1], ensure_ascii=False, sort_keys=True)
        assert b_after == b_before  # B 逐字节不变（null/布尔/intro 原样保留）
        # A 的修改生效
        assert merged["formFieldConfigVos"][0]["isRequiredField"] == 0

    def test_untouched_toplevel_keys_restored(self):
        from domains.njmind_form.tools._incremental_ops import restore_untouched
        from domains.njmind_form.tools._postprocess import postprocess_config
        cfg = _config()
        cfg["someTopNull"] = None          # 未提及的顶层键带 null
        ops = [{"op": "update_field", "key": "xingming",
                "patch": {"isRequiredField": 0}}]
        ar = apply_ops(cfg, ops)
        merged = restore_untouched(postprocess_config(ar.new_config), cfg, ar)
        assert merged.get("someTopNull") is None   # postprocess 会剥 null，还原后保留

    def test_touched_add_field_keeps_postprocess(self):
        """add 的新字段不受还原影响（不在基线里）。"""
        from domains.njmind_form.tools._incremental_ops import restore_untouched
        from domains.njmind_form.tools._postprocess import postprocess_config
        cfg = _config()
        ops = [{"op": "add_field", "after": "kaishiriqi",
                "field": {"fieldTitleKey": "jieshuriqi", "fieldTitleText": "结束日期",
                          "formFieldType": 2, "isRequiredField": True}}]
        ar = apply_ops(cfg, ops)
        assert ar.ok and ar.touched_keys == {"jieshuriqi"}
        merged = restore_untouched(postprocess_config(ar.new_config), cfg, ar)
        keys = [f["fieldTitleKey"] for f in merged["formFieldConfigVos"]]
        assert keys == ["xingming", "zhuangtai", "kaishiriqi", "jieshuriqi"]
        # 新字段的 postprocess 归一化保留（布尔→1）
        assert merged["formFieldConfigVos"][3]["isRequiredField"] == 1


class TestFixableFieldErrors:
    """校验机械修复：必填缺失/值域不合法的解析与三级来源抄值。"""

    def test_parse_missing_and_invalid(self):
        from domains.njmind_form.tools._postprocess import parse_fixable_field_errors
        errs = [
            {"message": "D: baoxiaoren.selectMode 为必填项(字段类型=USER)"},
            {"message": "D: seg.displayStyle=0 不合法,类型SEGMENT 允许值=[1]"},
        ]
        fx = parse_fixable_field_errors(errs)
        assert ("baoxiaoren", "selectMode", "USER", None) in fx
        assert ("seg", "displayStyle", "SEGMENT", ["1"]) in fx

    def test_fill_from_same_type_sibling(self):
        from domains.njmind_form.tools._postprocess import fill_missing_required
        cfg = {"formFieldConfigVos": [
            {"fieldTitleKey": "a", "formFieldType": 7},
            {"fieldTitleKey": "b", "formFieldType": 7, "selectMode": 2},
        ]}
        fx = [("a", "selectMode", "USER", None)]
        assert fill_missing_required(cfg, fx) is True
        assert cfg["formFieldConfigVos"][0]["selectMode"] == 2  # 抄兄弟字段

    def test_fill_skips_self_on_invalid_value(self):
        """值域错误不得把自己的错值当来源（回归：displayStyle=0 抄回 0）。"""
        from domains.njmind_form.tools._postprocess import fill_missing_required
        cfg = {"formFieldConfigVos": [
            {"fieldTitleKey": "seg", "formFieldType": 12, "displayStyle": 0},
        ]}
        fx = [("seg", "displayStyle", "SEGMENT", ["1"])]
        assert fill_missing_required(cfg, fx) is True
        assert cfg["formFieldConfigVos"][0]["displayStyle"] == 1  # 允许值兜底

    def test_fill_from_prop_defaults(self):
        from domains.njmind_form.tools._postprocess import fill_missing_required
        cfg = {"formFieldConfigVos": [
            {"fieldTitleKey": "dept", "formFieldType": 6},
        ]}
        fx = [("dept", "selectMode", "DEPARTMENT", None)]
        ok = fill_missing_required(cfg, fx, prop_defaults={6: {"selectMode": 1}})
        assert ok and cfg["formFieldConfigVos"][0]["selectMode"] == 1

    def test_no_source_no_guess(self):
        """四级来源都没有 → 不猜值（留给 LLM 重试路径）。"""
        from domains.njmind_form.tools._postprocess import fill_missing_required
        cfg = {"formFieldConfigVos": [{"fieldTitleKey": "x", "formFieldType": 9}]}
        fx = [("x", "mystery", "CHILD_FORM", None)]
        assert fill_missing_required(cfg, fx) is False
        assert "mystery" not in cfg["formFieldConfigVos"][0]


class TestAtomicityAndFullRewrite:
    def test_partial_failure_is_all_or_nothing(self):
        """第 2 条锚点失败 → 第 1 条的效果也不可见（原子性）。"""
        ops = [
            {"op": "update_field", "key": "xingming", "patch": {"isRequiredField": 0}},
            {"op": "remove_field", "key": "bucunzai"},
        ]
        cfg = _config()
        ar = apply_ops(cfg, ops)
        assert not ar.ok
        assert ar.new_config is None
        # 原对象未被就地污染
        assert cfg["formFieldConfigVos"][0]["isRequiredField"] == 1

    def test_full_rewrite_flag(self):
        ar = apply_ops(_config(), [{"op": "full_rewrite", "reason": "重新分组"}])
        assert ar.full_rewrite
        assert not ar.ok

    def test_unknown_op_fails(self):
        ar = apply_ops(_config(), [{"op": "magic", "key": "xingming"}])
        assert not ar.ok
        assert any("未知指令" in f for f in ar.failures)

    def test_empty_ops_fail(self):
        ar = apply_ops(_config(), [])
        assert not ar.ok

    def test_format_failures_includes_keys(self):
        msg = format_failures(["#1 锚点未命中"], ["a", "b"])
        assert "锚点未命中" in msg and "a,b" in msg
