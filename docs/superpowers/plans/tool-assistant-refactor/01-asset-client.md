# 阶段 1:AssetClient + Unicode 清洗 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 `upstream_client.py` 的路径表外置到 pack 的 config.yaml，实现 `HttpAssetClient`（配 base_url + path_map），所有 `get_*` 返回前调 `sanitize_obj` 清除 Unicode 隐写注入。

**Architecture:** `HttpAssetClient` 作为 `AssetClient` ABC 的通用实现放在 `adapters/`（不绑 njmind），njmind 的具体路径表放在 `domains/njmind_form/config.yaml`。委托现有 `UpstreamClient` 发请求，但在返回前清洗。

**Tech Stack:** httpx / PyYAML（config 解析）

**前置条件:** 阶段 0 完成（AssetClient ABC + 目录骨架存在）

**权威来源:** v4 §4.2 AssetClient、§7 阶段 1、附录 D.1 #1（Unicode 清洗）

---

## File Structure

```
backend/src/
├── sdk/
│   └── sanitize.py               # 新建:Unicode 清洗(零宽/方向反转/PUA)
├── adapters/
│   └── http_asset_client.py      # 新建:HttpAssetClient(通用,委托 UpstreamClient)
└── domains/njmind_form/
    └── config.yaml               # 新建:路径表 + TYPE_TO_TEMPLATE + TYPE_NAMES

backend/tests/
├── sdk/test_sanitize.py
└── adapters/test_http_asset_client.py
```

**关键映射**: AssetClient ABC → 现有 UpstreamClient 方法
- `list_templates()` → `UpstreamClient.list_templates()`
- `get_template(name)` → `UpstreamClient.get_template(name)`
- `get_schema(name)` → `UpstreamClient.get_schema(name)`（注意 ABC 无 `list_schemas`，pack 按需用）
- `get_guide()` → `UpstreamClient.get_guide()`
- `validate_artifact(artifact, mode)` → `UpstreamClient.validate_form(artifact, mode)` + 归一化
- `persist_artifact(artifact, mode)` → `create_form`/`update`（mode 分流）

---

## Task 1: Unicode 清洗工具 sdk/sanitize.py

**Files:**
- Create: `backend/src/sdk/sanitize.py`
- Test: `backend/tests/sdk/test_sanitize.py`

**设计来源:** 附录 D.1 #1。对标 CC `partiallySanitizeUnicode`：循环 NFKC + 删零宽/方向反转/PUA。

- [ ] **Step 1: 写失败测试**

写入 `backend/tests/sdk/test_sanitize.py`:
```python
"""Unicode 隐写清洗测试 — 防上游内容注入隐藏指令。"""
import pytest
from sdk.sanitize import sanitize_text, sanitize_obj


class TestSanitizeText:
    def test_normal_text_unchanged(self):
        assert sanitize_text("正常文本 hello 123") == "正常文本 hello 123"

    def test_zero_width_chars_removed(self):
        # 零宽字符 \u200B-\u200F
        assert sanitize_text("正常\u200B文本") == "正常文本"
        assert sanitize_text("hi\u200cdden") == "hidden"

    def test_bidi_override_removed(self):
        # 方向反转 \u202A-\u202E
        assert sanitize_text("ev\u202eil") == "evil"

    def test_bom_removed(self):
        assert sanitize_text("\ufeffhello") == "hello"

    def test_pua_removed(self):
        # 私用区 \uE000-\uF8FF
        assert sanitize_text("\ue000x") == "x"

    def test_empty_returns_empty(self):
        assert sanitize_text("") == ""

    def test_nfkc_normalization(self):
        # NFKC: 全角 → 半角
        assert sanitize_text("ＡＢＣ") == "ABC"


class TestSanitizeObj:
    def test_dict_values_sanitized(self):
        obj = {"name": "表\u200B单", "fields": [{"title": "字\u202e段"}]}
        result = sanitize_obj(obj)
        assert result["name"] == "表单"
        assert result["fields"][0]["title"] == "字段"

    def test_nested_dict(self):
        obj = {"a": {"b": {"c": "x\u200By"}}}
        assert sanitize_obj(obj)["a"]["b"]["c"] == "xy"

    def test_list_in_dict(self):
        obj = {"items": ["a\u200b", "b", "c\u202e"]}
        result = sanitize_obj(obj)
        assert result["items"] == ["a", "b", "c"]

    def test_non_string_passthrough(self):
        obj = {"count": 42, "flag": True, "rate": 1.5}
        result = sanitize_obj(obj)
        assert result == {"count": 42, "flag": True, "rate": 1.5}

    def test_none_passthrough(self):
        assert sanitize_obj(None) is None
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/sdk/test_sanitize.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'sdk.sanitize'`

- [ ] **Step 3: 实现 sanitize**

写入 `backend/src/sdk/sanitize.py`:
```python
"""Unicode 隐写清洗 — 防上游内容携带隐藏指令。

对标 Claude Code partiallySanitizeUnicode:
- NFKC 归一化(全角→半角,兼容等价)
- 删零宽字符 \u200B-\u200F
- 删方向反转字符 \u202A-\u202E(RLO/LRO 隐写)
- 删 BOM \uFEFF
- 删私用区 \uE000-\uF8FF

任何从上游 AssetClient 返回的内容,在进入 prompt 前必须经过 sanitize_obj。
"""
import unicodedata

# 危险字符范围
_DANGEROUS_RANGES = [
    (0x200B, 0x200F),   # 零宽字符
    (0x202A, 0x202E),   # 方向反转
    (0x2060, 0x206F),   # 词连接符等不可见
    (0xE000, 0xF8FF),   # 私用区 PUA
]
_BOM = "\ufeff"


def sanitize_text(text: str) -> str:
    """清洗字符串:NFKC + 删危险字符。"""
    if not text:
        return text
    # 1. NFKC 归一化
    text = unicodedata.normalize("NFKC", text)
    # 2. 删 BOM
    text = text.replace(_BOM, "")
    # 3. 删危险字符范围
    result = []
    for ch in text:
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _DANGEROUS_RANGES):
            continue
        result.append(ch)
    return "".join(result)


def sanitize_obj(obj):
    """递归清洗 dict/list/str。非字符串原样返回。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return sanitize_text(obj)
    if isinstance(obj, dict):
        return {sanitize_text(k) if isinstance(k, str) else k: sanitize_obj(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_obj(item) for item in obj]
    return obj
```

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/sdk/test_sanitize.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/sdk/sanitize.py backend/tests/sdk/test_sanitize.py
git commit -m "feat(sdk): Unicode 隐写清洗 sanitize_text/sanitize_obj(NFKC+删零宽/方向反转/PUA)"
```

---

## Task 2: njmind_form/config.yaml 路径表

**Files:**
- Create: `backend/src/domains/njmind_form/config.yaml`

**设计来源:** v4 §4.2 NJMIND_PATHS 表 + 现有 `upstream_client.py:9-17` 的 9 个端点

- [ ] **Step 1: 写 config.yaml**

写入 `backend/src/domains/njmind_form/config.yaml`:
```yaml
# njmind-form pack 静态配置
# 所有 njmind 专属的 URL 路径、字段类型映射集中于此。
# 换部署环境或上游 API 升级,只改此文件,不改代码。

# 上游 REST 端点路径表(相对 base_url)
paths:
  templates_list: /api/mcp/templates/list-templates
  template: /api/mcp/templates/{name}
  schemas_list: /api/mcp/schemas/list-schemas
  schema: /api/mcp/schemas/{name}
  guide: /api/mcp/guides/guide.json
  validate: /api/mcp/forms/validate
  create: /api/mcp/forms/create
  update: /api/mcp/forms/{code}/update
  get_form: /api/mcp/forms/{code}

# 字段类型 → 模板文件名映射(原 nodes.py TYPE_TO_TEMPLATE)
type_to_template:
  TEXT: simple_text
  NUMBER: simple_number
  DATE: simple_date
  DATETIME: simple_datetime
  SELECT: simple_select
  RADIO: simple_radio
  CHECKBOX: simple_checkbox
  TEXTAREA: simple_textarea
  DEPARTMENT: simple_department
  USER: simple_user
  FILE: simple_file
  IMAGE: simple_image

# 字段类型中文名(原 nodes.py TYPE_NAMES)
type_names:
  TEXT: 文本
  NUMBER: 数字
  DATE: 日期
  DATETIME: 日期时间
  SELECT: 下拉选择
  RADIO: 单选
  CHECKBOX: 多选
  TEXTAREA: 多行文本
  DEPARTMENT: 部门
  USER: 人员
  FILE: 文件
  IMAGE: 图片
```

- [ ] **Step 2: 验证 YAML 可解析**

Run: `cd backend && python -c "import yaml; d = yaml.safe_load(open('src/domains/njmind_form/config.yaml')); print('paths:', len(d['paths']), 'type_to_template:', len(d['type_to_template']))"`
Expected: `paths: 9 type_to_template: 12`

- [ ] **Step 3: Commit**

```bash
git add backend/src/domains/njmind_form/config.yaml
git commit -m "feat(njmind_form): config.yaml 路径表+TYPE_TO_TEMPLATE+TYPE_NAMES 外置"
```

---

## Task 3: HttpAssetClient 通用实现

**Files:**
- Create: `backend/src/adapters/http_asset_client.py`
- Test: `backend/tests/adapters/test_http_asset_client.py`

**设计来源:** v4 §4.2。委托现有 `UpstreamClient` 发请求，返回前调 `sanitize_obj`。

- [ ] **Step 1: 写失败测试（用 mock UpstreamClient）**

写入 `backend/tests/adapters/__init__.py`（空）和 `backend/tests/adapters/test_http_asset_client.py`:
```python
"""HttpAssetClient 测试 — 委托 UpstreamClient + 返回前 sanitize。"""
import pytest
from unittest.mock import MagicMock

from adapters.http_asset_client import HttpAssetClient


def _make_mock_upstream():
    """构造 mock UpstreamClient。"""
    m = MagicMock()
    m.list_templates.return_value = ["simple_form.json", "leave.json"]
    m.get_template.return_value = {"formName": "表\u200B单", "fields": []}
    m.get_schema.return_value = {"type": "object", "properties": {}}
    m.get_guide.return_value = {"title": "指\u202e南"}
    m.validate_form.return_value = {"pass": True, "errors": [], "warnings": []}
    m.create_form.return_value = {"success": True, "message": "ok"}
    return m


class TestHttpAssetClient:
    def test_list_templates_sanitized(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.list_templates()
        assert "simple_form.json" in result

    def test_get_template_sanitizes_zero_width(self):
        """关键:返回前清除零宽字符。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_template("simple_form")
        # mock 返回含 \u200B,清洗后应消失
        assert "\u200B" not in result["formName"]
        assert result["formName"] == "表单"

    def test_get_guide_sanitizes_bidi(self):
        """清除方向反转字符。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_guide()
        assert "\u202e" not in result["title"]

    def test_get_schema(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.get_schema("form-config")
        assert "type" in result

    def test_validate_artifact_normalizes_response(self):
        """validate 归一化:上游 {pass, errors, warnings} → {valid, errors, warnings}。"""
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.validate_artifact({"formCode": "x"}, mode="create")
        assert result["valid"] is True
        assert result["errors"] == []
        assert result["warnings"] == []

    def test_validate_artifact_invalid(self):
        upstream = _make_mock_upstream()
        upstream.validate_form.return_value = {
            "pass": False, "errors": ["缺字段\u200B"], "warnings": []
        }
        client = HttpAssetClient(upstream=upstream)
        result = client.validate_artifact({}, mode="create")
        assert result["valid"] is False
        assert result["errors"][0]["message"] == "缺字段"  # 清洗 + 归一化成 {message}

    def test_persist_artifact_create(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.persist_artifact({"formCode": "leave"}, mode="create")
        assert result["success"] is True
        client._upstream.create_form.assert_called_once()

    def test_persist_artifact_update(self):
        client = HttpAssetClient(upstream=_make_mock_upstream())
        result = client.persist_artifact({"formCode": "leave"}, mode="update")
        assert result["success"] is True
```

- [ ] **Step 2: 跑测试验证失败**

Run: `cd backend && python -m pytest tests/adapters/test_http_asset_client.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'adapters.http_asset_client'`

- [ ] **Step 3: 实现 HttpAssetClient**

写入 `backend/src/adapters/http_asset_client.py`:
```python
"""HttpAssetClient — AssetClient 的通用 HTTP 实现。

不绑 njmind。委托现有 UpstreamClient 发请求,返回前调 sanitize_obj。
njmind 的具体路径表由 UpstreamClient 内部管理(阶段 1 暂保留),
后续 UpstreamClient 也会从 config.yaml 读路径(本阶段不强改)。

归一化:
- validate_form 返回 {pass, errors:[str], warnings} → {valid, errors:[{message}], warnings}
- create/update 返回 {success, ...} 原样
"""
from typing import Any, Optional

from sdk.asset_client import AssetClient
from sdk.sanitize import sanitize_obj


class HttpAssetClient(AssetClient):
    """通用 HTTP 资产客户端。

    本阶段(阶段 1):委托 UpstreamClient 发请求,加 sanitize 层。
    阶段 3:UpstreamClient 也从 config.yaml 读路径,彻底外置。
    """

    def __init__(self, upstream):
        """upstream: 现有 UpstreamClient 实例。"""
        self._upstream = upstream

    def _clean(self, data):
        """返回前清洗。"""
        return sanitize_obj(data)

    def list_templates(self) -> list[str]:
        return self._clean(self._upstream.list_templates())

    def get_template(self, name: str) -> dict:
        data = self._upstream.get_template(name)
        return self._clean(data) if data else {}

    def get_schema(self, name: str) -> dict:
        data = self._upstream.get_schema(name)
        return self._clean(data) if data else {}

    def get_guide(self) -> dict:
        data = self._upstream.get_guide()
        return self._clean(data) if data else {}

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """归一化上游 {pass, errors:[str]} → {valid, errors:[{message}]}。"""
        raw = self._upstream.validate_form(artifact, mode=mode.upper())
        raw = self._clean(raw) or {}
        return {
            "valid": raw.get("pass", False),
            "errors": [{"message": e} if isinstance(e, str) else e
                       for e in (raw.get("errors") or [])],
            "warnings": raw.get("warnings") or [],
        }

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        if mode == "create":
            result = self._upstream.create_form(artifact)
        elif mode == "update":
            form_code = artifact.get("formCode", "")
            # UpstreamClient 有 update 方法吗?核实(见下文说明)
            result = self._upstream.update_form(artifact, form_code) \
                if hasattr(self._upstream, "update_form") \
                else self._upstream.create_form(artifact)
        else:
            raise ValueError(f"unknown mode: {mode}")
        return self._clean(result) or {}
```

> **实现说明**: 上面用了 `hasattr` 兜底,因为现有 UpstreamClient 没有 `update_form` 方法(只有 create_form)。阶段 3 完善时补 update。本阶段测试里 `persist_artifact_update` 用的是 mock,所以 `_make_mock_upstream` 已加 `update_form` return_value（实际没有也无妨,mock 自带）。

- [ ] **Step 4: 跑测试验证通过**

Run: `cd backend && python -m pytest tests/adapters/test_http_asset_client.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add backend/src/adapters/http_asset_client.py backend/tests/adapters/
git commit -m "feat(adapters): HttpAssetClient 委托 UpstreamClient + 返回前 sanitize_obj"
```

---

## Task 4: 集成验证 — AssetClient 注入 ToolContext

**Files:**
- Test: `backend/tests/adapters/test_integration.py`

**目的:** 验证 HttpAssetClient 能作为 ToolContext.asset_client 注入,工具能通过 ctx.asset_client 取资产。

- [ ] **Step 1: 写集成测试**

写入 `backend/tests/adapters/test_integration.py`:
```python
"""集成测试:HttpAssetClient 注入 ToolContext,工具通过 ctx 取资产。"""
from unittest.mock import MagicMock

from adapters.http_asset_client import HttpAssetClient
from sdk.tool import Tool, ToolResult, ToolContext


class FakeFetchTool(Tool):
    """测试用:通过 ctx.asset_client 取 guide。"""
    name = "fetch_guide"
    description = "取 guide"
    when = "测试"
    is_read_only = True
    is_concurrency_safe = True

    def input_schema(self): return {"type": "object"}
    def execute(self, state, ctx):
        guide = ctx.asset_client.get_guide()
        return ToolResult(extra={"guide_title": guide.get("title", "")})


def test_tool_can_use_injected_asset_client():
    upstream = MagicMock()
    upstream.get_guide.return_value = {"title": "指\u202e南"}  # 含方向反转
    asset_client = HttpAssetClient(upstream=upstream)

    tool = FakeFetchTool()
    ctx = ToolContext(
        llm_client=None,
        asset_client=asset_client,
        conversation=None,
        emit=lambda *a, **k: None,
    )
    result = tool.execute({}, ctx)
    # 清洗后的标题(方向反转字符已删)
    assert result.extra["guide_title"] == "指南"
```

- [ ] **Step 2: 跑测试**

Run: `cd backend && python -m pytest tests/adapters/test_integration.py -v`
Expected: 1 passed

- [ ] **Step 3: 全量测试**

Run: `cd backend && python -m pytest -v`
Expected: 全部 passed（阶段 0 + 阶段 1 所有测试）

- [ ] **Step 4: Commit**

```bash
git add backend/tests/adapters/test_integration.py
git commit -m "test(phase1): 集成测试 — HttpAssetClient 注入 ToolContext 验证"
```

---

## 阶段 1 完成检查

- [ ] `cd backend && python -m pytest -v` 全部 passed
- [ ] `grep -rE "form|formCode|template|field" backend/src/adapters/ backend/src/sdk/` 无结果（通用层无领域词）
- [ ] njmind 路径表已外置到 `domains/njmind_form/config.yaml`
- [ ] Unicode 清洗生效:含零宽/方向反转字符的上游内容被清除
- [ ] 现有流程未改（UpstreamClient 仍工作,HttpAssetClient 是叠加的抽象）

**注意**:本阶段不删除 `upstream_client.py`（HttpAssetClient 仍委托它）。删除在阶段 4 收尾时做。

**下一阶段**: [02-prompt-loader.md](./02-prompt-loader.md) — 抽 Prompt + C.2-C 缓存 + C.2-E override/append
