"""njmind_bpm 领域路由 —— pack 内工具二级选择。

产品语义(用户裁决):只有"更新生成新配置回显到页面"一条路——generate_process
承担全部生成/修改/重做;画布已有流程时按新描述整体重造并回显覆盖,无 create/update
分流,也就无路由歧义。
"""
from typing import Optional

from sdk.pack_router import DefaultPackRouter


class NjmindBpmRouter(DefaultPackRouter):
    """流程领域路由:generate_process 唯一业务工具 + chat 兜底。"""

    def build_prompt(self, has_artifact: bool) -> str:
        return (
            "你是流程配置领域的工具路由器。根据用户消息选工具，只返回 JSON。\n\n"
            "规则：\n"
            "1. 任何流程生成/修改/重做需求('做个XX审批流'/'改成...'/'重新生成...')"
            "都选 generate_process。\n"
            "2. 闲聊、询问能力、无关话题 → bpm_chat。\n"
            "3. 只返回 JSON，格式 {\"tool\": \"工具名\"}，不要解释，不要围栏。\n\n"
            f"{self.build_tools_section(has_artifact)}"
        )
