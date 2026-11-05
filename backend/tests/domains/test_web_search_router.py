"""web_search 独立后 KG 路由回归:一律 kb_search(联网由一级路由分发)。

原开关分流测试随 web_search 独立成 pack 而废弃——分流语义迁移至:
- web_search pack 工具 preflight 开关门(test_web_search_pack.py)
- 客服 other 分支直连(test_web_search_metrology.py)
"""
from domains.knowledge_graph.pack import create_router


def test_kg_router_always_returns_kb_search():
    r = create_router(None)
    # 开关开+联网话术也不抢(独立 pack 后一级路由按 domain 声明分发)
    assert r.route("上网查一下最新规程", None,
                   pack_params={"knowledge_graph": {"web_search": True}}) == ("kb_search", 1.0)
    assert r.route("普通知识问题", None) == ("kb_search", 1.0)
