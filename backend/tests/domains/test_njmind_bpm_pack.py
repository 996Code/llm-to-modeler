"""njmind_bpm pack 可发现性与 manifest 契约测试。"""
import pytest


def test_njmind_bpm_pack_is_discoverable():
    from domains import scan_pack_dirs, load_pack_configs
    assert "njmind_bpm" in scan_pack_dirs()
    configs = load_pack_configs(pack_names=["njmind_bpm"])
    cfg = configs.get("njmind_bpm")
    assert cfg, "njmind_bpm config.yaml 未被加载"


def test_manifest_declares_bpm_process_artifact():
    from domains import load_pack_configs
    cfg = load_pack_configs(pack_names=["njmind_bpm"])["njmind_bpm"]
    artifact = cfg["artifact"]
    assert artifact["type"] == "bpm-process"
    assert set(artifact["actions"]) == {"view_json", "apply", "rewind"}
    assert artifact["identity"]["approveRules"] == "taskId"
    assert artifact["identity"]["nodes"] == "key"


def test_manifest_is_not_fallback_domain():
    from domains import load_pack_configs
    cfg = load_pack_configs(pack_names=["njmind_bpm"])["njmind_bpm"]
    assert cfg["domain"]["fallback"] is False


def test_manifest_declares_no_fallback_tool():
    """fallback_tool 按全局合并 registry 解析且跨 pack 迭代序取先到者——
    双声明有遮蔽隐患;全局兜底由 njmind_form 覆盖,本 pack 不得声明。"""
    cfg = load_pack_configs_for_bpm()
    assert "fallback_tool" not in cfg["domain"]


def test_node_types_section_renders_from_guide_not_static():
    """#2 漂移守卫:node_types.j2 的枚举/节点来自 guide 变量(编译期 SSoT),
    静态兜底仅在 guide 缺失时出现。"""
    from sdk.prompt_loader import PromptLoader
    from pathlib import Path
    loader = PromptLoader(packs_root=Path(
        __file__).resolve().parents[2] / "src" / "domains")
    guide = {
        "nodeTypes": [{"code": "USER_TASK", "description": "人工审批", "rule": "x"}],
        "enumMappings": [{"group": "approveMethod", "name": "AND_ALL",
                          "code": 20, "description": "会签", "keywords": []}],
        "fieldOperatorCompatibility": {"NUMBER": ["GREATER_THAN"]},
    }
    out = loader.render("njmind_bpm", "parse", guide=guide)
    assert "人工审批" in out and "approveMethod.AND_ALL = 20" in out
    # guide 存在时不出现静态兜底行
    assert "| CC_TASK |" not in out and "| USER_TASK |" not in out
    # 兜底路径:无 guide 时静态节点表仍在、且不含任何枚举码(防漂移)
    out2 = loader.render("njmind_bpm", "parse", guide={})
    assert "START_EVENT" in out2
    assert "= 10" not in out2 and "= 20" not in out2


def load_pack_configs_for_bpm():
    from domains import load_pack_configs
    return load_pack_configs(pack_names=["njmind_bpm"])["njmind_bpm"]


def test_manifest_declares_upstream_service_and_paths():
    from domains import load_pack_configs
    cfg = load_pack_configs(pack_names=["njmind_bpm"])["njmind_bpm"]
    assert "njmind-modeler" in cfg["services"]
    paths = cfg["paths"]
    for key, expected in {
        "guide": "/api/mcp/bpm/guide",
        "templates_list": "/api/mcp/bpm/templates/list",
        "template": "/api/mcp/bpm/templates/{name}",
        "schema": "/api/mcp/bpm/schemas/{name}",
        "parse": "/api/mcp/bpm/parse",
        "render": "/api/mcp/bpm/render",
        "get_form": "/api/mcp/forms/{code}",
    }.items():
        assert paths[key] == expected, f"paths.{key}"


def test_registry_registers_chat_tool():
    from domains.njmind_bpm.pack import create_registry, create_router
    registry = create_registry()
    tool = registry.get("bpm_chat")
    assert tool is not None
    router = create_router(registry)
    assert router is not None


def test_router_routes_to_only_tool_without_llm():
    """无 llm_client 时 DefaultPackRouter 退化为返回首个注册工具(SDK 契约)。
    pack 注册顺序把 ChatTool 放最前——降级/兜底场景应落到无害闲聊,
    不误触发 generate/update 管线(它们会烧上游调用且需要宿主上下文)。
    route() 返回 (tool_name, confidence) 元组,降级路径 confidence=1.0。"""
    from domains.njmind_bpm.pack import create_router
    router = create_router()
    assert router.route("你好", None, history="", llm_client=None) == ("bpm_chat", 1.0)
