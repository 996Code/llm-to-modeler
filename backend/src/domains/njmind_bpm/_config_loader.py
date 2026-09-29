"""njmind_bpm config.yaml 加载器（路径表/服务名缓存，模式同 njmind_form）。"""
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
_PATHS: Optional[Dict[str, Any]] = None
_SERVICE_NAME: Optional[str] = None


def _load() -> Dict[str, Any]:
    return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}


def load_paths() -> Dict[str, Any]:
    global _PATHS
    if _PATHS is None:
        _PATHS = _load().get("paths") or {}
    return _PATHS


def load_service_name() -> str:
    global _SERVICE_NAME
    if _SERVICE_NAME is None:
        services = _load().get("services") or {}
        _SERVICE_NAME = next(iter(services), "")
    return _SERVICE_NAME
