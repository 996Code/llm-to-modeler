"""客服覆盖配置的装载与发布校验；空安装不携带业务场景。"""
import copy
import json
import logging
from string import Formatter
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)
FACTORY_SCENARIOS_KEY = "scenarios"
_ACTION_KINDS = ("kg", "chatbi")


class ConfigValidationError(ValueError):
    """配置结构非法。"""


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _rehydrate_options(value: Any) -> Any:
    from sdk.tool import AskOption
    if isinstance(value, list):
        return [AskOption(label=o["label"], description=o.get("description") or "")
                if isinstance(o, dict) else o for o in value]
    return value


def _rehydrate(table: Dict[str, Any]) -> Dict[str, Any]:
    table = copy.deepcopy(table)
    for cfg in table.values():
        required = cfg.get("required") or {}
        if isinstance(required.get("options"), list):
            required["options"] = _rehydrate_options(required["options"])
    return table


def factory_default_config() -> Dict[str, Any]:
    return {FACTORY_SCENARIOS_KEY: {}}


def _template_fields(template: str) -> set:
    try:
        fields = {field for _, field, _, _ in Formatter().parse(template) if field}
    except ValueError as exc:
        raise ConfigValidationError("模板占位符格式不正确") from exc
    if any(not field.isidentifier() for field in fields):
        raise ConfigValidationError("模板只支持直接槽位名占位符")
    return fields


def _validate_schema(schema):
    from jsonschema.validators import validator_for
    try:
        validator_for(schema).check_schema(schema)
    except Exception as exc:
        raise ConfigValidationError(f"input_schema 非法: {exc}") from exc

    def inspect(node):
        if isinstance(node, dict):
            for key in ("$ref", "$dynamicRef", "$recursiveRef"):
                if key in node and (not isinstance(node[key], str) or not node[key].startswith("#")):
                    raise ConfigValidationError("schema 不允许外部引用")
            for child in node.values():
                inspect(child)
        elif isinstance(node, list):
            for child in node:
                inspect(child)
    inspect(schema)
    if not isinstance(schema, dict) or schema.get("type", "object") != "object":
        raise ConfigValidationError("工具参数 schema 必须是 object")
    if any(key not in (schema.get("properties") or {}) for key in schema.get("required") or []):
        raise ConfigValidationError("必填参数必须在 properties 中声明")


def validate_config(config: Dict[str, Any], strict: bool = False) -> Dict[str, Any]:
    """strict=True 为发布校验(管理页保存):非通用回答模式必须显式查询
    模板;装载用宽松档——存量配置缺模板时回退原文检索,不拒绝装载。"""
    if not isinstance(config, dict):
        raise ConfigValidationError("配置必须是 JSON 对象")
    scenarios = config.get(FACTORY_SCENARIOS_KEY)
    if not isinstance(scenarios, dict):
        raise ConfigValidationError("scenarios 必须是对象")
    for key, cfg in scenarios.items():
        def fail(message):
            raise ConfigValidationError(f"场景 {key} {message}")
        if not isinstance(key, str) or not key.strip() or not isinstance(cfg, dict):
            fail("必须是非空 key 对应的对象")
        for field in ("label", "intent"):
            if not isinstance(cfg.get(field), str) or not cfg[field].strip():
                fail(f"缺少非空 {field}")
        required = cfg.get("required", {})
        if not isinstance(required, dict):
            fail("required 必须是对象")
        if required:
            for field in ("slot", "header", "question"):
                if not isinstance(required.get(field), str) or not required[field].strip():
                    fail(f"槽位定义缺少非空 {field}")
            if required.get("slot_type", "text") not in (
                    "text", "enum", "number", "integer", "boolean", "json", "date", "list"):
                fail("槽位类型不支持")
            if required.get("normalize") not in (None, "", "upper", "lower", "strip"):
                fail("normalize 必须是 upper/lower/strip")
        options = required.get("options", [])
        if not isinstance(options, list) or any(
                not isinstance(o, dict) or not isinstance(o.get("label"), str)
                or not o["label"].strip() or not isinstance(o.get("description", ""), str)
                for o in options):
            fail("options 必须是 {label, description} 数组")
        hints = cfg.get("_param_hints", {})
        if not isinstance(hints, dict) or any(not isinstance(v, str) for v in hints.values()):
            fail("参数提示必须是文本映射")
        if not isinstance(required.get("extract_hint", ""), str):
            fail("extract_hint 必须是文本")
        action = cfg.get("action")
        if not isinstance(action, dict) or action.get("kind") not in _ACTION_KINDS:
            fail("action.kind 必须是 kg/chatbi")
        params = action.get("params", {})
        if not isinstance(params, dict) or any(
                not isinstance(p, str) or not isinstance(v, str) or not v.strip()
                for p, v in params.items()):
            fail("params 必须是参数到槽位的映射")
        slots = set(params.values()) | set(hints)
        slots.update(s for s in (required.get("slot"), required.get("subject_slot")) if s)
        if action["kind"] == "chatbi":
            if not isinstance(action.get("tool"), str) or not action["tool"].strip():
                fail("动作缺少 tool")
            if not any(str(action.get(f) or "").strip() for f in ("server", "datasource_id", "env")):
                fail("动作需绑定 server/datasource_id 或 env")
            schema = action.get("input_schema")
            if schema is not None:
                _validate_schema(schema)
                slots.update(schema.get("properties", {}))
            if action.get("card", "_table_card") not in (
                    "_table_card", "_progress_card", "_instrument_card"):
                fail("结果卡构建器不支持")
        else:
            if action.get("conclusion", "answer") not in (
                    "answer", "mandatory", "turnaround", "qualification"):
                fail("知识回答模式不支持")
            for field in ("kb", "query_template", "answer_prompt"):
                if field in action and not isinstance(action[field], str):
                    fail(f"{field} 必须是文本")
            if not (str(action.get("kb") or "").strip() or action.get("kb_rules")):
                fail("知识库动作必须显式绑定知识库或选库规则")
            if (strict and action.get("conclusion", "answer") != "answer"
                    and not str(action.get("query_template") or "").strip()):
                fail("非通用回答模式必须显式配置查询模板")
        for i, rule in enumerate(action.get("kb_rules") or []):
            if (not isinstance(rule, dict) or not rule.get("slot") or not rule.get("kb")
                    or not isinstance(rule.get("in"), list) or not rule["in"]):
                fail(f"kb_rules[{i}] 必须是 {{slot, in: [值...], kb}} 结构")
            slots.add(rule["slot"])
        templates = [required.get("question", ""), action.get("stage", ""),
                     action.get("query_template", "")]
        notices = action.get("notices", {})
        if not isinstance(notices, dict):
            fail("notices 必须是对象")
        for status, notice in notices.items():
            if status not in ("forbidden", "not_found", "timeout", "upstream_error", "invalid_input"):
                fail("通知状态必须是明确的业务失败状态")
            if not isinstance(notice, dict):
                fail("通知必须是对象")
            for field in ("title", "message"):
                if not isinstance(notice.get(field), str):
                    fail(f"通知缺少 {field}")
            templates.extend(notice.get(k, "") for k in ("title", "message", "order"))
        for template in templates:
            if not isinstance(template, str):
                fail("模板必须是文本")
            unknown = _template_fields(template) - slots - {"subject", "query", "user_input"}
            if unknown:
                fail(f"模板引用未声明槽位: {', '.join(sorted(unknown))}")
    return config


def load_scenarios() -> Dict[str, Any]:
    from domains.metrology_assistant import runtime, store
    try:
        db = runtime.get_pack_db()
        rev = store.current_revision(db)
        cached = runtime.cached_config()
        if cached is not None and runtime.cached_revision() == rev:
            return copy.deepcopy(cached)
        config = store.load_config(db)
        if config is None:
            default = factory_default_config()
            rev = store.seed_config_if_absent(db, default)
            config = {**default, "_revision": rev}
        content = {k: v for k, v in config.items() if not k.startswith("_")}
        # 仅识别历史演示的原始声明；不覆盖已定制的配置，不写回数据库。
        from domains.metrology_assistant.legacy_demo import upgrade_legacy_scenario
        content = {**content, FACTORY_SCENARIOS_KEY: {
            key: upgrade_legacy_scenario(key, cfg)
            for key, cfg in content.get(FACTORY_SCENARIOS_KEY, {}).items()}}
        valid = validate_config(content)
        table = _rehydrate(valid[FACTORY_SCENARIOS_KEY])
        runtime.set_cached_config(table, int(config.get("_revision", rev)))
        return copy.deepcopy(table)
    except Exception as exc:
        cached = runtime.cached_config()
        if cached is not None:
            logger.exception("客服配置读取失败，使用最后有效快照")
            return copy.deepcopy(cached)
        raise RuntimeError("客服配置暂不可用，请检查配置存储或联系管理员") from exc
