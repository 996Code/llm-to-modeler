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


def _suspicion_score(ea: Dict, eb: Dict) -> float:
    """对的可疑度:inf=不可疑;有限值越小越可疑。

    名对全积取最优:同名(别名/本体互指)=0 最可疑;编辑距离(仅字数
    差 ≤_LEV_MAX 时计算)为主信号;字符重合 ≥_OVERLAP_MIN 兜底(折算
    10-overlap,排在一切编辑距离命中之后)。判可疑与排序共用本函数,
    cap 竞争不重算。
    """
    best = float("inf")
    for na in _names_of(ea):
        for nb in _names_of(eb):
            if na == nb:
                return 0.0
            if abs(len(na) - len(nb)) <= _LEV_MAX:
                lev = levenshtein(na, nb)
                if lev <= _LEV_MAX:
                    best = min(best, float(lev))
            if best > 9.0 and char_overlap(na, nb) >= _OVERLAP_MIN:
                best = min(best, 10.0 - char_overlap(na, nb))
    return best


def _suspicious(ea: Dict, eb: Dict) -> bool:
    """双阈值+别名桥接判可疑:任一名对满足即算。"""
    return _suspicion_score(ea, eb) != float("inf")


def prescreen_pairs(entities: List[Dict]) -> List[Tuple[str, str]]:
    """类型分桶+双阈值预筛 → 可疑对列表(normalized 名元组,排序去重)。

    返回对数即 LLM 需复核的候选;零可疑对时调用方可整段跳过 LLM。
    性能护栏:①首字符分桶(中文实体首字不同的对直接跳——可疑对几乎
    必共享首字或别名桥接;名字首字集合不相交的对永不比较);②单实体
    可疑对上限——同前缀批量实体("实体0001/实体0002"重合率全命中)
    不设限会产出千万对。**先全量判可疑再按可疑度降序入选**:
    cap 若在判可疑前截断,先到的平庸对会挤掉后到的高可疑对(名录顺序
    不稳定=谁占名额随机),而"保住最可疑的对"正是预筛的存在意义。
    """
    # 桶内按 normalized 排序后**滑动窗口配对**:每实体只与字典序前后
    # _WINDOW 个候选配对。正确性依据:双阈值可疑对(lev≤2/重合≥0.5)
    # 的名字高度相似,字典序几乎必然相邻(张三/张三丰相邻;实体0001
    # 与其 lev≤2 家族=数字近邻相邻);窗口外的对可疑概率可忽略。
    # 性能:O(n log n) 排序 + O(n×窗口) 配对,5000 同前缀实体从全配对
    # 1250 万对的 81s 降至毫秒级。窗口取 cap 的 4 倍留裕量。
    import heapq
    _WINDOW = _MAX_PAIRS_PER_ENTITY * 4

    def _name_sets(e: Dict) -> List[frozenset]:
        return [frozenset(n) for n in _names_of(e) if n]

    per_heap: Dict[str, list] = {}
    per_keys: Dict[str, set] = {}
    buckets: Dict[str, List[Dict]] = {}
    for e in entities or []:
        buckets.setdefault(e.get("type") or "", []).append(e)
    for members in buckets.values():
        by_first: Dict[str, List[Dict]] = {}
        for m in members:
            for f in {n[0] for n in _names_of(m) if n}:
                by_first.setdefault(f, []).append(m)
        for group in by_first.values():
            group.sort(key=lambda m: m.get("normalized") or m.get("name") or "")
            sets = {id(m): _name_sets(m) for m in group}
            n = len(group)
            for i in range(n):
                for j in range(i + 1, min(i + 1 + _WINDOW, n)):
                    ea, eb = group[i], group[j]
                    na = ea.get("normalized") or ea.get("name") or ""
                    nb = eb.get("normalized") or eb.get("name") or ""
                    if not na or not nb or na == nb:
                        continue
                    key = (na, nb) if na < nb else (nb, na)
                    best = 0.0
                    for sa in sets[id(ea)]:
                        for sb in sets[id(eb)]:
                            if sa == sb:
                                best = 1.0
                                break
                            inter = len(sa & sb)
                            if inter and inter / len(sa | sb) > best:
                                best = inter / len(sa | sb)
                        if best >= 1.0:
                            break
                    if best < 0.2:
                        # 代理粗筛:字符集几乎不相干的对不进 cap 竞争。
                        # 阈值须低于 _OVERLAP_MIN——「张三/张叁」lev=1 但
                        # 重合仅 1/3,代理分阈值过高会误杀编辑距离路命中
                        continue
                    for name in (na, nb):
                        heap = per_heap.setdefault(name, [])
                        keys = per_keys.setdefault(name, set())
                        if key in keys:
                            continue
                        if len(heap) < _MAX_PAIRS_PER_ENTITY:
                            heapq.heappush(heap, (-best, key))
                            keys.add(key)
                        elif -heap[0][0] < best:
                            removed = heapq.heapreplace(heap, (-best, key))
                            keys.discard(removed[1])
                            keys.add(key)
    # 堆内存活的对过精确可疑判定(代理分只管 cap 竞争),稳定排序输出
    by_name = {e.get("normalized") or e.get("name"): e for e in entities or []}
    alive = set()
    for heap in per_heap.values():
        alive.update(k for _, k in heap)
    out = [k for k in sorted(alive) if _suspicious(by_name[k[0]], by_name[k[1]])]
    return out
