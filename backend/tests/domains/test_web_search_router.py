"""web_search 对话开关路由测试——pack_params.knowledge_graph.web_search。

开关开+话术明确要联网 → web_search;开关关或普通问题 → kb_search(默认)。
另覆盖:路由参数协商(旧 route 无 pack_params 参数不炸)。
"""
from domains.knowledge_graph.pack import KnowledgeQuestionRouter, create_router


def _router():
    return create_router(None)


def test_switch_on_with_web_hint_routes_to_web_search():
    r = _router()
    out = r.route("上网查一下最新的压力表检定规程", None,
                  pack_params={"knowledge_graph": {"web_search": True}})
    assert out == ("web_search", 0.9)


def test_switch_on_plain_question_stays_kb_search():
    """开关开但话术没点明联网(保守触发):仍走知识库。"""
    r = _router()
    out = r.route("压力表检定规程是什么", None,
                  pack_params={"knowledge_graph": {"web_search": True}})
    assert out == ("kb_search", 1.0)


def test_switch_off_never_routes_to_web_search():
    """开关关(默认):即使话术点明联网也不走(用户没开权限)。"""
    r = _router()
    out = r.route("上网查一下最新的规程", None,
                  pack_params={"knowledge_graph": {"web_search": False}})
    assert out == ("kb_search", 1.0)


def test_no_pack_params_defaults_to_kb_search():
    """旧宿主/独立模式未传 pack_params:行为与开关关一致。"""
    r = _router()
    assert r.route("上网查一下", None) == ("kb_search", 1.0)


def test_hint_words_coverage():
    """信号词表逐词验证(保守词表:普通'查一下/最新'不在内)。"""
    r = _router()
    params = {"knowledge_graph": {"web_search": True}}
    for hint in ("上网查", "联网搜", "网上查", "搜索引擎", "互联网上"):
        assert r.route(f"帮我{hint}JJG 544", None, pack_params=params)[0] == "web_search", hint
    # 普通话术不触发
    for plain in ("查一下JJG 544", "最新的检定规程", "知识库里有什么"):
        assert r.route(plain, None, pack_params=params)[0] == "kb_search", plain


def test_legacy_route_signature_without_pack_params():
    """旧 pack 路由(无 pack_params 参数)在引擎协商下不被传参,不炸。"""
    class LegacyRouter:
        def route(self, user_input, artifact, history="", llm_client=None,
                  conv_id=None):
            return "kb_search", 1.0

    out = LegacyRouter().route("上网查一下", None)
    assert out == ("kb_search", 1.0)


def test_router_accepts_pack_params_kwarg():
    """**kwargs 路由同样接受 pack_params(协商的 VAR_KEYWORD 分支)。"""
    class KwargRouter:
        def route(self, user_input, artifact, **kwargs):
            return "kb_search", 1.0

    out = KwargRouter().route("q", None, pack_params={"x": 1})
    assert out == ("kb_search", 1.0)
