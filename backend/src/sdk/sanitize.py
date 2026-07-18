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
