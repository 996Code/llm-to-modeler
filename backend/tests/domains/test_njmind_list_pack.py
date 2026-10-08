"""njmind_list pack 骨架装配测试（B2）——registry 装配/自动发现/上游 fail-closed/路由。"""
import pytest


def test_pack_装配_registry含list_chat与generate_list():
    from domains.njmind_list.pack import create_registry
    names = [t.name for t in create_registry().all()]
    assert names[0] == "list_chat"
    assert "list_chat" in names and "chat" not in names
    assert "generate_list" in names


def test_pack_自动发现():
    from domains import scan_pack_dirs
    assert "njmind_list" in scan_pack_dirs()


def test_upstream_validate_上游失败_fail_closed():
    class FakeT:
        def post(self, service, path, json_body=None, auth=False):
            return None, "timeout"
    from domains.njmind_list.upstream import ListModelerAPI
    r = ListModelerAPI(FakeT()).validate({}, [])
    assert r["pass"] is False and "上游" in r["errors"][0]["message"]


class _FakeLlm:
    """路由用 fake LLM：chat_json 固定返回 generate_list（测试不真调 LLM）。"""

    def __init__(self, tool_name):
        self._tool = tool_name

    def chat_json(self, messages, conv_id=None, stage=None, **kw):
        return {"tool": self._tool}


def test_router_路由到generate_list():
    from domains.njmind_list.pack import create_router
    router = create_router()
    name = router.route("生成设备台账列表", None, history="",
                        llm_client=_FakeLlm("generate_list"))
    assert name == ("generate_list", None)


def test_router_无llm降级到list_chat兜底():
    """无 llm_client 时 DefaultPackRouter 退化为首个注册工具（SDK 契约）。

    pack 注册顺序把 ChatTool 放最前——降级场景落到无害闲聊，
    不误触发生成管线（它会烧上游调用且需要宿主上下文）。"""
    from domains.njmind_list.pack import create_router
    router = create_router()
    assert router.route("你好", None, history="", llm_client=None) == ("list_chat", 1.0)


def test_manifest_端点表与服务声明():
    from domains import load_pack_configs
    cfg = load_pack_configs(pack_names=["njmind_list"])["njmind_list"]
    assert "njmind-modeler" in cfg["services"]
    for key, expected in {
        "tables": "/api/mcp/listconfig/tables",
        "guide": "/api/mcp/listconfig/guide",
        "templates_list": "/api/mcp/listconfig/templates",
        "template": "/api/mcp/listconfig/templates/{name}",
        "validate": "/api/mcp/listconfig/validate",
    }.items():
        assert cfg["paths"][key] == expected, f"paths.{key}"


def test_manifest_非兜底域且声明list_config制品():
    from domains import load_pack_configs
    cfg = load_pack_configs(pack_names=["njmind_list"])["njmind_list"]
    assert cfg["domain"]["fallback"] is False
    assert "fallback_tool" not in cfg["domain"]
    artifact = cfg["artifact"]
    assert artifact["type"] == "list-config"
    assert set(artifact["actions"]) == {"view_json", "apply", "rewind"}
    # I4（跨仓审查移交）：按钮身份键与 artifact.config 路径对齐
    assert artifact["identity"] == {
        "tableShowFields": "fieldTitleKey",
        "rowButtons": "buttonId",
        "buttonGroupConfig.buttonConfigList": "buttonId",
    }


def test_get_guide_拼query参数():
    from unittest.mock import MagicMock
    from domains.njmind_list.upstream import ListModelerAPI
    t = MagicMock()
    t.get.return_value = {"fieldCatalog": []}
    api = ListModelerAPI(t)
    api.get_guide("t1", part_table_code="p1", main_table_field=("a", "b"))
    t.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/listconfig/guide",
        auth=False, params={"tableCode": "t1", "partTableCode": "p1",
                            "mainTableField": "a,b"})


def test_get_guide_仅必填tableCode():
    from unittest.mock import MagicMock
    from domains.njmind_list.upstream import ListModelerAPI
    t = MagicMock()
    t.get.return_value = {"fieldCatalog": []}
    ListModelerAPI(t).get_guide("t1")
    t.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/listconfig/guide",
        auth=False, params={"tableCode": "t1"})


def test_list_tables_匿名GETtables端点():
    from unittest.mock import MagicMock
    from domains.njmind_list.upstream import ListModelerAPI
    t = MagicMock()
    t.get.return_value = [{"code": "t1", "name": "表一"}]
    r = ListModelerAPI(t).list_tables()
    t.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/listconfig/tables", auth=False)
    assert r == [{"code": "t1", "name": "表一"}]


def test_get_template_补json后缀():
    from unittest.mock import MagicMock
    from domains.njmind_list.upstream import ListModelerAPI
    t = MagicMock()
    ListModelerAPI(t).get_template("column-templates")
    t.get.assert_called_once_with(
        "njmind-modeler", "/api/mcp/listconfig/templates/column-templates.json",
        auth=False, cache=True)


def test_generate_list_六步管线已实装():
    from domains.njmind_list.tools.generate_list import GenerateListTool
    from sdk.tool import CompositeTool, ToolContext
    tool = GenerateListTool()
    assert isinstance(tool, CompositeTool)
    assert tool.name == "generate_list"
    assert tool.steps == ["fetch_guide", "fetch_existing", "parse_intents",
                          "fetch_templates", "generate", "validate"]
    assert [s["key"] for s in tool.pipeline_steps] == tool.steps
    ctx = ToolContext(llm_client=None, asset_client=None, conversation=None,
                      emit=lambda *a, **k: None)
    # fetch_guide 未装配 api → fail-closed（占位 NotImplementedError 已被 B5 替换）
    import pytest as _pytest
    with _pytest.raises(RuntimeError):
        tool.execute({}, ctx)


def test_service_locator_装配与重置():
    from domains.njmind_list import service_locator
    service_locator.wire_transport(object())
    assert service_locator.get_api() is not None
    service_locator.reset_list_transport()
    with pytest.raises(RuntimeError):
        service_locator.get_api()
