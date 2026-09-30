"""njmind_list 领域路由 —— pack 内工具二级选择。

产品语义（对齐 BPM 裁决）：生成/修改/重做统一走 generate_list 单管线
（整体重造回显覆盖），无 create/update 分流，也就无路由歧义。
"""
from typing import Optional

from sdk.pack_router import DefaultPackRouter


class NjmindListRouter(DefaultPackRouter):
    """列表配置领域路由：generate_list 唯一业务工具 + list_chat 兜底。"""

    def build_prompt(self, has_artifact: bool) -> str:
        return (
            "你是列表页配置领域的工具路由器。根据用户消息选工具，只返回 JSON。\n\n"
            "规则：\n"
            "1. 任何列表生成/修改/重做需求('做个XX列表'/'加一列'/'加导出按钮'/"
            "'只看今年数据'/'重新生成...')都选 generate_list。\n"
            "2. 闲聊、询问能力、无关话题 → list_chat。\n"
            "3. 只返回 JSON，格式 {\"tool\": \"工具名\"}，不要解释，不要围栏。\n\n"
            f"{self.build_tools_section(has_artifact)}"
        )
