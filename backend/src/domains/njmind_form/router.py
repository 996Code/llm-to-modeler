"""njmind_form 领域路由 —— 本 pack 内的工具二级选择（领域知识归 pack）。

【判断结构：LLM 语义主判 + 数据规则复核】
- LLM 主判：自然话术千变万化（"我还要个邮箱"/"删掉重来做个新的"），
  语义理解交给领域 prompt（build_prompt 的规则段，含增量话术与创造性
  话术的划分）；
- 数据规则复核：不做关键词独裁（错杀风险：'删掉重来做一个新的'会命中
  '删掉'被误判为修改），只在**结果与数据事实矛盾**时修正——
  画布为空时任何 modify 判定都是幻觉（没有基线可改）。

【背景事故】画布上有旧表单时用户说"生成请假申请单的表单"，LLM 曾判给
modify→full_rewrite→字段 key 全换但标识被回填保留，产出"半新半旧"配置
（已存数据列全部错位）。修复分两层：本路由的 prompt 规则（创造性话术走
create）+ modify_form._modify_full 的延续性回填（字段 key 零交集=新表单
语义，不保留旧标识——那条同样是确定性数据规则）。
"""
import re  # noqa: F401 （保留 import 供后续规则扩展；当前无正则短路）
from typing import Optional

from sdk.pack_router import DefaultPackRouter
from sdk.registry import ToolRegistry


class NjmindFormRouter(DefaultPackRouter):
    """表单领域路由：LLM 语义主判 + 数据规则复核（不抢判断，只兜矛盾）。"""

    def __init__(self, registry: ToolRegistry):
        super().__init__(registry)

    def route(self, user_input: str, artifact: Optional[dict],
              history: str = "", llm_client=None) -> Optional[str]:
        """LLM 主判 + 数据铁律前置：

        前置铁律（数据事实，无语义争议，前置可省一次 LLM 调用）：
          画布为空 → create_form（没东西可改——LLM 在此场景说 modify 是幻觉）。

        其余交给 LLM 语义判断（领域规则在 build_prompt）。
        """
        has_fields = bool(
            artifact and artifact.get("formFieldConfigVos"))

        # 前置铁律：画布为空（数据事实，无争议）
        if not has_fields:
            return "create_form"

        # LLM 语义主判（领域规则在 build_prompt 里）
        return super().route(user_input, artifact, history=history,
                             llm_client=llm_client)

    def build_prompt(self, has_artifact: bool) -> str:
        # 复用父类的候选工具清单拼装，替换规则段为表单领域规则
        base = super().build_prompt(has_artifact)
        tools_section = base[base.find("候选工具:"):]

        return (
            "你是表单领域的工具路由器。根据用户消息与画布状态选工具，只返回 JSON。\n\n"
            "规则（按优先级）：\n"
            "1. 【最重要】画布已有内容时（has_artifact=true，**包括未保存的草稿**"
            "——用户手动拖了字段再让 AI 继续加/删/改，同样是修改现有表单），"
            "对**现有表单**的加字段/删字段/改属性/改选项/改布局类指令必须选"
            " modify_form（选 create_form 会丢弃用户已有的字段）。\n"
            "2. 【同样重要】用户指名要一个**新东西**——'生成/创建/做一个 XX 表单'"
            "'设计一个 XX'（XX 与当前表单主题无关也照走）——无论画布是否有内容"
            "都选 create_form：产物是独立的新表单，应用前用户现有表单无恙，"
            "用户自己决定是否覆盖。\n"
            "3. 画布为空且无明确创造指令 → 按语义选（描述需求默认 create_form）。\n"
            "4. 查询/查看已有表单 → get_form；复制表单 → clone_form；"
            "图片识别生成 → image_form。\n"
            "5. 闲聊、询问能力、无关话题 → chat。\n"
            "6. 标注 (仅当 has_artifact=true) 的工具仅在画布有内容时可选。\n"
            "7. 只返回 JSON，不要解释。\n\n"
            f"{tools_section}"
        )
