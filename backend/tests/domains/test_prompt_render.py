"""njmind_form prompts 模板渲染测试。"""
import pytest
from pathlib import Path
from engine.prompt_loader import PromptLoader


@pytest.fixture
def loader():
    """创建 PromptLoader，指向 njmind_form pack。"""
    packs_root = Path(__file__).parent.parent.parent / "src" / "domains"
    return PromptLoader(packs_root)


class TestIntentPrompt:
    """工具选择 prompt 测试。"""

    def test_intent_prompt_renders(self, loader):
        """intent.j2 能渲染，包含 3 种意图说明。"""
        result = loader.render("njmind_form", "intent", has_existing_config=False)
        assert "意图识别器" in result
        assert "create" in result
        assert "modify" in result
        assert "general" in result

    def test_intent_prompt_includes_config_flag(self, loader):
        """intent.j2 包含 has_existing_config 变量。"""
        result = loader.render("njmind_form", "intent", has_existing_config=True)
        assert "has_existing_config=True" in result


class TestParsePrompt:
    """字段解析 prompt 测试。"""

    def test_parse_prompt_renders(self, loader):
        """parse.j2 能渲染，包含字段类型表。"""
        result = loader.render("njmind_form", "parse", guide={})
        assert "表单需求分析器" in result
        assert "字段类型对照表" in result
        assert "TEXT" in result
        assert "NUMBER" in result

    def test_parse_prompt_includes_keyword_hints(self, loader):
        """parse.j2 包含关键词映射（当 guide 有 keywordIndex）。"""
        guide = {
            "keywordIndex": {
                "手机": ["TEXT"],
                "金额": ["NUMBER"],
            }
        }
        result = loader.render("njmind_form", "parse", guide=guide)
        assert "关键词映射参考" in result
        assert "手机" in result


class TestGeneratePrompt:
    """配置组装 prompt 测试。"""

    def test_generate_prompt_renders(self, loader):
        """generate.j2 能渲染，包含模板 JSON。"""
        form_template = {"formName": "测试表单", "fields": []}
        field_templates = {"TEXT": {"fieldType": 0}}
        result = loader.render(
            "njmind_form", "generate",
            form_template=form_template,
            field_templates=field_templates,
        )
        assert "表单配置组装器" in result
        assert "测试表单" in result
        assert "字段模板" in result


class TestModifyPrompt:
    """配置修改 prompt 测试。"""

    def test_modify_prompt_renders(self, loader):
        """modify.j2 能渲染，包含当前配置。"""
        config = {"formName": "现有表单", "formFieldConfigVos": []}
        result = loader.render("njmind_form", "modify", config=config, guide={})
        assert "表单配置修改器" in result
        assert "现有表单" in result
        assert "修改规则" in result


class TestChatPrompt:
    """闲聊 prompt 测试。"""

    def test_chat_prompt_renders(self, loader):
        """chat.j2 能渲染，包含角色定位。"""
        result = loader.render("njmind_form", "chat")
        assert "表单配置助手" in result
        assert "友好自然" in result
