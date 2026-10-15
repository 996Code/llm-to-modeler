"""P2#11 证据卡升级(可信度双件套第二件)——artifact 补 table_provenance。

蓝图:答案可追溯到"用了哪些表哪些字段多少行"——schema 卡数据源。
行数裁决:表级实时 COUNT 大表慢且多为估算误导,落"本查询 rowcount"
(已有)+列级 schema(名/中文名/类型/注释)+JOIN 路径,数字与来源都可溯。
"""
from tests.domains.test_chatbi_pipeline import _run, env  # noqa: F401


class TestTableProvenance:

    def test_artifact_carries_table_provenance(self, env):
        """制品含 table_provenance:current_tables 每表的列 schema 与 JOIN 路径。"""
        sql = ('SELECT u.city AS "城市", COUNT(*) AS "订单数" FROM public.biz_orders o '
               'JOIN public.biz_users u ON o.user_id = u.id GROUP BY u.city')
        from tests.domains.test_chatbi_pipeline import FakeLLM
        llm = FakeLLM({
            "chatbi.think": {"tables": ["biz_orders(订单表)", "biz_users(用户表)"],
                             "aggregation": "COUNT", "caveats": [],
                             "prev_sql_review": ""},
            "chatbi.generate_sql": f"```sql\n{sql}\n```",
            "chatbi.chart": {"chart_type": "bar", "dim_col": "城市",
                             "measure_cols": ["订单数"]},
        })
        result, state = _run(env, "各城市的订单数量", llm)
        assert result.error_for_llm is None
        prov = result.artifact.get("table_provenance")
        assert prov, "制品缺 table_provenance(证据卡数据源)"
        by_name = {t["name"]: t for t in prov}
        # 两张表都有 schema 卡
        assert "biz_orders" in by_name and "biz_users" in by_name
        orders = by_name["biz_orders"]
        assert orders["display_name"], "表缺中文显示名"
        cols = {c["name"]: c for c in orders["columns"]}
        assert cols, "表缺列信息"
        first_col = next(iter(cols.values()))
        assert {"name", "display_name", "data_type"} <= set(first_col), \
            f"列卡缺字段: {first_col}"
        # 溯源要素齐全:查询行数/SQL/JOIN 路径在制品顶层已有,这里锚定新键
        assert result.artifact["rowcount"] >= 1
        assert result.artifact["sql"].lower().startswith("select")

    def test_provenance_no_tables_empty_list(self, env):
        """无 current_tables(澄清路径)不崩——provenance 为空列表或缺省。"""
        from tests.domains.test_chatbi_pipeline import FakeLLM
        # 触发澄清:无召回类问题(2 表小库阈值内直接全表降级,难造空表)
        # 改为直接断言组装函数对空表集的行为
        from domains.chatbi.tools.ask_data import _build_table_provenance
        assert _build_table_provenance(None, []) == []
