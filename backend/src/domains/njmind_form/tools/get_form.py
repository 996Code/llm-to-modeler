"""GetFormTool - 查询已有表单配置的工具。

【模块定位】
属于 njmind_form 域(南讯表单域)。当用户说"查看请假表单"或"显示 XXX 表单
的配置"时,引擎选中本工具:从用户消息里提取 formCode,调上游 API 查询,
返回完整的表单配置(artifact)。

【Java 类比】
  - Tool ≈ Spring ``@Component`` 实现 ``Tool`` 接口
  - execute 的"LLM 提取 → API 查询 → 返回"三步流程
    ≈ Service 方法里调用三个协作对象(client / asset_client / formatter)
  - artifact 概念 ≈ DDD 里的领域对象 (Domain Object / Aggregate Root),
    是工具产出的核心数据结构,会被后续节点复用

【与 QueryLeaveStatusTool 的区别】
  - 那个是"纯查询返回文本",产物是 reply 字符串
  - 这个是"查询返回制品",产物是 form_config 字典(artifact),
    会进入 state 供后续节点(如 modify_form)使用
"""
import logging
from typing import Any, Dict, Optional

from sdk.tool import Tool, ToolResult, ToolContext

logger = logging.getLogger(__name__)
from domains.njmind_form.keys import FIELDS, FIELD_KEY, FIELD_TITLE


class GetFormTool(Tool):
    """查询已有表单配置。

    【职责】
      1. 用 LLM 从用户自然语言中提取 formCode(表单唯一标识)
      2. 调上游 AssetClient 查询该 formCode 对应的完整配置
      3. 把配置作为 artifact 返回,供后续修改 / 展示

    【Java 类比】
      ``class GetFormTool implements Tool``,等价于一个查询型 Service。
      内部 ``_extract_form_code`` 是私有辅助方法,封装"LLM 提取"逻辑。
    """

    # ── 工具元数据 ──
    name = "get_form"
    description = "根据 formCode 查询已有表单配置"
    when = "用户想查看已有表单,如'查看请假表单'、'显示XXX表单的配置'、'获取表单详情'"

    # ── 安全声明 ──
    # 只读工具:不修改任何数据,并发安全

    # ── 插件化元数据 ──
    # 与 query_leave_status 不同:本工具不依赖已有 artifact,
    # 而是主动去查询并产出 artifact

    def input_schema(self) -> dict:
        """输入 schema:user_input 必填。

        Returns:
            JSON Schema dict,user_input 标记为 required。
        """
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的查询指令"},
            },
            "required": ["user_input"],
        }

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行查询：优先用当前上下文里的表单，其次才按 formCode 查上游。

        【两条路径】
          路径 A（嵌入/多轮场景）：state.source_artifact 已有当前表单配置
            （嵌入模式 = 宿主每次请求随 GET_CONTEXT 下发的画布；独立模式 = 会话内
            最近一次制品）。"简述当前表单/表单里有什么"这类问题直接据此回答——
            不该去用户消息里猜 formCode。
          路径 B（无上下文）：LLM 从用户消息提取 formCode → 调上游查询（原逻辑）。

        Args:
            state: 工作流状态，取 user_input / source_artifact
            ctx:   执行上下文，提供 emit / llm_client / asset_client

        Returns:
            ToolResult:成功时带 artifact + summary；失败时带 error_for_llm。
        """
        user_input = state.get("user_input", "")

        # ── 路径 A：当前上下文直接回答 ──
        current = state.get("source_artifact")
        if current and isinstance(current, dict) and current.get(FIELDS):
            fields = current.get(FIELDS, [])
            form_name = current.get("formName") or current.get("formCode") or "当前表单"
            # 摘要里带上字段名列表（前 15 个），"简述一下"这类问题用户要的就是这个
            names = [f.get(FIELD_TITLE) or f.get(FIELD_KEY, "") for f in fields[:15]]
            names_desc = "、".join(n for n in names if n)
            if len(fields) > 15:
                names_desc += f" 等 {len(fields)} 个字段"
            ctx.emit("stage", "fetch_form", "正在读取当前页面的表单配置...")
            ctx.emit("stage", "fetch_done", f"当前表单共 {len(fields)} 个字段 ✓")
            return ToolResult(
                artifact=current,
                summary=f"当前表单「{form_name}」（{current.get('formCode', '')}），"
                        f"共 {len(fields)} 个字段：{names_desc}",
                extra={
                    "formatted": self.format_result(current),
                },
            )

        # ── 路径 B：无当前上下文 → LLM 提取 formCode 查上游（原逻辑）──
        # emit("stage", ...) 推一个 SSE 进度事件给前端(类比进度条更新)
        ctx.emit("stage", "extract_form_code", "AI 正在识别表单标识...")
        form_code = self._extract_form_code(user_input, ctx)

        if not form_code:
            # 提取失败:返回 error_for_llm,error_for_llm 是给 LLM 看的,
            # LLM 会据此生成引导用户补充的话术
            return ToolResult(
                error_for_llm="无法从用户消息中提取表单标识(formCode)",
                summary="查询失败:未提供表单标识",
            )

        # Step 2: 调用 API 查询
        ctx.emit("stage", "fetch_form", f"正在查询表单 {form_code}...")
        form_config = ctx.asset_client.get_form(form_code)

        if not form_config:
            # 表单不存在:同样让 LLM 引导用户
            return ToolResult(
                error_for_llm=f"表单 {form_code} 不存在或查询失败",
                summary=f"查询失败:表单 {form_code} 不存在",
            )

        # Step 3: 返回结果
        # 从配置里取展示用的字段:表单名 + 字段数
        form_name = form_config.get("formName", form_code)
        field_count = len(form_config.get(FIELDS, []))

        ctx.emit("stage", "fetch_done", f"查询成功 ✓ 共 {field_count} 个字段")

        # artifact 是核心产物(完整的表单配置 dict),会进入 state 供后续节点使用
        # extra.formatted 是给前端 SSE 渲染用的精简字段
        return ToolResult(
            artifact=form_config,
            summary=f"已查询到表单「{form_name}」,共 {field_count} 个字段",
            extra={
                "formatted": self.format_result(form_config),
            },
        )

    def _extract_form_code(self, user_input: str, ctx: ToolContext) -> Optional[str]:
        """用 LLM 从用户消息中提取 formCode。

        【为什么要 LLM?】
          用户说的可能是中文表单名("请假表单"),也可能是英文标识
          ("leave_apply")。用规则解析很难覆盖,交给 LLM 提取最灵活。
          类比 Java:这相当于调用一个 NLP 服务做实体识别 (NER)。

        【Prompt 设计】
          - system_prompt 明确角色("表单标识提取器")+ 输出格式(纯 JSON)
          - 给几个示例帮助 LLM 理解 formCode 的形态
          - 强调"只输出 JSON,不要解释",避免 LLM 啰嗦

        Args:
            user_input: 用户原始消息
            ctx:        执行上下文,提供 llm_client

        Returns:
            提取到的 formCode 字符串;失败返回 None。
        """
        system_prompt = """你是表单标识提取器。从用户消息中提取 formCode(表单唯一标识)。

formCode 通常是英文或拼音组成的标识符,如:
- "qingjia_sqb" (请假申请表)
- "leave_apply" (请假申请)
- "employee_info" (员工信息表)
- "customer_form" (客户表单)

如果用户消息中包含明确的 formCode,直接提取。
如果用户只说了表单名称(如"请假表单"),尝试推断可能的 formCode。
如果无法提取,返回空字符串。

输出格式: {"formCode": "提取的标识"}
只输出 JSON,不要解释。"""

        # 构造 OpenAI 风格的 messages 列表
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ]

        try:
            # chat_json:让 LLM 返回 JSON 并自动解析成 dict
            # conv_id 用于上游服务的日志关联 / 限流
            result = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
            # strip 去掉首尾空白,避免空格干扰
            return result.get("formCode", "").strip()
        except Exception as e:
            # LLM 调用失败(超时 / 格式错误 / 网络),降级返回 None
            logger.warning(f"formCode extraction failed: {e}")
            return None

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:把 artifact 压缩成一行摘要文本。

        【用途】
          长对话历史会超出 LLM 上下文窗口,需要压缩。这里把完整的表单配置
          (可能几百个字段)压成一行,保留关键信息(表单名 + 前 10 个字段名)。

        【Java 类比】
          相当于 ``toString()`` 的精简版,专门给 token 压缩场景用。

        Args:
            artifact: 完整的表单配置

        Returns:
            压缩后的单行摘要字符串。
        """
        form_name = artifact.get("formName", "")
        form_code = artifact.get("formCode", "")
        fields = artifact.get(FIELDS, [])
        # 只取前 10 个字段名,超出的用"共 N 个字段"概括
        field_summary = ", ".join(
            f.get(FIELD_TITLE, "") for f in fields[:10]
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"查询的表单: {form_name} ({form_code}), 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从 artifact 生成会话标题。

        Args:
            artifact: 表单配置

        Returns:
            形如"查询: 请假申请表"的标题字符串。
        """
        return f"查询: {artifact.get('formName', '表单')}"

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从 artifact 提取前端渲染需要的精简字段(钩子方法)。

        【设计意图】
          前端不需要完整的表单配置(几百字段太重),只要几个展示字段。
          这个方法就是"制品 → 前端视图模型"的转换钩子。
          类比 Java:DTO 转换 (Entity → VO)。

        Args:
            artifact: 完整表单配置

        Returns:
            只含 fieldCount / formName / formCode / title 的精简 dict。
        """
        fields = artifact.get(FIELDS, [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "表单查询"),
        }
