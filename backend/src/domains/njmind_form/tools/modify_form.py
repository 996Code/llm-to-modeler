"""ModifyFormTool - 修改已有表单配置的复合工具(3 步管线)。

【模块定位】
属于 njmind_form 域。当用户说"加一个手机号字段"、"删除 xxx"、"把 xxx 改成必填"
时,引擎选中本工具。与 GetFormTool(单步查询)不同,本工具是**复合工具
(CompositeTool)**,内部有 3 个串行步骤,并支持校验失败后自动重试。

【3 步管线】
  fetch_guide → modify → validate
    1. fetch_guide: 从上游拉"配置指南"(字段类型映射等元数据)
    2. modify:      LLM 根据用户指令修改现有配置(JSON)
    3. validate:    提交上游校验,失败则带错误信息重跑 modify(最多 3 次)

【Java 类比】
  - CompositeTool ≈ Spring 里带 ``@Transactional`` 的编排型 Service,
    内部串联多个子步骤;Tool 则是单步 Service
  - 3 步管线 + 重试 ≈ Activiti / Camunda 的 BPMN 流程,
    或 Spring Batch 的 Step 序列
  - run_pipeline ≈ 模板方法模式 (Template Method):
    基类定义执行框架,子类实现各 ``_step_xxx`` 钩子

【关键概念:source_artifact vs artifact】
  - source_artifact: 用户传入的"原始配置"(修改前的版本)
  - artifact:        工具产出的"当前配置"(修改后的版本,可能经过多次重试)
  两者分开,便于审计"改了什么"以及回滚。
"""
import json
import logging
from typing import Any, Dict, Optional

# CompositeTool:复合工具基类,提供 run_pipeline 模板方法
# ClarificationRaised:需要追问时抛的异常(让 graph interrupt)
from sdk.tool import CompositeTool, ToolResult, ToolContext, ClarificationRaised
from domains.njmind_form.tools._config_loader import load_type_mappings

logger = logging.getLogger(__name__)

# 启动时一次性加载字段类型映射(模块级常量,避免每次工具调用都读文件)
# _TYPE_TO_TEMPLATE: 类型 → prompt 模板片段
# _TYPE_NAMES:       所有合法类型名(供 LLM 校验)
_TYPE_TO_TEMPLATE, _TYPE_NAMES = load_type_mappings()

# 校验失败后的最大重试次数(防止死循环)
MAX_RETRIES = 3


class ModifyFormTool(CompositeTool):
    """根据自然语言指令修改已有 njmind 表单配置。

    【职责】
      1. 从 state["source_artifact"](已有配置)出发
      2. 跑 3 步管线:fetch_guide → modify → validate
      3. 校验失败时自动带错误信息重跑 modify(最多 MAX_RETRIES 次)
      4. 返回修改后的 artifact + summary

    【Java 类比】
      ``class ModifyFormTool extends CompositeTool``,
      相当于一个带事务、带重试、带多步编排的 Service。
      ``run_pipeline`` 是基类提供的模板方法,自动按 steps 顺序调
      ``_step_fetch_guide / _step_modify / _step_validate``。

    【破坏性标记】
      is_destructive = True,因为会改变表单配置(写入操作)。
      引擎会据此要求确认 / 记审计日志。
    """

    # ── 工具元数据 ──
    name = "modify_form"
    description = "修改已有表单配置(加/删/改字段)"
    when = "用户想修改已有表单,如'加一个手机号字段'、'删除xxx'、'把xxx改成必填'"

    # ── 安全声明 ──
    # 修改操作:有破坏性、非只读、非并发安全(并发改同一表单会冲突)
    is_destructive = True
    is_read_only = False
    is_concurrency_safe = False

    # ── 插件化元数据 ──
    # modify 必须有已有配置:不能凭空改,得先有 source_artifact
    # (通常先跑 GetFormTool 拿到配置,再跑本工具修改)
    requires_existing_artifact = True  # modify 必须有已有配置

    # steps 是 CompositeTool 的核心声明:定义管线的步骤顺序
    # 基类 run_pipeline 会按此顺序调用 _step_fetch_guide / _step_modify / _step_validate
    steps = ["fetch_guide", "modify", "validate"]

    # Pipeline 步骤定义(用于前端动态渲染进度条)
    # key 对应 steps,label 是给用户看的中文
    pipeline_steps = [
        {"key": "fetch_guide", "label": "获取指南"},
        {"key": "modify", "label": "修改配置"},
        {"key": "validate", "label": "校验结果"},
    ]

    def input_schema(self) -> dict:
        """输入 schema:user_input + source_artifact 都必填。

        Returns:
            JSON Schema dict,source_artifact 是 object 类型(完整表单配置)。
        """
        return {
            "type": "object",
            "properties": {
                "user_input": {"type": "string", "description": "用户的修改指令"},
                "source_artifact": {"type": "object", "description": "已有的表单配置"},
            },
            "required": ["user_input", "source_artifact"],
        }

    def validate_input(self, state: dict) -> Optional[str]:
        """语义校验:modify 必须有 source_artifact。

        【与 input_schema 的区别】
          input_schema 校验"字段是否存在 + 类型对不对"(结构校验);
          validate_input 校验"业务上是否合理"(语义校验)。
          类比 Java:Bean Validation (@NotNull) vs 业务校验逻辑。

        Args:
            state: 工作流状态

        Returns:
            错误信息字符串(校验失败时),或 None(校验通过)。
        """
        if not state.get("source_artifact"):
            return "修改表单需要已有配置(source_artifact),但当前没有"
        return None

    def execute(self, state: dict, ctx: ToolContext) -> ToolResult:
        """执行 3 步管线。

        【流程】
          1. 初始化 retry_count / validation_errors(防止 KeyError)
          2. 调基类 run_pipeline 跑完 3 步(内部可能多次重试)
          3. 从 state["artifact"] 取最终结果,组装 ToolResult

        Args:
            state: 工作流状态(会被管线修改:写入 artifact / guide / validation_errors)
            ctx:   执行上下文

        Returns:
            ToolResult:成功带 artifact;失败(extra.validation_errors 非空)仍返回,
            但 summary 会反映"未完成"。
        """
        # setdefault:不存在才设默认值,存在不动(类比 Java 的 computeIfAbsent)
        state.setdefault("retry_count", 0)
        state.setdefault("validation_errors", [])

        # 核心:跑管线。基类会按 self.steps 顺序调各 _step_xxx 方法
        self.run_pipeline(state, ctx)

        # 管线跑完后,artifact 是最终配置(可能经过了多次重试修改)
        artifact = state.get("artifact")
        if artifact:
            form_name = artifact.get("formName", "")
            field_count = len(artifact.get("formFieldConfigVos", []))
            summary = f"已修改「{form_name}」,共 {field_count} 个字段"
        else:
            # 没产出 artifact:可能是 modify 步骤异常 / validate 全部失败
            summary = "表单修改未完成"

        return ToolResult(
            artifact=artifact,
            summary=summary,
            extra={
                # 校验错误列表(供前端展示 / LLM 解释失败原因)
                "validation_errors": state.get("validation_errors", []),
                "formatted": self.format_result(artifact) if artifact else {},
            },
        )

    def summarize_artifact(self, artifact: dict) -> str:
        """给压缩器用:把 artifact 压成一行摘要。

        与 GetFormTool.summarize_artifact 几乎一样,只是前缀改成"当前表单"
        (因为这是修改后的最新版本)。

        Args:
            artifact: 修改后的表单配置

        Returns:
            单行摘要字符串。
        """
        form_name = artifact.get("formName", "")
        form_code = artifact.get("formCode", "")
        fields = artifact.get("formFieldConfigVos", [])
        field_summary = ", ".join(
            f.get("fieldTitleText", "") for f in fields[:10]
        )
        if len(fields) > 10:
            field_summary += f" ... 共 {len(fields)} 个字段"
        return f"当前表单: {form_name} ({form_code}), 字段: {field_summary}"

    def title_for(self, artifact: dict) -> str:
        """给对话列表用:从 artifact 生成会话标题。

        Args:
            artifact: 表单配置

        Returns:
            表单名(没有则"新对话")。
        """
        return artifact.get("formName", "新对话")

    def format_result(self, artifact: dict) -> dict:
        """给 SSE 用:从 artifact 提取前端渲染字段(钩子方法)。

        与 GetFormTool.format_result 一致,把完整配置转成精简视图模型。

        Args:
            artifact: 完整表单配置

        Returns:
            含 fieldCount / formName / formCode / title 的精简 dict。
        """
        fields = artifact.get("formFieldConfigVos", [])
        return {
            "fieldCount": len(fields),
            "formName": artifact.get("formName", ""),
            "formCode": artifact.get("formCode", ""),
            "title": artifact.get("formName", "新对话"),
        }

    # ── Steps(管线的三个具体步骤) ──────────────────────────

    def _step_fetch_guide(self, state: dict, ctx: ToolContext) -> None:
        """Step 1: 获取配置指南。

        【作用】
          从上游拉"配置指南"——通常是字段类型映射、必填规则等元数据。
          后续 modify 步骤会把 guide 喂给 LLM,让它知道"有哪些合法字段类型"。

        Args:
            state: 会写入 state["guide"]
            ctx:   提供 asset_client + emit
        """
        ctx.emit("stage", "fetch_guide", "正在从上游获取配置指南...")
        state["guide"] = ctx.asset_client.get_guide()

    def _step_modify(self, state: dict, ctx: ToolContext) -> None:
        """Step 2: LLM 基于指令修改现有 FormConfig。

        【两种模式】
          - 首次修改:基于 source_artifact(用户原始配置)
          - 重试修改:基于 artifact(上一次的产出,带校验错误一起再喂给 LLM)

        【Prompt 构造】
          - system: 配置指南 + 字段类型模板(render 出来)
          - user:   对话历史(压缩版)+ 修改指令 [+ 校验错误(重试时)]

        Args:
            state: 读 user_input / source_artifact / artifact / guide /
                   compressed_history / validation_errors;写 state["artifact"]
            ctx:   提供 emit / llm_client / prompt_loader
        """
        # 判断是首次还是重试:有 validation_errors 说明上轮校验失败了
        is_retry = bool(state.get("validation_errors"))

        if is_retry:
            ctx.emit("stage", "modify_retry", f"校验失败，正在修复并重新修改（第 {state.get('retry_count', 0)} 次重试）...")
        else:
            ctx.emit("stage", "modify", "AI 正在根据指令修改现有配置...")

        # 基础配置:retry 用 artifact(上次的产出),首次用 source_artifact(原始)
        # 为什么重试用 artifact?因为 artifact 是"离正确最近的版本",
        # 带着校验错误让 LLM 修补,比从原始重改效率高
        base_config = state.get("artifact") if is_retry else state.get("source_artifact")
        if not base_config:
            # 兜底:理论上不会走到,防 NPE
            logger.error("ModifyFormTool: no base config to modify!")
            state["artifact"] = state.get("source_artifact")
            return

        # 渲染 system prompt:把指南 + 字段类型模板拼进去
        # 通过 ctx.prompt_loader 加载模板(类比 Spring 的 TemplateEngine)
        system_prompt = self._render_prompt(
            ctx, "modify",
            config=base_config,
            guide=state.get("guide") or {},
        )

        # 构建 user message:对话历史 + 修改指令 [+ 校验错误]
        user_parts = []
        # 压缩历史让 LLM 知道上下文(用户之前可能提过相关需求)
        if state.get("compressed_history"):
            user_parts.extend(["## 对话历史", state["compressed_history"], ""])

        if is_retry:
            # 重试模式:把校验错误明示给 LLM,让它针对性修复
            # 只取前 5 条错误,避免 prompt 过长
            error_msgs = [
                e.get("message", str(e))
                for e in state.get("validation_errors", [])[:5]
            ]
            user_parts.extend([
                "## 原始修改指令",
                state.get("user_input", ""),
                "",
                "## 校验失败，请修复",
                "\n".join(f"- {m}" for m in error_msgs),
                "",
                "## 当前配置",
                f"```json\n{json.dumps(base_config, ensure_ascii=False)}\n```",
                "请修复后输出完整配置。",
            ])
        else:
            # 首次模式:只给指令,让 LLM 自由修改
            user_parts.extend([
                "## 修改指令",
                state.get("user_input", ""),
                "",
                "请根据指令修改上面的配置，输出修改后的完整 JSON。",
            ])

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "\n".join(user_parts)},
        ]

        # 调 LLM 产出新配置(JSON),chat_json 自动解析成 dict
        config = ctx.llm_client.chat_json(messages, conv_id=ctx.conv_id)
        # 写回 state,供 validate 步骤使用
        state["artifact"] = config
        # 重试场景下清空上轮的错误(本轮还没校验,先清空)
        state["validation_errors"] = []

    def _step_validate(self, state: dict, ctx: ToolContext) -> None:
        """Step 3: 提交上游校验。失败时工具内部 retry(重跑 modify)。

        【校验逻辑】
          - 调 asset_client.validate_artifact 提交校验
          - 区分 errors(硬错误,必须修)和 warnings(警告,可忽略)
          - 只有 errors 非空才算失败
          - 失败 → retry_count++ → 若未超限则递归调 _step_modify + _step_validate

        【Java 类比】
          相当于带重试的校验循环:
          ``while (retries < MAX && !valid) { modify(); validate(); }``
          这里用递归实现,本质一样。

        Args:
            state: 读 artifact / retry_count;写 validation_errors / retry_count
            ctx:   提供 emit / asset_client
        """
        ctx.emit("stage", "validate", "正在提交到上游平台进行校验...")
        artifact = state.get("artifact")
        if not artifact:
            # 没配置可校验(上游 modify 失败的兜底)
            state["validation_errors"] = [{"message": "No configuration to validate"}]
            ctx.emit("stage", "validate_fail", "校验失败：无配置可校验")
            return

        # mode="update":告诉上游这是"修改"校验(区别于"新建"校验,规则可能不同)
        result = ctx.asset_client.validate_artifact(artifact, mode="update")

        # 区分 errors 和 warnings
        errors = result.get("errors", [])
        warnings = result.get("warnings", [])

        # 只有 errors 非空才算校验失败，warnings 不算
        # (warnings 是软提示,如"建议加默认值",不阻塞)
        if result.get("valid") or not errors:
            state["validation_errors"] = []
            if warnings:
                ctx.emit("stage", "validate_pass", f"校验通过 ✓（{len(warnings)} 个警告）")
            else:
                ctx.emit("stage", "validate_pass", "校验通过 ✓")
            return

        # 校验失败:递增重试计数,记录错误
        state["retry_count"] = state.get("retry_count", 0) + 1
        state["validation_errors"] = errors

        if state["retry_count"] < MAX_RETRIES:
            # 还有重试机会:带错误重跑 modify,然后再 validate
            # 只展示前 3 条错误给前端(避免刷屏)
            error_msgs = [e.get("message", str(e)) for e in state["validation_errors"][:3]]
            ctx.emit("stage", "validate_retry",
                     f"校验失败：{'；'.join(error_msgs)}，正在重试（第 {state['retry_count']} 次）...")
            # 递归:重新修改 → 重新校验(形成 modify-validate 循环)
            self._step_modify(state, ctx)
            return self._step_validate(state, ctx)
        else:
            # 重试用尽:不再重试,把最终错误暴露给上层
            error_msgs = [e.get("message", str(e)) for e in state["validation_errors"][:3]]
            ctx.emit("stage", "validate_fail",
                     f"校验失败（已达最大重试次数）：{'；'.join(error_msgs)}")

    # ── 辅助方法 ───────────────────────────────────────────────

    def _render_prompt(self, ctx: ToolContext, name: str, **vars) -> str:
        """通过 ctx.prompt_loader 渲染模板。

        【设计】
          prompt 模板不硬编码在代码里,而是放在外部文件(prompt_loader 加载),
          方便产品 / 运营调整 prompt 不用改代码。
          类比 Java:Spring 的 ``@Value`` + PropertyPlaceholder / Thymeleaf。

        Args:
            ctx:  执行上下文,提供 prompt_loader
            name: 模板名(如 "modify")
            **vars: 模板变量

        Returns:
            渲染后的 prompt 字符串;若没有 prompt_loader 则返回空串(降级)。
        """
        # hasattr 检查:防御性编程,有些 ctx 可能没注入 prompt_loader
        if hasattr(ctx, "prompt_loader") and ctx.prompt_loader:
            # render(domain, template_name, **vars):按域 + 名加载模板并填充变量
            return ctx.prompt_loader.render("njmind_form", name, **vars)
        # 降级:没 loader 就返回空 prompt(可能导致 LLM 表现差,但不会崩)
        logger.warning(f"No prompt_loader, returning empty for {name}")
        return ""
