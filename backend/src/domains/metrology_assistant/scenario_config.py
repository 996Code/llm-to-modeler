"""场景配置装载层:DB 唯一事实源,SCENARIOS 常量降级为出厂默认种子。

P1 工程化(设计 §8.1/§9):场景/槽位/规则/来源映射从代码常量迁入
assistant_config 表——改场景是管理页操作,不是改代码;YAML/常量只留
插件声明与首启默认。

装载链(每次对话请求走一次,进程内缓存+revision 比对):
  DB 有配置 → 校验后返回(管理页改的即时生效);
  DB 无配置 → 用出厂默认(SCENARIOS 常量转档)种子化并返回;
  DB 不可用 → 出厂默认兜底 + 留痕(演示环境 PG 闪断不阻断问答,
  但管理页保存仍会 fail-closed 报错——读写不对称是刻意的:
  读可以旧,写必须真)。

配置结构(content JSON):
  scenarios: {key: {label, intent, required{...}, action{...}}}  与
    SCENARIOS 常量同构(表驱动形状不变,只是事实源从代码移到 DB);
  其余键(欢迎语等)预留,管理页 tab 逐个接。

校验(设计 §8.1"保存即校验"):场景 key 唯一、required/action 形状、
action.kind 枚举、chatbi 动作的 env/tool/params/notices/card 完整——
非法配置拒绝装载,执行器零容错(不静默跳过坏场景)。
"""
import json
import logging
from typing import Any, Dict, Optional

from domains.metrology_assistant.tools.assistant import SCENARIOS

logger = logging.getLogger(__name__)

# 出厂默认:代码内 SCENARIOS 表转档(首启种子;之后 DB 为准)
FACTORY_SCENARIOS_KEY = "scenarios"

# action.kind 允许值(与执行器分支一一对应)
_ACTION_KINDS = ("kg", "chatbi")


class ConfigValidationError(ValueError):
    """配置结构非法(管理页保存 400 / 装载拒绝)。"""


def _jsonable(value: Any) -> Any:
    """递归转 JSON 可序列化(AskOption 等 pydantic 模型 → dict)。"""
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return value.model_dump()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def _rehydrate_options(value: Any) -> Any:
    """JSON dict → AskOption(执行器/追问规格消费 SDK 类型)。"""
    from sdk.tool import AskOption
    if isinstance(value, list):
        return [AskOption(**o) if isinstance(o, dict) else o for o in value]
    return value


def _rehydrate(table: Dict[str, Any]) -> Dict[str, Any]:
    """DB/校验后的纯 JSON 场景表 → 执行器可消费形态(AskOption 复原)。"""
    for cfg in table.values():
        required = cfg.get("required") or {}
        if isinstance(required.get("options"), list):
            required["options"] = _rehydrate_options(required["options"])
    return table


def factory_default_config() -> Dict[str, Any]:
    """出厂默认配置(代码 SCENARIOS 表转档;种子与回滚基线)。"""
    return {FACTORY_SCENARIOS_KEY: _jsonable(
        {k: dict(v) for k, v in SCENARIOS.items()})}


def validate_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """校验配置结构;非法抛 ConfigValidationError,合法返回规范化配置。

    只认 FACTORY_SCENARIOS_KEY 一个业务键,其余键透传(管理页后续
    tab 扩展时在此追加校验)。
    """
    if not isinstance(config, dict):
        raise ConfigValidationError("配置必须是 JSON 对象")
    scenarios = config.get(FACTORY_SCENARIOS_KEY)
    if not isinstance(scenarios, dict) or not scenarios:
        raise ConfigValidationError("scenarios 必须是非空对象")
    for key, cfg in scenarios.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigValidationError("场景 key 必须是非空文本")
        if not isinstance(cfg, dict):
            raise ConfigValidationError(f"场景 {key} 必须是对象")
        for field in ("label", "intent"):
            if not isinstance(cfg.get(field), str) or not cfg[field].strip():
                raise ConfigValidationError(f"场景 {key} 缺少非空 {field}")
        required = cfg.get("required")
        if not isinstance(required, dict):
            raise ConfigValidationError(f"场景 {key} 缺少 required 槽位定义")
        for field in ("slot", "header", "question", "options"):
            if field not in required:
                raise ConfigValidationError(
                    f"场景 {key} 槽位定义缺少 {field}")
        options = required.get("options")
        if not isinstance(options, list) or any(
                not isinstance(o, dict) or not isinstance(o.get("label"), str)
                for o in options):
            raise ConfigValidationError(
                f"场景 {key} options 必须是 {{label, description}} 数组")
        action = cfg.get("action")
        if not isinstance(action, dict) or action.get("kind") not in _ACTION_KINDS:
            raise ConfigValidationError(
                f"场景 {key} action.kind 必须是 {'/'.join(_ACTION_KINDS)}")
        if action["kind"] == "chatbi":
            for field in ("env", "tool", "params", "stage", "error_noun"):
                if not action.get(field):
                    raise ConfigValidationError(
                        f"场景 {key} chatbi 动作缺少 {field}")
            if not isinstance(action.get("params"), dict) or not action["params"]:
                raise ConfigValidationError(
                    f"场景 {key} params 必须是非空槽位映射")
            if not action.get("card"):
                raise ConfigValidationError(f"场景 {key} 缺少结果卡构建器 card")
    return config


def load_scenarios() -> Dict[str, Any]:
    """读当前生效场景表(缓存+revision 比对;DB 异常出厂默认兜底)。

    返回 {key: cfg} 与代码 SCENARIOS 同构——执行器/提示词拼装/
    追问解析零改动(事实源换了,形状没换)。
    """
    from domains.metrology_assistant import runtime, store
    try:
        db = runtime.get_pack_db()
        rev = store.current_revision(db)
        cached = runtime.cached_config()
        if cached is not None and runtime.cached_revision() == rev:
            return cached
        config = store.load_config(db)
        if config is None:
            # 首启(或清表后的测试库):出厂默认种子化
            default = factory_default_config()
            store.seed_config_if_absent(db, default)
            config = {**default, "_revision": 1}
        scenarios = validate_config(
            {k: v for k, v in config.items() if not k.startswith("_")})
        table = _rehydrate(scenarios[FACTORY_SCENARIOS_KEY])
        runtime.set_cached_config(table, rev)
        return table
    except ConfigValidationError:
        # 管理页保存了坏配置(保存侧校验漏网/手工改库):拒绝装载,
        # 出厂默认兜底并留痕——不静默用坏配置回答业务问题
        logger.exception("场景配置校验失败,回退出厂默认")
        return {k: dict(v) for k, v in SCENARIOS.items()}
    except Exception:
        # DB 不可用:演示环境读路径降级出厂默认(写路径仍 fail-closed)
        logger.exception("场景配置读取失败,回退出厂默认")
        return {k: dict(v) for k, v in SCENARIOS.items()}
