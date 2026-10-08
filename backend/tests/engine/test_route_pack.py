"""一级领域路由的 packs 子集声明测试（与 INIT packs 同名同义）。

契约：宿主嵌入窗口时 INIT 下发的 packs（插件链路声明）沿用到 chat 请求
——chat.packs 非空时一级路由只在声明子集内决策：
- 子集为单插件 → 直通（零 LLM，弹窗/单域窗口 100% 命中）；
- 子集为多插件 → LLM 仅在子集内路由（候选收窄，误路由面变小）；
- 未声明 → 维持全量 LLM 语义路由（独立模式/旧客户端不变）。

背景：多 pack 部署后 LLM 一级路由曾把脚本弹窗消息误判进别的 pack
（'大于100显示红色'像列表渲染需求）→ 列表工具调上游 → 弹窗请求不带
宿主 services 表 → fail-closed 误报。单一 packs 概念（INIT/meta/chat
三处同名同义）根治：窗口初始化声明什么链路，后续消息就只在什么链路里走。
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
        pack_routers={"njmind_form": MagicMock(), "njmind_list": MagicMock(),
                      "njmind_bpm": MagicMock()},
        pack_configs={
            "njmind_form": {"domain": {"description": "表单配置", "fallback": True}},
            "njmind_list": {"domain": {"description": "列表页配置"}},
            "njmind_bpm": {"domain": {"description": "流程配置"}},
        },
    )


class TestPacksSubsetRouting:
    def test_single_pack_subset_short_circuits(self):
        """声明集只有 1 个插件 → 直通，零 LLM（弹窗/单域窗口场景）。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack("[script:js] 大于100显示红色",
                                 packs=["njmind_form"])
        assert pack == "njmind_form"
        llm.chat_json.assert_not_called()

    def test_multi_pack_subset_routes_within_subset(self):
        """声明集多个插件 → LLM 只在子集内路由（子集外不可达）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack("加一列显示金额", packs=["njmind_list", "njmind_bpm"])
        assert pack == "njmind_list"
        # LLM 的候选清单只含声明的两个插件（prompt 里不含 njmind_form）
        prompt_sent = llm.chat_json.call_args[0][0][0]["content"]
        assert "njmind_list" in prompt_sent and "njmind_bpm" in prompt_sent
        assert "njmind_form" not in prompt_sent

    def test_undeclared_still_full_llm(self):
        """未声明 → 维持全量 LLM 语义路由（独立模式不变）。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_list"}
        _configure_multi_pack(llm)
        assert nodes._route_pack("加一列显示金额") == "njmind_list"
        prompt_sent = llm.chat_json.call_args[0][0][0]["content"]
        assert "njmind_form" in prompt_sent  # 全量候选

    def test_subset_with_unassembled_pack_filters_it_out(self):
        """声明集含未装配插件（拼错/未启用）→ 过滤掉，不影响其余声明。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        pack = nodes._route_pack("x", packs=["njmind_form", "ghost_pack"])
        assert pack == "njmind_form"  # 过滤后只剩 1 个 → 直通
        llm.chat_json.assert_not_called()

    def test_subset_all_unassembled_falls_back_to_full(self):
        """声明集全部未装配 → 等于无有效声明，回全量 LLM。"""
        llm = MagicMock()
        llm.chat_json.return_value = {"pack": "njmind_form"}
        _configure_multi_pack(llm)
        pack = nodes._route_pack("x", packs=["ghost_a", "ghost_b"])
        assert pack == "njmind_form"
        llm.chat_json.assert_called_once()

    def test_classify_node_passes_packs_from_state(self):
        """classify_intent_node 从 state 读 packs 声明传给 _route_pack。"""
        llm = MagicMock()
        _configure_multi_pack(llm)
        state = {
            "user_input": "[script:js] 大于100显示红色",
            "packs": ["njmind_form"],
            "compressed_history": "",
            "conversation_id": "",
        }
        result = nodes.classify_intent_node(state)
        assert "njmind_form" in result.get("intent_reason", "")
        llm.chat_json.assert_not_called()
