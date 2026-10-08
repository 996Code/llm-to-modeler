"""一级领域路由的显式 pack 声明测试（平台化契约）。

契约：谁发起请求，谁声明插件；引擎照声明路由。
- ChatRequest.pack（宿主/前端显式声明"本请求路由到该插件"）非空且该
  pack 已装配 → 一级路由直通（零 LLM 调用）。
- 未声明/声明的 pack 未装配 → 维持原路由行为（单 pack 直通 / LLM 语义
  判断 / fallback）。

背景：多 pack 部署后 LLM 一级路由曾把脚本弹窗消息误判进别的 pack
（'大于100显示红色'像列表渲染需求）→ 列表工具调上游 → 弹窗请求不带
宿主 services 表 → fail-closed 误报。显式声明让弹窗类强契约场景
100% 命中，引擎零领域知识（不写死 pack 名、不嗅探消息标记）。
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


class TestExplicitPackRouting:
    def test_target_pack_short_circuits(self):
        """请求声明 pack → 直通该 pack，零 LLM 调用（引擎不猜）。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack("大于100显示红色", target_pack="njmind_form")
        assert pack == "njmind_form"
        llm.chat_json.assert_not_called()

    def test_target_pack_is_whatever_host_says(self):
        """声明不绑定任何固定 pack：声明 njmind_list 就路由 njmind_list。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack("随便什么话术", target_pack="njmind_list")
        assert pack == "njmind_list"
        llm.chat_json.assert_not_called()

    def test_undeclared_message_still_uses_llm(self):
        """未声明 → 维持 LLM 语义路由（悬浮窗自然语言场景不变）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        _configure_multi_pack(llm)
        assert nodes._route_pack("加一列显示金额") == "njmind_list"
        llm.chat_json.assert_called_once()

    def test_target_pack_not_assembled_falls_back_to_llm(self):
        """声明的 pack 未装配（拼错/未启用）→ 不短路，走 LLM 路由。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_form"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack("x", target_pack="njmind_bpm")
        assert pack == "njmind_form"
        llm.chat_json.assert_called_once()

    def test_classify_node_passes_target_pack_from_state(self):
        """classify_intent_node 从 state 读宿主声明并传给 _route_pack。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        state = {
            "user_input": "[script:js] 大于100显示红色",
            "target_pack": "njmind_form",
            "pack_params": {"njmind_form": {"script_profile": "table_formatter"}},
            "compressed_history": "",
            "conversation_id": "",
        }
        result = nodes.classify_intent_node(state)
        assert "njmind_form" in result.get("intent_reason", "")
        llm.chat_json.assert_not_called()

    def test_no_message_sniffing_in_engine(self):
        """引擎不再嗅探消息标记：裸 [script:js] 无声明时走 LLM（标记只归
        pack 二级路由消费——njmind_form/router.py 的既有契约）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_form"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack("[script:js] 大于100显示红色")
        assert pack == "njmind_form"
        llm.chat_json.assert_called_once()
