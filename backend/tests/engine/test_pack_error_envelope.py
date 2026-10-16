"""D6 错误契约信封 + D4 工具输出类型 —— 引擎侧映射与 suppress_output。

D6:工具抛 PackInvokeError{retryable/permanent/credential},引擎按类分流
(retryable 同参数自动重试一次;permanent/credential 终止回流带类型标注;
普通异常行为与之前完全一致)。

D4:ToolResult.suppress_output=True 的数据制品,SSE result 事件透传
suppressOutput/link;stream 持久化时 LLM 可见历史(assistant content)
换短指针,制品(data_artifact)完整落库。
"""
from unittest.mock import MagicMock

import pytest

try:
    import langgraph  # noqa: F401
    _HAS_LANGGRAPH = True
except ImportError:
    _HAS_LANGGRAPH = False

from sdk.registry import ToolRegistry
from sdk.tool import PackInvokeError, Tool, ToolContext, ToolResult

pytestmark = pytest.mark.skipif(
    not _HAS_LANGGRAPH, reason="langgraph package not installed"
)


# ── 测试替身 ─────────────────────────────────────────────────

class _ScriptedTool(Tool):
    """按脚本抛错/返回的工具:raises 里的异常依次抛,耗尽后返回 ok 结果。"""

    name = "scripted_tool"
    description = "脚本化错误工具"
    when = "测试"

    def __init__(self, raises=None, ok_result=None):
        self.raises = raises or []
        self.ok_result = ok_result or ToolResult(
            artifact={"rows": [1, 2]}, artifact_type="data", summary="成功")
        self.execute_calls = 0

    def input_schema(self) -> dict:
        return {}

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        self.execute_calls += 1
        if self.raises:
            raise self.raises.pop(0)
        return self.ok_result


def _drive(tool, state_extra=None):
    """配置引擎节点并直驱 execute_tool_node(同 test_clarify_gate 模式)。"""
    from engine import nodes
    registry = ToolRegistry()
    registry.register(tool)
    nodes.configure(registry=registry, llm_client=MagicMock(),
                    asset_client=MagicMock(), conversation=None,
                    prompt_loader=None, pack_routers={}, pack_configs={})
    state = {
        "tool_name": tool.name,
        "tool_state": {"user_input": "x", "conversation_id": "",
                       "forward_headers": {}},
        "conversation_id": "",
    }
    state.update(state_extra or {})
    return nodes.execute_tool_node(state)


# ── D6:三类映射 ─────────────────────────────────────────────

class TestPackErrorEnvelope:

    def test_retryable_retries_once_then_succeeds(self):
        """retryable(网络抖动)→ 引擎同参数重试一次,重试成功则正常返回。"""
        tool = _ScriptedTool(raises=[PackInvokeError("网关瞬时 500", PackInvokeError.RETRYABLE)])
        update = _drive(tool)
        assert tool.execute_calls == 2                    # 首次失败 + 重试一次
        result = ToolResult.model_validate(update["tool_result"])
        assert result.artifact == {"rows": [1, 2]}        # 重试成功,正常结果

    def test_retryable_retry_fails_again_annotated(self):
        """retryable 重试再失败 → 回流错误带"已自动重试"标注(不无限重试)。"""
        tool = _ScriptedTool(raises=[
            PackInvokeError("网关持续 500", PackInvokeError.RETRYABLE),
            PackInvokeError("网关持续 500", PackInvokeError.RETRYABLE),
        ])
        update = _drive(tool)
        assert tool.execute_calls == 2
        result = ToolResult.model_validate(update["tool_result"])
        assert "已自动重试" in result.error_for_llm

    def test_permanent_terminates_without_retry(self):
        """permanent(恒定失败)→ 不重试直接终止,错误带"永久性"标注。"""
        tool = _ScriptedTool(raises=[
            PackInvokeError("参数校验拒绝: 不存在的表", PackInvokeError.PERMANENT)])
        update = _drive(tool)
        assert tool.execute_calls == 1                    # 永不重试
        result = ToolResult.model_validate(update["tool_result"])
        assert "永久性" in result.error_for_llm
        assert "参数校验拒绝" in result.error_for_llm

    def test_credential_terminates_with_config_hint(self):
        """credential(密钥错)→ 终止并提示检查插件配置/密钥。"""
        tool = _ScriptedTool(raises=[
            PackInvokeError("数据源密码解密失败", PackInvokeError.CREDENTIAL)])
        update = _drive(tool)
        assert tool.execute_calls == 1
        result = ToolResult.model_validate(update["tool_result"])
        assert "请检查插件配置/密钥" in result.error_for_llm

    def test_normal_exception_behavior_unchanged(self):
        """普通异常(非信封)→ 兜底包装,行为与 D6 之前完全一致(向后兼容)。"""
        tool = _ScriptedTool(raises=[RuntimeError("任意崩溃")])
        update = _drive(tool)
        assert tool.execute_calls == 1                    # 不重试
        result = ToolResult.model_validate(update["tool_result"])
        assert result.error_for_llm == "任意崩溃"          # 原样,无类型标注

    def test_invalid_error_type_rejected(self):
        """信封构造防呆:非法 error_type 直接 ValueError(协议错误当场暴露)。"""
        with pytest.raises(ValueError):
            PackInvokeError("x", "unknown_type")


# ── D4:suppress_output 透传与持久化 ─────────────────────────

class _CaptureStore:
    """捕获 add_message 调用的假会话存储。"""

    def __init__(self):
        self.messages = []

    def add_message(self, **kw):
        self.messages.append(kw)

    def update_conversation_config(self, *a, **kw):
        pass


class TestSuppressOutput:

    def _result_state(self, result: ToolResult) -> dict:
        return {"tool_result": result.model_dump()}

    def test_sse_payload_carries_suppress_flag(self):
        """handle_result_node:suppress_output 的数据制品,SSE result 事件
        透传 suppressOutput + link(持久化层据此分流)。"""
        from engine import nodes
        result = ToolResult(
            artifact={"items": [{"query_count": 42}]}, artifact_type="data",
            summary="共 42 次查询", suppress_output=True, link="/admin#metrics")
        update = nodes.handle_result_node(self._result_state(result))
        payload = update["sse_events"][0]["data"]
        assert payload["suppressOutput"] is True
        assert payload["link"] == "/admin#metrics"
        assert payload["data"] == {"items": [{"query_count": 42}]}  # 制品完整

    def test_sse_payload_without_flag_unchanged(self):
        """未声明 suppress_output 的数据制品:payload 不带该键(零影响)。"""
        from engine import nodes
        result = ToolResult(artifact={"rows": []}, artifact_type="data",
                            summary="普通结果")
        update = nodes.handle_result_node(self._result_state(result))
        payload = update["sse_events"][0]["data"]
        assert "suppressOutput" not in payload and "link" not in payload

    def test_persisted_history_pointer_not_json(self):
        """stream 持久化:suppressOutput → LLM 可见历史(assistant content)
        是短指针不含 JSON;制品(data_artifact)完整落库。"""
        from engine.stream import _save_result_conversation
        store = _CaptureStore()
        result_data = {
            "artifactType": "data",
            "data": {"items": [{"query_count": 42, "avg_ms": 123,
                                "error_rate": 1.5}]},
            "summary": "共 42 次查询, 平均 123ms, 错误率 1.5%",
            "suppressOutput": True,
            "link": "/admin#metrics",
        }
        _save_result_conversation(store, "conv-1", "u1", "查询质量怎么样",
                                   result_data, None)
        assistant = [m for m in store.messages if m["role"] == "assistant"][0]
        # LLM 可见文本 = 指针:不含统计 JSON,含 link 入口
        assert "query_count" not in assistant["content"]
        assert "不占用对话上下文" in assistant["content"]
        assert "/admin#metrics" in assistant["content"]
        # 制品完整(消息存储的一部分,历史回放渲染明细卡)
        assert assistant["data_artifact"] == result_data["data"]

    def test_persisted_history_normal_summary_kept(self):
        """未 suppress 的结果:assistant content 照旧是 summary(回归)。"""
        from engine.stream import _save_result_conversation
        store = _CaptureStore()
        _save_result_conversation(store, "conv-1", "u1", "问",
                                  {"artifactType": "data", "data": {"r": 1},
                                   "summary": "查询完成, 返回 3 行"}, None)
        assistant = [m for m in store.messages if m["role"] == "assistant"][0]
        assert assistant["content"] == "查询完成, 返回 3 行"


# ── D4 试点工具:chatbi query_stats ─────────────────────────

class TestQueryStatsTool:

    def test_returns_suppressed_artifact_with_link(self, monkeypatch):
        """query_stats 工具:统计明细进 artifact + suppress_output + link,
        summary 只有一句话结论(LLM 可见文本不烧 token)。"""
        from types import SimpleNamespace
        from domains.chatbi.tools.query_stats import QueryStatsTool
        from domains.chatbi import datasources as ds_mod, query_stats as qs_mod

        monkeypatch.setattr(ds_mod, "resolve_datasource",
                            lambda db, ds_id, decrypt=True: SimpleNamespace(
                                id="ds1", name="销售库"))
        metrics = [{"data_source_id": "ds1", "query_count": 42, "avg_ms": 123,
                    "max_ms": 900, "error_count": 1, "error_rate": 2.4,
                    "slow_count": 3, "heal_count": 1,
                    "retrieval_degraded_count": 0, "chart_degraded_count": 0,
                    "ask_user_count": 2}]
        monkeypatch.setattr(qs_mod, "datasource_metrics",
                            lambda db, **kw: metrics)

        tool = QueryStatsTool(app_state=None, db=MagicMock())
        ctx = MagicMock()
        ctx.session_state = None
        result = tool.execute({"user_input": "查询质量怎么样"}, ctx)

        assert result.suppress_output is True
        assert result.link and "admin" in result.link
        assert result.artifact["items"] == metrics        # 明细完整
        assert "42" in result.summary and len(result.summary) < 80

    def test_empty_stats_honest_summary(self, monkeypatch):
        """无查询记录:诚实提示(不是 suppress 的场景,普通 data 结果)。"""
        from types import SimpleNamespace
        from domains.chatbi.tools.query_stats import QueryStatsTool
        from domains.chatbi import datasources as ds_mod, query_stats as qs_mod

        monkeypatch.setattr(ds_mod, "resolve_datasource",
                            lambda db, ds_id, decrypt=True: SimpleNamespace(
                                id="ds1", name="空库"))
        monkeypatch.setattr(qs_mod, "datasource_metrics", lambda db, **kw: [])

        tool = QueryStatsTool(app_state=None, db=MagicMock())
        result = tool.execute({"user_input": "查询质量怎么样"}, MagicMock())
        assert "暂无查询记录" in result.summary
