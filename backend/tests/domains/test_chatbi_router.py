"""ChatBIKeywordRouter 路由正确性回归(2026-11 功能测试加固)。

误路由是确定性路由的唯一风险面——这里锁定每类输入的期望去向,
特别是"首查带图表词"不得误切 switch_chart 的边界。
"""
from types import SimpleNamespace

from domains.chatbi import pack as chatbi_pack


def _registry(*names):
    return SimpleNamespace(all=lambda: [SimpleNamespace(name=n) for n in names])


ROUTER = chatbi_pack.ChatBIKeywordRouter(_registry("ask_data", "switch_chart", "query_stats"))


def route(text):
    return ROUTER.route(text)[0]


class TestChartSwitchRouting:
    def test_switch_phrase_routes_to_switch_chart(self):
        assert route("换成饼图") == "switch_chart"
        assert route("把图表切换成折线图") == "switch_chart"
        assert route("改用柱状图展示") == "switch_chart"

    def test_cold_switch_gets_friendly_guidance_path(self):
        """冷启动"换成饼图"也走 switch_chart(其无 prev_sql 分支给引导)。"""
        assert route("换成表格") == "switch_chart"

    def test_first_query_with_chart_word_stays_ask_data(self):
        """首查带图表诉求(无切换动词)必须归 ask_data——误切会让用户
        收到"没有可切换的历史查询"而无法完成首次查询(功能回归红线)。"""
        assert route("统计每个城市的订单数量并画出柱状图") == "ask_data"
        assert route("查一下最近三个月的销售额,画个饼图") == "ask_data"
        assert route("各门店业绩对比,用表格给我") == "ask_data"

    def test_chart_word_without_verb_is_ask_data(self):
        assert route("饼图") == "ask_data"
        assert route("看看订单趋势折线") == "ask_data"


class TestQueryStatsRouting:
    def test_meta_questions_route_to_stats(self):
        assert route("最近查询质量怎么样") == "query_stats"
        assert route("慢查询多不多") == "query_stats"

    def test_long_data_query_with_stats_substring_stays_ask_data(self):
        """"查询统计每个城市的订单量"是数据查询,不是运行质量元问题。"""
        assert route("查询统计每个城市的订单量并排序") == "ask_data"

    def test_normal_data_query_routes_to_ask_data(self):
        assert route("上个月销售额是多少") == "ask_data"
        assert route("各城市订单数量") == "ask_data"


class TestZeroLLM:
    def test_router_never_calls_llm(self):
        class Boom:
            def chat_json(self, *a, **kw):
                raise AssertionError("确定性路由不得调用 LLM")

        for text in ("换成饼图", "查询质量怎么样", "随便什么问题"):
            name, conf = ROUTER.route(text, llm_client=Boom())
            assert name in ("ask_data", "switch_chart", "query_stats")
            assert conf == 1.0
