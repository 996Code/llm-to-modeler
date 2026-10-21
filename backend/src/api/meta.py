"""Pack 元数据 API —— 把已加载 pack 的 manifest 声明吐给前端。

【模块定位】
前端（diff 组件 / 版本历史 UI / 服务发现）需要知道当前加载了哪些 pack、
每个 pack 声明了怎样的制品结构（identity 对齐键、展示字段）与服务依赖，
却不需要也不应该知道 pack 的业务实现细节。本端点就是「声明 → 前端」的出口。

【数据来源】
由 main.py lifespan 里 load_all_packs 得到的 registry + prompt_loader 装配，
把「以 config.yaml 为载体的 pack manifest」以只读形式暴露。前端只消费通用字段：
  - name / artifact.identity / artifact.display / services / tools
这些字段在引擎和前端之间是「不透明声明」，领域词不泄漏到协议层。
"""
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meta", tags=["meta"])


def _manifest_for(pack_name: str, cfg: Dict[str, Any], tools: List[Any],
                  display_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从 pack 的 config.yaml（cfg）+ 工具注册表（tools）提取对前端有用的声明。

    display_override: 管理端 DB 覆盖值（PackSettings 的 display 键）——
    展示配置（欢迎标题/常用问题/标签）是运营口径不是业务逻辑,提取到
    DB 后管理端改完即生效,不用改 config.yaml 重新部署。DB > manifest。
    """
    artifact = cfg.get("artifact", {})
    services = cfg.get("services", {})
    display = artifact.get("display", {})
    if display_override:
        display = {**display, **display_override}
    return {
        "name": pack_name,
        "artifact": {
            "type": artifact.get("type", "config"),
            "identity": artifact.get("identity", {}),
            "display": display,
            # 制品卡动作集（pack 声明，前端按此渲染按钮）：view_json/apply/rewind。
            # 不同插件的制品交互不同——如某类制品可能只要 apply 不要
            # view_json/rewind。未声明时前端回退最小集（仅 view_json）。
            "actions": artifact.get("actions", ["view_json"]),
        },
        "services": list(services.keys()),
        "tools": [
            {"name": t.name, "when": getattr(t, "when", "")}
            for t in tools
        ],
    }


@router.get("/packs")
async def list_packs(request: Request):
    """返回已加载 pack 的 manifest 声明列表。

    前端（diff 组件 / 版本历史 UI / 服务发现）只消费通用字段：
      name / artifact.identity / artifact.display / services
    这些是「不透明声明」，领域词不泄漏到协议层（守门不变量 #2）。

    可选查询参数 ``?packs=a,b,c``：嵌入宿主可在 iframe URL 上声明要用的
    pack 子集（如宿主指定默认插件）。语义与 PACKS_ENABLED 一致——
    请求参数是上层（宿主）声明、env 是部署方声明，两者**取交集**：
    只返回两处都允许的 pack；某一方未声明时不参与限制。
    """
    registry = getattr(request.app.state, "registry", None)
    pack_configs = getattr(request.app.state, "pack_configs", {}) or {}
    if not registry:
        return []  # 未装配（启动失败/测试）→ Fail-Closed 空列表

    # 查询参数白名单：?packs=pack_a,pack_b（宿主在 iframe URL 上声明）
    request_names = _parse_names(request.query_params.get("packs"))
    # 部署方白名单：优先读运行时启停状态（app.state.pack_state，管理端热切换
    # 后即时生效）；未装配（旧测试环境）时回退读 env PACKS_ENABLED。
    # 语义与 PACKS_ENABLED 一致——请求参数是上层（宿主）声明、部署方声明是
    # 运行时状态，两者**取交集**：只返回两处都允许的 pack；某一方未声明时
    # 不参与限制。
    pack_state = getattr(request.app.state, "pack_state", None)
    if pack_state is not None:
        env_names: Optional[set] = set(pack_state.enabled_names())
    else:
        env_names = _packs_whitelist()
    if request_names is not None and env_names is not None:
        # 两处都声明 → 交集（宿主想要且部署方允许的）
        allowed = request_names & env_names
    else:
        # 只声明了一处 → 以声明的那处为准；都没声明 → 全量（不限制）
        allowed = request_names if request_names is not None else env_names

    result = []
    # 展示配置 DB 覆盖(PackSettings 的 display 组字段):读一次全量,逐 pack 合并。
    # resolve_all 返回扁平键(welcome_title/welcome_subtitle/welcome_examples),
    # 这里收拢成 display 字典——DB 有值的键才覆盖 manifest(空=回落默认)。
    display_overrides: Dict[str, Dict[str, Any]] = {}
    settings_store = getattr(request.app.state, "settings_store", None)
    if settings_store is not None:
        try:
            from services.pack_settings import read_settings_schema, resolve_all
            _DISPLAY_KEYS = ("welcome_title", "welcome_subtitle", "welcome_examples")
            for pack_name in pack_configs:
                schema = read_settings_schema(pack_name)
                if not schema:
                    continue
                saved = settings_store.get_values(pack_name)
                resolved = resolve_all(pack_name, saved=saved, schema=schema)
                d = {k: resolved[k] for k in _DISPLAY_KEYS
                     if resolved.get(k) not in (None, "")}
                # welcome_examples 在设置页是 JSON 字符串(表单单行输入),
                # display 消费方要数组——就地转换;坏 JSON 丢弃该键回退默认
                we = d.get("welcome_examples")
                if isinstance(we, str):
                    try:
                        d["welcome_examples"] = json.loads(we)
                    except (ValueError, TypeError):
                        d.pop("welcome_examples", None)
                if d:
                    display_overrides[pack_name] = d
        except Exception as e:  # noqa: BLE001  展示覆盖失败回退 manifest,不阻断
            logger.warning(f"display 覆盖读取失败(回退 manifest): {e}")
    for pack_name, cfg in pack_configs.items():
        if allowed is not None and pack_name not in allowed:
            continue
        # manifest.tools 从空：前端 diff/历史只用 artifact/services，
        # 不需要工具列表——避免维护「工具→pack」反向表
        result.append(_manifest_for(pack_name, cfg, [],
                                    display_override=display_overrides.get(pack_name)))
    return result


def _parse_names(raw: Any) -> Optional[set]:
    """解析逗号分隔名列表；空/未传返回 None（= 不参与限制）。"""
    if not raw:
        return None
    names = {n.strip() for n in str(raw).split(",") if n.strip()}
    return names or None


def _packs_whitelist() -> Optional[set]:
    """读取部署方白名单 PACKS_ENABLED（与 domains._packs_whitelist 同逻辑）。

    两者必须保持一致（一处改动另一处同步改）；meta 层不复用 domains 的
    _packs_whitelist，避免 api 层反向 import domains（维持 api→domains 单向）。
    """
    import os
    raw = os.getenv("PACKS_ENABLED", "").strip()
    if not raw:
        return None
    return {n.strip() for n in raw.split(",") if n.strip()}
