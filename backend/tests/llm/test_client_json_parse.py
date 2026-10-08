"""LLM 客户端 JSON 提取容错测试（含 Python repr 第四级兜底）。

背景：996code 网关 json_object 模式曾整体失效——三个模型均把
chat_json 的 content 输出成 Python repr（单引号 dict / True / None），
三级 JSON 容错全部失败，金路径"未能生成结果"（2026-09-30 UI 实测，
call_logs 佐证）。第四级用 ast.literal_eval 兜底，本文件锁死该行为。
"""
import json

import pytest

from llm.client import LLMClient


def _bare_client() -> LLMClient:
    """绕过 __init__ 的网络配置，只测纯文本解析方法。"""

    class _Cfg:
        base_url = "http://test"
        model = "test"
        api_key = "k"
        temperature = 0.1
        max_tokens = 64

    client = LLMClient.__new__(LLMClient)
    client.config = _Cfg()
    return client


@pytest.fixture()
def client():
    return _bare_client()


# ── 一级：纯 JSON ────────────────────────────────────────────────


def test_纯JSON直接解析(client):
    assert client._parse_json_from_text('{"pack": "njmind_list"}') == {"pack": "njmind_list"}


# ── 二级：markdown 代码块 ────────────────────────────────────────


def test_markdown代码块提取(client):
    text = '```json\n{"tool": "generate_list"}\n```'
    assert client._parse_json_from_text(text) == {"tool": "generate_list"}


# ── 三级：前后缀文字包裹 ─────────────────────────────────────────


def test_前后缀文字包裹提取(client):
    text = '好的，结果如下：{"a": 1} 以上。'
    assert client._parse_json_from_text(text) == {"a": 1}


# ── 四级：Python repr（网关 json_object 失效兜底）────────────────


def test_repr单引号dict解析(client):
    assert client._parse_json_from_text("{'pack': 'njmind_list'}") == {"pack": "njmind_list"}


def test_repr布尔与None转JSON兼容(client):
    parsed = client._parse_json_from_text("{'tool': 'generate_list', 'ok': True, 'n': None}")
    assert parsed == {"tool": "generate_list", "ok": True, "n": None}
    # 往返保证是纯 JSON 兼容形态（可再次序列化）
    assert json.loads(json.dumps(parsed)) == parsed


def test_repr嵌套结构与前端缀文字(client):
    text = "前缀 {'conditionConfig': {'queryCondition': ['a']}, 'flags': [1, 2]} 后缀"
    parsed = client._parse_json_from_text(text)
    assert parsed == {"conditionConfig": {"queryCondition": ["a"]}, "flags": [1, 2]}


def test_真实事故样本_generate产物repr形态(client):
    # 2026-09-30 call_logs 实录片段（截断的安全子集）
    sample = ("{'conditionConfig': {'queryCondition': ['dabiaoti']}, "
              "'queryPage': {'defPageSize': 20, 'pageSizes': [10, 20, 50, 100], 'pageType': 0}}")
    parsed = client._parse_json_from_text(sample)
    assert parsed["queryPage"]["defPageSize"] == 20
    assert parsed["conditionConfig"]["queryCondition"] == ["dabiaoti"]


def test_无法解析仍抛ValueError(client):
    with pytest.raises(ValueError):
        client._parse_json_from_text("这不是任何结构化输出")
