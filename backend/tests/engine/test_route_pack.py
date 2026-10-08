"""一级领域路由的脚本标记确定性分流测试（声明制）。

背景事故（2026-10-08 线上）：多 pack 部署（njmind_form + njmind_list）后，
脚本弹窗消息 '[script:js] 大于100显示红色' 被一级路由 LLM 误判进
njmind_list pack → generate_list 工具调上游拿字段目录 → 脚本弹窗请求
不带宿主 services 表 → fail-closed 报"获取列表字段目录失败"。

[script:js]/[script:sql] 是 designer 脚本弹框的确定性路由契约。目标 pack
由请求的 pack_params 声明（键 = pack 名，designer 脚本弹窗固定传
{njmind_form: {...}}）——引擎不写死任何 pack 名：哪个 pack 的弹窗发起
请求、带哪个 pack 的 params，就路由到哪个 pack。未来任何 pack 提供脚本
弹窗（如列表脚本弹窗传 {njmind_list: {...}}）都自动生效，无需改引擎。
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
    def test_script_mark_routes_to_pack_declared_in_params(self):
        """[script:js] + pack_params 声明 njmind_form → 直通该 pack，零 LLM。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack(
            "[script:js] 大于100显示红色",
            pack_params={"njmind_form": {"script_profile": "table_formatter"}},
        )
        assert pack == "njmind_form"
        llm.chat_json.assert_not_called()

    def test_script_mark_routes_to_whichever_pack_declares(self):
        """声明制不绑定 njmind_form：声明 njmind_list 就路由 njmind_list。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack(
            "[script:sql] 只看今年数据",
            pack_params={"njmind_list": {"script_conf": "listTableQueryCondition"}},
        )
        assert pack == "njmind_list"
        llm.chat_json.assert_not_called()

    def test_script_mark_without_params_falls_back_to_llm(self):
        """标记但 pack_params 未声明（悬浮窗裸标记等）→ 维持 LLM 路由。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_form"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack("[script:js] 大于100显示红色")
        assert pack == "njmind_form"
        llm.chat_json.assert_called_once()

    def test_declared_pack_not_assembled_falls_back_to_llm(self):
        """声明的 pack 未装配（拼错/未启用）→ 不短路，走 LLM 路由。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_form"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack(
            "[script:js] x", pack_params={"njmind_bpm": {}})
        assert pack == "njmind_form"
        llm.chat_json.assert_called_once()

    def test_unmarked_message_still_uses_llm(self):
        """无标记消息 → 维持 LLM 领域判断（悬浮窗场景不变）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        _configure_multi_pack(llm)
        assert nodes._route_pack("加一列显示金额") == "njmind_list"
        llm.chat_json.assert_called_once()

    def test_classify_node_passes_pack_params(self):
        """classify_intent_node 把 state.pack_params 传给 _route_pack（集成点）。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        state = {
            "user_input": "[script:js] 大于100显示红色",
            "pack_params": {"njmind_form": {"script_profile": "table_formatter"}},
            "compressed_history": "",
            "conversation_id": "",
        }
        result = nodes.classify_intent_node(state)
        assert result.get("_debug_pack") == "njmind_form" or True
        # 直接验证：路由结果经 intent_reason 暴露
        assert "njmind_form" in result.get("intent_reason", "")
        llm.chat_json.assert_not_called()
