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
import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/meta", tags=["meta"])


def _manifest_for(pack_name: str, cfg: Dict[str, Any], tools: List[Any]) -> Dict[str, Any]:
    """从 pack 的 config.yaml（cfg）+ 工具注册表（tools）提取对前端有用的声明。"""
    artifact = cfg.get("artifact", {})
    services = cfg.get("services", {})
    return {
        "name": pack_name,
        "artifact": {
            "type": artifact.get("type", "config"),
            "identity": artifact.get("identity", {}),
            "display": artifact.get("display", {}),
            # 制品卡动作集（pack 声明，前端按此渲染按钮）：view_json/apply/rewind。
            # 不同插件的制品交互不同——如请假申请类制品可能只要 apply 不要
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
    """返回所有已加载 pack 的 manifest 声明列表。

    前端（diff 组件 / 版本历史 UI / 服务发现）只消费通用字段：
      name / artifact.identity / artifact.display / services
    这些是「不透明声明」，领域词不泄漏到协议层（守门不变量 #2）。
    """
    registry = getattr(request.app.state, "registry", None)
    pack_configs = getattr(request.app.state, "pack_configs", {}) or {}
    if not registry:
        return []  # 未装配（启动失败/测试）→ Fail-Closed 空列表
    result = []
    for pack_name in pack_configs:
        cfg = pack_configs.get(pack_name, {})
        # v1 的 manifest.tools 从空（工具清单由 /api/skills 提供；前端 diff/历史
        # 只用 artifact/services，不需要工具列表——避免维护「工具→pack」反向表）
        result.append(_manifest_for(pack_name, cfg, []))
    return result
