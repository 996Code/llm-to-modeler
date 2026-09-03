"""leave_application 依赖的上游服务名与端点路径（与 config.yaml 同源）。

地址解析走通用机制（upstream_client.resolve_base）：宿主 services 表按请求
下发，未下发则 fail-closed 报错。
"""
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
_CACHE: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _CACHE
    if _CACHE is None:
        _CACHE = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return _CACHE


def load_paths() -> Dict[str, str]:
    """端点路径表（config.yaml paths 段，单一事实源）。"""
    return _load().get("paths") or {}


def load_service_name() -> str:
    """上游服务名（config.yaml services 段首个 key，单一事实源）。"""
    services = _load().get("services") or {}
    return next(iter(services), "")


SERVICE_NAME = load_service_name()  # 缺声明为空串→preflight fail-closed
PATHS = load_paths()
