"""平台 MCP 声明翻译器与能力 diff 契约测试。"""
from sdk.mcp_draft import capability_diff, translate_input_schema


def test_translate_required_enum_and_types():
    out = translate_input_schema({
        "type": "object",
        "description": "查询预约",
        "properties": {
            "instrument": {"type": "string", "description": "器具名称"},
            "count": {"type": "integer"},
            "state": {"type": "string", "enum": ["pending", "done"]},
        },
        "required": ["instrument"],
    })
    assert out["required_slot"] == "instrument"
    slots = {x["name"]: x for x in out["slots"]}
    assert slots["instrument"]["required"] is True
    assert slots["count"]["slot_type"] == "number"
    assert slots["state"]["slot_type"] == "enum"
    assert slots["state"]["enum_options"] == ["pending", "done"]


def test_translate_malformed_schema_is_safe_empty():
    assert translate_input_schema(None)["slots"] == []
    assert translate_input_schema({"type": "string"})["required_slot"] is None


def test_capability_diff_new_and_stale_are_deterministic():
    result = capability_diff(
        {"server-b": ["z", "a"], "server-a": ["x"]},
        [("server-b", "z"), ("server-gone", "old")],
    )
    assert result == {
        "new": [("server-a", "x"), ("server-b", "a")],
        "stale": [("server-gone", "old")],
    }
