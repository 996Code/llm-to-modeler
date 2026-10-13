"""目录名词确定性匹配原语 — 中文名词 → 目录真实 code。

【模块定位】
零领域知识的通用原语层:输入是一份"目录"(code + 中文名称的条目列表),
输出是确定性匹配结果。上层 pack 只负责取目录、组装 prompt、把 pending
候选项展示给调用方选择,所有匹配规则集中在这里。

本模块不含任何业务名词(不知道目录里装的是什么),因此可被任意
pack 复用 —— 只要对方能给出 CatalogEntry 列表。

【匹配规则(顺序固定,全部纯函数、无 I/O)】
对每个待匹配语义串 semantic:
  ① semantic == entry.code 且唯一命中 → 透传保留(存量 code 直通)
  ② name 精确唯一 → 直选;多个精确命中 → pending(命中集)
  ③ 无精确命中但包含唯一(semantic in name 或 name in semantic)→ 直选;
     多个 → pending(命中集)
  ④ 零命中 → pending(全目录)
resolved 只收真实 code,去重保序,绝不混入原始语义串;
无法直选的语义串原样进入 pending,等待上层二次选择。
"""
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class CatalogEntry:
    """目录条目:一条可被选中并落库的记录。"""

    code: str  # 稳定标识,最终落库值
    name: str  # 中文名词,匹配目标


@dataclass(frozen=True)
class PendingPick:
    """无法直选的语义串 + 需上层挑选的候选集。"""

    semantic: str
    candidates: tuple[CatalogEntry, ...]


@dataclass(frozen=True)
class ResolveResult:
    """批量匹配结果:已确定 code 与待挑选项。"""

    resolved: tuple[str, ...]  # 已是真实 code,去重保序
    pending: tuple[PendingPick, ...]


def _pick_one(semantic: str, catalog: Sequence[CatalogEntry]):
    """按规则表①-④匹配单个语义串。

    Returns:
        (命中条目, None) —— 可直选,取 entry.code;
        (None, 候选元组) —— 需 pending,候选为命中集或全目录。
    """
    # ① 存量 code 直查:唯一命中才透传,保证不误吞目录里不存在的串。
    code_hits = [e for e in catalog if semantic == e.code]
    if len(code_hits) == 1:
        return code_hits[0], None
    # ② name 精确:唯一直选,多个精确进 pending(命中集)。
    exact_hits = [e for e in catalog if semantic == e.name]
    if len(exact_hits) == 1:
        return exact_hits[0], None
    if len(exact_hits) > 1:
        return None, tuple(exact_hits)
    # ③ 包含匹配(双向):仅当②零命中时才降级到模糊层。
    contains_hits = [e for e in catalog if semantic in e.name or e.name in semantic]
    if len(contains_hits) == 1:
        return contains_hits[0], None
    if len(contains_hits) > 1:
        return None, tuple(contains_hits)
    # ④ 零命中:全目录进 pending,交上层决定。
    return None, tuple(catalog)


def resolve_names(values: Sequence[str], catalog: Sequence[CatalogEntry]) -> ResolveResult:
    """把一批语义串确定性匹配成目录真实 code。

    Args:
        values: 待匹配语义串(中文名词或存量 code,顺序即业务给出顺序)。
        catalog: 目录条目列表。

    Returns:
        ResolveResult:resolved 为真实 code 去重保序;
        无法直选的语义串逐个生成 PendingPick(保持输入顺序)。
    """
    resolved: list[str] = []
    pending: list[PendingPick] = []
    for semantic in values:
        entry, candidates = _pick_one(semantic, catalog)
        if entry is not None:
            # 去重保序:首个出现位置固定,后到的重复 code 丢弃。
            if entry.code not in resolved:
                resolved.append(entry.code)
        else:
            pending.append(PendingPick(semantic=semantic, candidates=candidates))
    return ResolveResult(resolved=tuple(resolved), pending=tuple(pending))


def lookup_selected(label: str, candidates: Sequence[CatalogEntry]) -> CatalogEntry | None:
    """按上层构造的 label 在候选内精确反查条目。

    供上层把调用方回选的文本映射回目录条目:name 精确相等优先,
    其次 code 精确相等;找不到返回 None(调用方负责提示重选)。

    Args:
        label: 上层展示给调用方、又被原样回传的文本。
        candidates: 该次 pending 的候选条目集。

    Returns:
        命中的 CatalogEntry;无命中返回 None。
    """
    for entry in candidates:
        if label == entry.name or label == entry.code:
            return entry
    return None


def entries_from_rows(rows: Sequence[dict]) -> list[CatalogEntry]:
    """把 dict 行序列转换成 CatalogEntry 列表。

    每行需含字符串型 code 与 name;缺任一字段或值非字符串的行剔除
    (上游脏数据不进目录,匹配层只面对干净条目)。

    Args:
        rows: 形如 {"code": ..., "name": ...} 的行序列。

    Returns:
        合法条目列表,保持原行顺序。
    """
    entries: list[CatalogEntry] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        code = row.get("code")
        name = row.get("name")
        if isinstance(code, str) and isinstance(name, str):
            entries.append(CatalogEntry(code=code, name=name))
    return entries
