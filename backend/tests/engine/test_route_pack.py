"""一级领域路由的脚本标记确定性分流测试。

背景事故（2026-10-08 线上）：多 pack 部署（njmind_form + njmind_list）后，
脚本弹窗消息 '[script:js] 大于100显示红色' 被一级路由 LLM 误判进
njmind_list pack → generate_list 工具调上游拿字段目录 → 脚本弹窗请求
不带宿主 services 表 → fail-closed 报"获取列表字段目录失败"。

[script:js]/[script:sql] 是 designer 脚本弹框的确定性路由契约（见
njmind_form/router.py 的二级分流），必须在一级路由就短路直通 njmind_form，
不交给 LLM 概率判断。
"""
from unittest.mock import MagicMock

from engine import nodes


def _configure_multi_pack(llm):
    nodes.configure(
        registry=MagicMock(),
        llm_client=llm,
        asset_client=None,
        conversation=None,
        prompt_loader=MagicMock(),
        pack_routers={"njmind_form": MagicMock(), "njmind_list": MagicMock()},
        pack_configs={
            "njmind_form": {"domain": {"description": "表单配置", "fallback": True}},
            "njmind_list": {"domain": {"description": "列表页配置"}},
        },
    )


class TestScriptMarkPackRouting:
    def test_script_js_mark_short_circuits_to_form_pack(self):
        """[script:js] 标记 → 确定性直通 njmind_form，零 LLM 调用。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        assert nodes._route_pack("[script:js] 大于100显示红色") == "njmind_form"
        llm.chat_json.assert_not_called()

    def test_script_sql_mark_short_circuits_to_form_pack(self):
        llm = MagicMock()
        _configure_multi_pack(llm)
        assert nodes._route_pack("[script:sql] 只看今年数据") == "njmind_form"
        llm.chat_json.assert_not_called()

    def test_unmarked_message_still_uses_llm(self):
        """无标记消息 → 维持 LLM 领域判断（悬浮窗场景不变）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        _configure_multi_pack(llm)
        assert nodes._route_pack("加一列显示金额") == "njmind_list"
        llm.chat_json.assert_called_once()

    def test_form_pack_missing_falls_back_to_llm(self):
        """njmind_form 未装配时标记不短路（单 pack 直通本就覆盖）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        nodes.configure(
            registry=MagicMock(),
            llm_client=llm,
            asset_client=None,
            conversation=None,
            prompt_loader=MagicMock(),
            pack_routers={"njmind_list": MagicMock()},
            pack_configs={"njmind_list": {"domain": {"description": "列表页配置"}}},
        )
        assert nodes._route_pack("[script:js] x") == "njmind_list"
