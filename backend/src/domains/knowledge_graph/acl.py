"""知识库检索数据权限(嵌入模式)。

【身份来源】(用户裁决 2026-10-17:角色走 pack_params 透传,不做同步表)
  - user_id:宿主网关注入的 X-User-Id(引擎已有通道,ctx.user_id)
  - roles:宿主按请求透传 pack_params.knowledge_graph.user_roles
    (list 或逗号分隔串)——角色永远是最新的,宿主侧自管,引擎零改动

【判定语义】
  - 未传 user_id 且未传 roles → 完全不过滤(开放;独立模式即此形态)
  - 库未配置 ACL(kg_kb_acl 无该库行) → 全放行(默认开放)
  - 配置了 ACL → user_id 命中"允许用户"或任一角色命中"允许角色"才放行
  - 管理端(retrieve-test 等 admin 门内)不受限——admin 已是更高权限面

【生效点】kb_search 工具(对话)与 POST /search(REST)共用本模块;
两处的库解析逻辑各自接入,判定只有一份。
"""
from typing import Any, Dict, List, Optional, Tuple


def request_identity(user_id: Optional[str], roles_raw: Any) -> Tuple[str, List[str]]:
    """归一请求身份:anonymous/空 = 未传;roles 接受 list/逗号串。"""
    user = (user_id or "").strip()
    if user == "anonymous":
        user = ""
    roles: List[str] = []
    if isinstance(roles_raw, str):
        roles = [r.strip() for r in roles_raw.split(",") if r.strip()]
    elif isinstance(roles_raw, (list, tuple)):
        roles = [str(r or "").strip() for r in roles_raw if str(r or "").strip()]
    return user, roles


def has_identity(user: str, roles: List[str]) -> bool:
    """宿主是否传了身份——没传则整条 ACL 链路短路(不过滤)。"""
    return bool(user or roles)


def _kb_allowed(entries: List[Dict[str, str]], user: str, roles: List[str]) -> bool:
    for e in entries:
        if e["subjectType"] == "user" and user and e["subjectId"] == user:
            return True
        if e["subjectType"] == "role" and roles and e["subjectId"] in roles:
            return True
    return False


def filter_kbs(store, kbs: List[Dict[str, Any]],
               user: str, roles: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按 ACL 过滤库列表。返回 (可访问库, 被拒库名)。

    无身份时原样返回(调用方也可先 has_identity 短路,这里双保险)。
    """
    if not has_identity(user, roles):
        return kbs, []
    acl_map = store.get_all_kb_acl()
    out: List[Dict[str, Any]] = []
    denied: List[str] = []
    for kb in kbs:
        entries = acl_map.get(kb["id"])
        if not entries or _kb_allowed(entries, user, roles):
            out.append(kb)
        else:
            denied.append(kb.get("name") or kb["id"])
    return out, denied


def kb_allowed(store, kb_id: str, user: str, roles: List[str]) -> bool:
    """单库判定(显式指定库的场景)。无身份=放行;无 ACL=放行。"""
    if not has_identity(user, roles):
        return True
    entries = store.get_all_kb_acl().get(kb_id)
    if not entries:
        return True
    return _kb_allowed(entries, user, roles)
