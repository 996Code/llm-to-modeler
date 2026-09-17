"""图谱关系编辑核心(数据层)——校验 + 增删,api 端点调用,单测直测。

设计(复核报告 P0/P1 修复项):
  - parse_on_conditions: ON 字符串 → 结构化条件列表, 校验表/列真实性——
    拼写错误、无效列、prompt 污染文本进不了语义层(源 ChatBI 用
    源列/目标列下拉生成结构化 on_conditions 的后端等价);
  - add_relationship: 源/目标表都校验 + 正向重复 409 + 自环拒绝;
  - delete_relationship: 正反向关系一起清理(源语义), ON 匹配支持
    反向书写形式(orders.user_id = users.id ≡ users.id = orders.user_id);
  - 索引重建失败不抛异常: 调用方按 index_rebuilt 标志决定 degraded 展示,
    不再"语义层成功 + 索引失败"仍返回完全成功。
"""
from __future__ import annotations

import logging
import re
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class GraphEditError(Exception):
    """图谱编辑业务错误(status 映射 HTTP 状态码, api 层转 HTTPException)。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


# ON 片段: table.column = table.column (列名兼容引号风格)
_ON_PART_RE = re.compile(
    r"^[\"`]?(\w+)[\"`]?\.[\"`]?(\w+)[\"`]?\s*=\s*[\"`]?(\w+)[\"`]?\.[\"`]?(\w+)[\"`]?$")
# AND 分割(词边界, 大小写不敏感)
_AND_SPLIT_RE = re.compile(r"\s+AND\s+", re.IGNORECASE)


def parse_on_conditions(content, from_table: str, target_table: str,
                        on: str) -> list[dict]:
    """解析并校验 ON 条件串 → 结构化条件列表。

    格式: ``t1.c1 = t2.c2 [AND t1.c2 = t2.c2]``(多列 JOIN)。
    约束: 每个片段两侧必须是 from/target 表(各占一侧, 不允许同表);
    列必须真实存在于该表。

    Returns:
        [{"left_table", "left_column", "right_table", "right_column"}, ...]
        left 恒为 from_table 一侧。
    Raises:
        GraphEditError(422): 格式非法 / 表不在这对关系里 / 列不存在。
    """
    on = (on or "").strip()
    if not on:
        raise GraphEditError(422, "ON 条件不能为空")
    parts = [p.strip() for p in _AND_SPLIT_RE.split(on) if p.strip()]
    if not parts:
        raise GraphEditError(422, "ON 条件格式无法解析")

    columns_by_table: dict[str, set[str]] = {
        m.name: {c.name for c in m.columns} for m in content.models}
    conditions: list[dict] = []
    for part in parts:
        m = _ON_PART_RE.match(part)
        if not m:
            raise GraphEditError(
                422, f"ON 条件格式非法: 「{part}」(应为 表.列 = 表.列, 多列用 AND 连接)")
        t1, c1, t2, c2 = m.groups()
        if {t1, t2} != {from_table, target_table} or t1 == t2:
            raise GraphEditError(
                422, f"ON 条件两侧表必须是 {from_table} 与 {target_table}, 实际: {t1} / {t2}")
        for t, c in ((t1, c1), (t2, c2)):
            cols = columns_by_table.get(t)
            if cols is None:
                raise GraphEditError(422, f"表 {t} 不在语义层中")
            if c not in cols:
                raise GraphEditError(422, f"列 {t}.{c} 不存在")
        # 归一化: left 恒为 from 一侧
        if t1 == from_table:
            conditions.append({"left_table": t1, "left_column": c1,
                               "right_table": t2, "right_column": c2})
        else:
            conditions.append({"left_table": t2, "left_column": c2,
                               "right_table": t1, "right_column": c1})
    return conditions


def conditions_to_on(conditions: list[dict]) -> str:
    """结构化条件 → 规范 ON 串(与 parse_on_conditions 互逆)。"""
    return " AND ".join(
        f"{c['left_table']}.{c['left_column']} = "
        f"{c['right_table']}.{c['right_column']}" for c in conditions)


def _pair_key(on: str) -> Optional[frozenset]:
    """ON 串 → 方向无关的列对集合(用于反向关系匹配);解析失败返回 None。"""
    parts = [p.strip() for p in _AND_SPLIT_RE.split(on or "") if p.strip()]
    pairs = set()
    for part in parts:
        m = _ON_PART_RE.match(part)
        if not m:
            return None
        t1, c1, t2, c2 = m.groups()
        pairs.add(frozenset({f"{t1}.{c1}", f"{t2}.{c2}"}))
    return frozenset(pairs) if pairs else None


def _run_rebuild(rebuilder: Optional[Callable[[], Any]]) -> tuple[bool, Optional[str]]:
    """执行索引重建(可选);失败降级返回标志而不是抛异常。"""
    if rebuilder is None:
        return True, None
    try:
        rebuilder()
        return True, None
    except Exception as e:
        logger.warning("图谱变更后索引重建失败(降级): %s", e)
        return False, str(e)[:200]


def add_relationship(
    db, ds_id: str, *,
    from_table: str, target_table: str,
    join_type: str, on: str, cardinality: str,
    index_rebuilder: Optional[Callable[[], Any]] = None,
    expected_version: Optional[int] = None,
) -> dict:
    """新增图谱关系(手工标注 JOIN): 校验 → 追加 → 语义层新版本 → 重建索引。

    Returns:
        {"version", "index_rebuilt", "warning"}
    Raises:
        GraphEditError: 404(语义层/表不存在) / 422(ON 校验失败/自环) /
        409(同 ON 关系已存在)
    """
    from domains.chatbi import semantic
    from domains.chatbi.models import Relationship

    _loaded = semantic.load_content(db, ds_id)
    content, current_version = (_loaded if _loaded[0] is not None
                                 else (None, None))
    if content is None:
        raise GraphEditError(404, "该数据源尚未扫描语义层")
    if expected_version is not None and current_version != expected_version:
        raise GraphEditError(409, f"版本冲突: 当前 v{current_version}, "
                                  f"请求基于 v{expected_version}")
    models_by_name = {m.name: m for m in content.models}
    source_model = models_by_name.get(from_table)
    if source_model is None:
        raise GraphEditError(404, f"源表 {from_table} 不存在")
    # 目标表同样校验——此前只查源表, 不存在的目标表可写入语义层(复核报告)
    if target_table not in models_by_name:
        raise GraphEditError(404, f"目标表 {target_table} 不存在")
    if from_table == target_table:
        raise GraphEditError(422, "源表与目标表不能相同(自环关系请拆中间表)")

    conditions = parse_on_conditions(content, from_table, target_table, on)
    normalized_on = conditions_to_on(conditions)

    # 正向重复(同目标 + 等价 ON, 方向无关) → 409
    new_key = _pair_key(normalized_on)
    for r in source_model.relationships:
        if r.target_model == target_table and (
                r.on == normalized_on or _pair_key(r.on or "") == new_key):
            raise GraphEditError(
                409, f"关系 {from_table} → {target_table} (ON: {normalized_on}) 已存在")

    # name 去重: 同表对多关系时追加序号, 避免同名混淆
    base_name = f"rel_{from_table}_{target_table}"
    existing_names = {r.name for m in content.models for r in m.relationships}
    rel_name = base_name
    suffix = 2
    while rel_name in existing_names:
        rel_name = f"{base_name}_{suffix}"
        suffix += 1

    source_model.relationships.append(Relationship(
        name=rel_name,
        target_model=target_table,
        join_type=join_type,
        on=normalized_on,
        type=cardinality,
        source="manual",
        confidence=1.0,
    ))
    try:
        version = semantic.save_content(db, ds_id, content, source="manual",
                                        expected_version=expected_version)
    except Exception as e:
        if 'VersionConflict' in type(e).__name__:
            # 十二审 8.3: CAS冲突→409(不是500), 前端进专门的409分支
            raise GraphEditError(409, f"图谱已被其他管理员更新——请刷新后重试")
        raise
    index_rebuilt, warning = _run_rebuild(index_rebuilder)
    return {"version": version, "index_rebuilt": index_rebuilt, "warning": warning}


def delete_relationship(
    db, ds_id: str, *,
    from_table: str, target_table: str, on: str = "",
    index_rebuilder: Optional[Callable[[], Any]] = None,
    expected_version: Optional[int] = None,
) -> dict:
    """删除图谱关系: 正向(from→target)与反向(target→from)一起清理。

    on 匹配方向无关: 「orders.user_id = users.id」与
    「users.id = orders.user_id」视为同一条件;on 为空 = 删除该表对全部关系。

    Returns:
        {"version", "removed_forward", "removed_reverse",
         "index_rebuilt", "warning"}
    Raises:
        GraphEditError: 404(语义层不存在 / 无匹配关系)
    """
    from domains.chatbi import semantic

    content = semantic.load_current_content(db, ds_id)
    if content is None:
        raise GraphEditError(404, "该数据源尚未扫描语义层")
    models_by_name = {m.name: m for m in content.models}
    if from_table not in models_by_name:
        raise GraphEditError(404, f"源表 {from_table} 不存在")

    target_key = _pair_key(on) if on else None

    def _matches(rel_target: str, rel_on: str, other: str) -> bool:
        """rel 是某模型上指向 other 的关系, 判断是否命中删除条件。"""
        if rel_target != other:
            return False
        if not on:
            return True  # 未指定 ON = 删除该方向全部
        if rel_on == on:
            return True
        rk = _pair_key(rel_on or "")
        return rk is not None and rk == target_key

    removed_forward = removed_reverse = 0
    source_model = models_by_name.get(from_table)
    if source_model is not None:
        before = len(source_model.relationships)
        source_model.relationships = [
            r for r in source_model.relationships
            if not _matches(r.target_model, r.on or "", target_table)]
        removed_forward = before - len(source_model.relationships)

    # 反向清理(源 ChatBI 语义): target 表上指向 from 的关系一并删除,
    # 否则留下孤立的另一半造成图谱不一致
    target_model = models_by_name.get(target_table)
    if target_model is not None:
        before = len(target_model.relationships)
        target_model.relationships = [
            r for r in target_model.relationships
            if not _matches(r.target_model, r.on or "", from_table)]
        removed_reverse = before - len(target_model.relationships)

    if removed_forward == 0 and removed_reverse == 0:
        raise GraphEditError(
            404, f"未找到匹配关系 {from_table} → {target_table}")

    try:
        version = semantic.save_content(db, ds_id, content, source="manual",
                                        expected_version=expected_version)
    except Exception as e:
        if 'VersionConflict' in type(e).__name__:
            raise GraphEditError(409, f"图谱已被其他管理员更新——请刷新后重试")
        raise
    index_rebuilt, warning = _run_rebuild(index_rebuilder)
    return {"version": version, "removed_forward": removed_forward,
            "removed_reverse": removed_reverse,
            "index_rebuilt": index_rebuilt, "warning": warning}
