"""P2#9 消歧预筛——类型分桶+Levenshtein/字符重合双阈值+别名桥接。

身份复合(_run_consolidation)此前把全量名录分批给 LLM——千级库每批
都是全量调用。预筛先在本地收窄:同类型分桶配对,双阈值(编辑距离/
字符重合)+别名桥接判可疑,仅可疑对涉及的实体送 LLM。
阈值参照 RAGFlow entity_resolution.py(Levenshtein ≤ 2 / 重合 ≥ 0.5)。
纯函数零 IO,千级库毫秒级。
"""
from typing import Dict, List, Tuple

# 阈值(参照 RAGFlow entity_resolution 预筛)
_LEV_MAX = 2          # 编辑距离上限
_OVERLAP_MIN = 0.5    # 字符重合率下限
_MAX_PAIRS_PER_ENTITY = 8   # 单实体可疑对上限(防同前缀批量实体爆炸)


def levenshtein(a: str, b: str) -> int:
    """编辑距离(经典 DP,两行滚动)。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1,        # 删除
                           cur[j - 1] + 1,     # 插入
                           prev[j - 1] + (ca != cb)))  # 替换
        prev = cur
    return prev[-1]


def char_overlap(a: str, b: str) -> float:
    """字符重合率:|字符集交集| / |字符集并集|。"""
    sa, sb = set(a), set(b)
    union = sa | sb
    return len(sa & sb) / len(union) if union else 0.0


def _names_of(e: Dict) -> List[str]:
    """实体参与匹配的全部名字(本体名+别名,normalized 优先)。"""
    out = [e.get("normalized") or e.get("name") or ""]
    out.extend(str(a) for a in e.get("aliases") or [])
    return [x for x in out if x]


def _suspicious(ea: Dict, eb: Dict) -> bool:
    """双阈值+别名桥接判可疑:任一名对满足即算。"""
    for na in _names_of(ea):
        for nb in _names_of(eb):
            if na == nb:                      # 别名/本体互指
                return True
            if abs(len(na) - len(nb)) <= _LEV_MAX and levenshtein(na, nb) <= _LEV_MAX:
                return True
            if char_overlap(na, nb) >= _OVERLAP_MIN:
                return True
    return False


def prescreen_pairs(entities: List[Dict]) -> List[Tuple[str, str]]:
    """类型分桶+双阈值预筛 → 可疑对列表(normalized 名元组,排序去重)。

    返回对数即 LLM 需复核的候选;零可疑对时调用方可整段跳过 LLM。
    性能护栏:①首字符分桶(中文实体首字不同的对直接跳——可疑对几乎
    必共享首字或别名桥接);②单实体可疑对上限(同前缀批量实体如
    "实体0001/实体0002"重合率全命中,不设限会产出千万对)。
    """
    out: List[Tuple[str, str]] = []
    seen = set()
    buckets: Dict[str, List[Dict]] = {}
    for e in entities or []:
        buckets.setdefault(e.get("type") or "", []).append(e)
    for members in buckets.values():
        # 首字符索引:同 type 内按首字分桶,跨桶只走别名桥接候选
        by_first: Dict[str, List[Dict]] = {}
        for m in members:
            names = _names_of(m)
            firsts = {n[0] for n in names if n}
            for f in firsts:
                by_first.setdefault(f, []).append(m)
        per_entity: Dict[str, int] = {}
        for group in by_first.values():
            for i in range(len(group)):
                for j in range(i + 1, len(group)):
                    ea, eb = group[i], group[j]
                    na = ea.get("normalized") or ea.get("name") or ""
                    nb = eb.get("normalized") or eb.get("name") or ""
                    if not na or not nb or na == nb:
                        continue
                    key = (na, nb) if na < nb else (nb, na)
                    if key in seen:
                        continue
                    if per_entity.get(na, 0) >= _MAX_PAIRS_PER_ENTITY:
                        continue
                    if per_entity.get(nb, 0) >= _MAX_PAIRS_PER_ENTITY:
                        continue
                    if _suspicious(ea, eb):
                        seen.add(key)
                        out.append(key)
                        per_entity[na] = per_entity.get(na, 0) + 1
                        per_entity[nb] = per_entity.get(nb, 0) + 1
    return out
