"""PromptLoader 测试 — render + section 缓存 + override/append。"""
import pytest
from engine.prompt_loader import PromptLoader, PromptOverrides


class TestRender:
    def test_render_simple_template(self, tmp_path):
        pack_dir = tmp_path / "njmind_form" / "prompts"
        pack_dir.mkdir(parents=True)
        (pack_dir / "hello.j2").write_text("Hello, {{ name }}!")

        loader = PromptLoader(packs_root=tmp_path)
        result = loader.render("njmind_form", "hello", name="表单")
        assert result == "Hello, 表单!"

    def test_render_caches_static(self, tmp_path):
        """静态 section(无变量)渲染后缓存。"""
        pack_dir = tmp_path / "p" / "prompts" / "_sections"
        pack_dir.mkdir(parents=True)
        (pack_dir / "intro.j2").write_text("你是助手。")

        loader = PromptLoader(packs_root=tmp_path)
        r1 = loader.render("p", "_sections/intro")
        # 修改文件(模拟缓存命中:不应重读)
        (pack_dir / "intro.j2").write_text("你是别的。")
        r2 = loader.render("p", "_sections/intro")
        assert r1 == r2 == "你是助手。"


class TestCacheableFalse:
    def test_cacheable_false_forces_recompute(self, tmp_path):
        """frontmatter cacheable: false → 每次重算。"""
        pack_dir = tmp_path / "p" / "prompts"
        pack_dir.mkdir(parents=True)
        (pack_dir / "time.j2").write_text(
            "---\ncacheable: false\n---\n现在: {{ ts }}"
        )

        loader = PromptLoader(packs_root=tmp_path)
        r1 = loader.render("p", "time", ts="A")
        r2 = loader.render("p", "time", ts="B")
        assert "A" in r1
        assert "B" in r2


class TestAssemble:
    def test_assemble_static_plus_dynamic(self, tmp_path):
        sections_dir = tmp_path / "p" / "prompts" / "_sections"
        sections_dir.mkdir(parents=True)
        (sections_dir / "intro.j2").write_text("身份:助手")
        (tmp_path / "p" / "prompts" / "main.j2").write_text("主任务 {{ user_input }}")

        loader = PromptLoader(packs_root=tmp_path)
        result = loader.assemble(
            "p", "main",
            sections=["intro"],
            dynamic={"user_input": "创建表单"},
        )
        assert "身份:助手" in result
        assert "主任务" in result
        assert "创建表单" in result

    def test_override_replaces_all(self, tmp_path):
        (tmp_path / "p" / "prompts").mkdir(parents=True)
        (tmp_path / "p" / "prompts" / "main.j2").write_text("原内容")

        loader = PromptLoader(packs_root=tmp_path)
        result = loader.assemble(
            "p", "main", sections=[], dynamic={},
            overrides=PromptOverrides(override="完全替换"),
        )
        assert result.strip() == "完全替换"

    def test_append_added_at_end(self, tmp_path):
        (tmp_path / "p" / "prompts").mkdir(parents=True)
        (tmp_path / "p" / "prompts" / "main.j2").write_text("原内容")

        loader = PromptLoader(packs_root=tmp_path)
        result = loader.assemble(
            "p", "main", sections=[], dynamic={},
            overrides=PromptOverrides(append="合规规则"),
        )
        assert "原内容" in result
        assert "合规规则" in result
        # append 在末尾
        assert result.index("合规规则") > result.index("原内容")
