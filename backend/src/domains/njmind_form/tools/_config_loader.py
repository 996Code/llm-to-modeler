"""config.yaml 加载器 - 从 njmind_form/config.yaml 读取类型映射。

供 CreateFormTool / ModifyFormTool 使用。
"""
from pathlib import Path
from typing import Dict, Tuple

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
_CACHE: Tuple[Dict[int, str], Dict[int, str]] | None = None


def load_type_mappings() -> Tuple[Dict[int, str], Dict[int, str]]:
    """加载 type_to_template 和 type_names 映射。

    返回 (type_to_template, type_names),key 是 int(type_code)。
    结果缓存,只读一次文件。
    """
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    with open(_CONFIG_PATH, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    # YAML 的 key 可能是 str,转成 int
    t2t = {int(k): v for k, v in cfg.get("type_to_template", {}).items()}
    tn = {int(k): v for k, v in cfg.get("type_names", {}).items()}

    _CACHE = (t2t, tn)
    return _CACHE
