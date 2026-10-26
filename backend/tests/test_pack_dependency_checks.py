"""插件间依赖(通用机制)单测:manifest 声明 pack: 依赖 + 反向依赖扫描。

平台零插件名知识——本文件只用假 manifest 验证机制本身,不绑定任何具体插件
(metrology 的声明存在性除外,那属于插件自己的配置)。
"""
from services.pack_dependency import (
    STATUS_MISSING, STATUS_OK, evaluate_pack, evaluate_pack_dependency,
    pack_dependents,
)


def _pack_dep():
    return {"pack": "some_pack", "required": True}


def test_pack_dependency_ok_when_target_enabled():
    r = evaluate_pack_dependency("a", "some_pack", _pack_dep(), {}, None,
                                 use_probe=False,
                                 pack_enabled=lambda n: n == "some_pack")
    assert r["status"] == STATUS_OK


def test_pack_dependency_missing_when_target_disabled():
    r = evaluate_pack_dependency("a", "some_pack", _pack_dep(), {}, None,
                                 use_probe=False, pack_enabled=lambda n: False)
    assert r["status"] == STATUS_MISSING
    assert r["missing"] == ["插件 some_pack 未启用"]


def test_pack_dependency_unchecked_passes_with_detail():
    """调用方未注入启用判定 → 不误伤(fail-open + 留痕),不让缺状态存储
    的调用路径(旧测试/离线工具)把所有插件拦成依赖缺失。"""
    r = evaluate_pack_dependency("a", "some_pack", _pack_dep(), {}, None,
                                 use_probe=False, pack_enabled=None)
    assert r["status"] == STATUS_OK
    assert "未检查" in r["detail"]


def test_evaluate_pack_aggregates_pack_and_infra_deps():
    manifest = {
        "dependencies": {
            "kg": {"pack": "knowledge_graph", "required": True},
            "optional_dep": {"pack": "chatbi", "required": False},
        }
    }
    enabled = {"knowledge_graph", "chatbi"}
    r = evaluate_pack("a", manifest, use_probe=False,
                      pack_enabled=lambda n: n in enabled)
    assert r["status"] == STATUS_OK
    assert set(r["dependencies"]) == {"kg", "optional_dep"}
    # 可选依赖禁用:记录但不阻断
    r2 = evaluate_pack("a", manifest, use_probe=False,
                       pack_enabled=lambda n: n == "knowledge_graph")
    assert r2["status"] == STATUS_OK
    assert r2["dependencies"]["optional_dep"]["status"] == STATUS_MISSING
    # 必需依赖禁用:聚合缺失
    r3 = evaluate_pack("a", manifest, use_probe=False,
                       pack_enabled=lambda n: n == "chatbi")
    assert r3["status"] == STATUS_MISSING
    assert "knowledge_graph" in r3["detail"]


def test_pack_dependents_reverse_scan():
    manifests = {
        "a": {"dependencies": {"kg": {"pack": "target"}}},
        "b": {"dependencies": {"infra": {"fields": ["x"]}}},
        "c": {"dependencies": {"t": {"pack": "target", "required": False}}},
        "target": {},
    }
    assert pack_dependents("target", manifests) == ["a", "c"]
    assert pack_dependents("b", manifests) == []
    # 不自依赖
    assert pack_dependents("target", {"target": {"dependencies": {
        "self": {"pack": "target"}}}}) == []


def test_metrology_manifest_declares_pack_dependencies():
    """计量插件如实声明对知识库/智能问数的插件间依赖(通用机制的使用方)。"""
    from domains import load_pack_configs
    deps = (load_pack_configs(pack_names=["metrology_assistant"])
            .get("metrology_assistant") or {}).get("dependencies") or {}
    declared = {d.get("pack") for d in deps.values() if isinstance(d, dict)}
    assert {"knowledge_graph", "chatbi"} <= declared
