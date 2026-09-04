"""KbSearchTool - 前台对话工具:知识库检索问答(GraphRAG)。

【路由】
  引擎一级路由按 manifest domain.description 选中 knowledge_graph 域,
  二级路由(DefaultPackRouter 中性框架)按 name/when 选中本工具。

【多库语义】
  state 带 kb 参数(如"在产品手册库里查")→ 按名解析;
  未指定且只有一库 → 自动选中;未指定且多库 → AskSpec 追问(引擎
  interrupt/resume 原生支持,用户带 answers 重发)。

【产物】
  artifact_type="data":{subgraph, sources} —— 前端通用数据卡展示,
  子图结构完整返回(图谱可视化页同构)。
"""
import logging
from typing import Any, Optional

from sdk.tool import AskOption, AskQuestion, AskSpec, Tool, ToolContext, ToolResult

logger = logging.getLogger(__name__)


class KbSearchTool(Tool):
    """知识库检索问答。"""

    name = "kb_search"
    description = "在知识库(知识图谱)中检索事实并回答:实体属性、关系链、制度条款、文档内容"
    when = ("用户想基于已导入的知识库/文档回答事实性问题,如'张伟向谁汇报'"
            "'XX 和 YY 是什么关系''知识库里告警怎么升级'")

    def __init__(self, app_state: Any = None):
        # app_state 由 create_registry(app_state) 装配时注入(平台组件入口)
        self._app_state = app_state

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户消息(检索问题)"},
                "kb": {"type": "string", "description": "知识库名(可选;多库时指定)"},
            },
            "required": ["user_input"],
        }

    def validate_input(self, state: dict) -> Optional[str]:
        if not (state.get("user_input") or state.get("query") or "").strip():
            return "缺少检索问题(user_input)"
        return None

    def preflight(self, state: dict, ctx: ToolContext) -> Optional[ToolResult]:
        """执行前提:插件存储可达(依赖检测在加载期已保证;这里防运行期掉线)。"""
        if not self._app_state:
            return ToolResult(error_for_llm="知识库插件未正确装配(app_state 未注入)")
        from domains.knowledge_graph import runtime
        try:
            runtime.get_kg_store(self._app_state).list_kbs()
        except Exception as e:
            return ToolResult(error_for_llm=f"知识库存储暂不可用: {e}")
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        from domains.knowledge_graph import retrieval, runtime

        query = (state.get("user_input") or state.get("query") or "").strip()
        kb_hint = (state.get("kb") or "").strip()
        store = runtime.get_kg_store(self._app_state)

        # ── 知识库解析(指定名 > 追问答案 > 唯一库自动 > 多库追问) ──
        kb = None
        if kb_hint:
            kb = store.get_kb_by_name(kb_hint)
            if not kb:
                return ToolResult(error_for_llm=f"知识库「{kb_hint}」不存在")
        else:
            # 追问恢复:interrupt 后引擎把用户回答注入 tool_state["clarify_answers"]
            # (见 engine/nodes.py 的追问恢复注入,与 njmind_form 同一约定)
            answers = state.get("clarify_answers") or {}
            chosen = str(answers.get("kb") or answers.get("知识库") or "").strip()
            kbs = store.list_kbs()
            if chosen:
                kb = next((k for k in kbs if k["name"] == chosen), None)
            if kb is None and len(kbs) == 1:
                kb = kbs[0]
            elif kb is None and kbs:
                return ToolResult(ask=AskSpec(questions=[AskQuestion(
                    question="要在哪个知识库里检索?",
                    header="知识库",
                    options=[AskOption(label=k["name"],
                                       description=k.get("description") or "") for k in kbs[:4]],
                )]))
            elif kb is None:
                return ToolResult(
                    reply="当前还没有任何知识库。请先在「知识图谱」管理页创建知识库并导入文档。",
                    summary="无可用知识库")

        # ── 混合检索 + 回答 ──
        ctx.emit("stage", "kb_search.retrieve", message=f"正在检索知识库「{kb['name']}」…")
        ctx.trace("kb_search.retrieve", f"检索 {kb['name']}", "info")
        try:
            result = retrieval.answer_question(
                self._app_state, kb, query, conv_id=ctx.conv_id)
        except Exception as e:
            logger.exception("kb_search 检索失败")
            return ToolResult(error_for_llm=f"知识库检索失败: {e}")

        ctx.emit("stage", "kb_search.answer", message="正在综合回答…")
        ctx.trace("kb_search.answer", "生成回答", "ok")
        sub = result.get("subgraph") or {}
        # 【ToolResult 三态契约】reply 与 artifact 互斥(引擎 reply 优先):
        # data 制品的展示文本放 summary —— 气泡显示回答,下方数据卡渲染子图
        return ToolResult(
            artifact={
                "type": "kg_search_result",
                "kb": result["kb"],
                "subgraph": sub,
                "sources": result.get("sources") or {},
            },
            artifact_type="data",
            summary=result.get("answer") or "(模型未返回回答)",
            extra={"intent": result.get("intent"), "chunkCount": len(result.get("chunks") or [])},
        )

    def format_result(self, artifact: dict) -> dict:
        """SSE data 制品卡摘要字段(引擎试金石:不读制品内部结构)。"""
        sub = (artifact or {}).get("subgraph") or {}
        sources = (artifact or {}).get("sources") or {}
        return {
            "nodeCount": len(sub.get("nodes") or []),
            "edgeCount": len(sub.get("edges") or []),
            "chunkHits": len(sources.get("chunks") or []),
        }

    def summarize_artifact(self, artifact: dict) -> str:
        kb = (artifact or {}).get("kb") or {}
        sub = artifact.get("subgraph") or {}
        return f"检索知识库「{kb.get('name', '')}」命中 {len(sub.get('nodes') or [])} 实体"
