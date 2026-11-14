"""智能客服展示配置(欢迎页)——schema 数据契约 + meta 覆盖链。

metrology_assistant 挂上 settings.schema.yaml(display 组)后:插件中心
出现"设置"入口,保存的欢迎页标题/副标题/常用问题经 /api/meta/packs
合并进 manifest display,对话欢迎页即时生效(改完即生效,无需部署)。
本文件锁住这条链的数据契约:字段齐、manifest 挂了设置声明、
保存 → meta 可见、清空 → 回落 manifest 默认。
"""

from types import SimpleNamespace

import pytest
from starlette.requests import Request

from api.meta import list_packs
from domains import load_pack_configs
from services.pack_settings import (
    PackSettingsStore,
    read_settings_schema,
    validate_values,
)

PACK = "metrology_assistant"
DISPLAY_KEYS = ("welcome_title", "welcome_subtitle", "welcome_examples")


@pytest.fixture()
def settings_store():
    return PackSettingsStore()


def _request(configs, names, settings_store=None):
    state = SimpleNamespace(
        registry=SimpleNamespace(),
        pack_configs=configs,
        pack_state=SimpleNamespace(enabled_names=lambda: list(configs)),
        settings_store=settings_store,
    )
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/meta/packs",
            "query_string": f"packs={names}".encode(),
            "headers": [],
            "app": SimpleNamespace(state=state),
        }
    )


async def _display(store=None):
    """查 metrology_assistant 在 /api/meta/packs 的 display 合并结果。"""
    result = await list_packs(_request(load_pack_configs([PACK]), PACK, store))
    assert [i["name"] for i in result] == [PACK]
    return result[0]["artifact"]["display"]


async def test_schema_declares_display_group():
    schema = read_settings_schema(PACK)
    assert schema, f"{PACK} 缺 settings.schema.yaml(插件中心不出现设置入口)"
    group = next(g for g in schema["groups"] if g["key"] == "display")
    fields = {f["key"]: f for f in group["fields"]}
    assert set(DISPLAY_KEYS) <= set(fields), f"display 组缺字段: {DISPLAY_KEYS}"
    assert all(fields[k]["type"] == "string" for k in DISPLAY_KEYS)


async def test_manifest_admin_declares_settings_page():
    cfg = load_pack_configs([PACK])[PACK]
    assert cfg.get("admin", {}).get("settings"), (
        "config.yaml admin.settings 未挂(前端 PacksTab 不出现设置按钮)"
    )


async def test_saved_display_settings_override_manifest(settings_store):
    # 未保存:display 无欢迎标题/副标题(前端回落默认"智能助手"),示例取 manifest
    d = await _display()
    assert "welcome_title" not in d
    assert "welcome_subtitle" not in d
    assert isinstance(d["welcome_examples"], list) and d["welcome_examples"]

    # 保存(与设置页同链:validate → save_values)
    values = {
        "welcome_title": "计量智能客服",
        "welcome_subtitle": "计量检测业务问答",
        "welcome_examples": (
            '[{"title":"强检判定","desc":"器具+用途",'
            '"prompt":"压力表用于贸易结算,需要强制检定吗？","icon":"search"}]'
        ),
    }
    clean, errors = validate_values(read_settings_schema(PACK), values)
    assert errors == []
    settings_store.save_values(PACK, clean)

    d = await _display(settings_store)
    assert d["welcome_title"] == "计量智能客服"
    assert d["welcome_subtitle"] == "计量检测业务问答"
    # 设置页保存的是 JSON 字符串,meta 出口已转数组
    assert d["welcome_examples"] == [
        {
            "title": "强检判定",
            "desc": "器具+用途",
            "prompt": "压力表用于贸易结算,需要强制检定吗？",
            "icon": "search",
        }
    ]


async def test_clear_display_settings_falls_back_to_manifest(settings_store):
    settings_store.save_values(PACK, {"welcome_title": "临时标题"})
    settings_store.save_values(PACK, {"welcome_title": None})  # 清除 = 回落
    d = await _display(settings_store)
    assert "welcome_title" not in d
