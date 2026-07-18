"""PromptLoader — Prompt section 装配器。

对标 Claude Code systemPrompt.ts + systemPromptSections.ts。

两项增强(v4):
- C.2-C:section 级缓存(以 pack+name 为 key,frontmatter 可声明 cacheable: false)
- C.2-E:区分 override(替换默认)/ append(挂尾)

机制归 Engine,模板内容归 pack。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
from jinja2 import Environment, FileSystemLoader, StrictUndefined


@dataclass
class PromptOverrides:
    """多来源 prompt 合并(C.2-E)。当前只有 pack 一种来源,留口子。
    未来 embed 宿主可传 override(覆盖身份)/append(追加合规规则)。"""
    override: Optional[str] = None
    append: Optional[str] = None


class PromptLoader:
    """Prompt section 装配器。

    缓存粒度是 section,不是整份 prompt:
    - 静态片段(intro/field_types 等)渲染一次后缓存
    - 动态段(当前 artifact、压缩历史)每次重算
    """

    def __init__(self, packs_root: Path):
        """packs_root: 含 <pack_name>/prompts/ 的根目录。"""
        self._packs_root = Path(packs_root)
        self._cache: dict[tuple, str] = {}
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(self._packs_root)),
            undefined=StrictUndefined,
            autoescape=False,  # prompt 是 trusted,不转义
        )
        self._frontmatter_cache: dict[tuple, tuple[dict, str]] = {}

    def render(self, pack_name: str, template_name: str, **vars) -> str:
        """渲染单个 prompt 模板,带 section 级缓存。"""
        frontmatter, _ = self._read_frontmatter(pack_name, template_name)
        cacheable = frontmatter.get("cacheable", True)

        # 无变量 + cacheable → 缓存
        if cacheable and not vars:
            cache_key = (pack_name, template_name)
            if cached := self._cache.get(cache_key):
                return cached

        # 渲染(Jinja2 路径:<pack>/prompts/<name>.j2)
        template_path = f"{pack_name}/prompts/{template_name}.j2"
        template = self._jinja_env.get_template(template_path)
        rendered = template.render(**vars) if vars else template.render()

        if cacheable and not vars:
            self._cache[(pack_name, template_name)] = rendered
        return rendered

    def assemble(self, pack_name: str, template_name: str, sections: list[str],
                 dynamic: dict, overrides: Optional[PromptOverrides] = None) -> str:
        """组装完整 prompt(C.2-E 优先级,对标 CC buildEffectiveSystemPrompt):
            0. override → 完全替换
            1. 静态主干 = sections 拼接 + 主模板
            2. 动态段(dynamic)作为主模板变量
            3. append → 挂到最末
        """
        # 0. override 完全替换(不走 Jinja2,纯文本)
        if overrides and overrides.override:
            return overrides.override

        # 1. 静态 sections
        parts = []
        for section in sections:
            parts.append(self.render(pack_name, f"_sections/{section}"))

        # 2. 主模板(带动态变量)
        parts.append(self.render(pack_name, template_name, **dynamic))

        # 3. append(纯文本,不走 Jinja2)
        if overrides and overrides.append:
            parts.append(overrides.append)

        return "\n\n".join(p for p in parts if p.strip())

    def _read_frontmatter(self, pack_name: str, template_name: str) -> tuple[dict, str]:
        """读取模板 frontmatter(YAML between --- and ---)。"""
        cache_key = (pack_name, template_name)
        if cache_key in self._frontmatter_cache:
            return self._frontmatter_cache[cache_key]
        template_path = self._packs_root / pack_name / "prompts" / f"{template_name}.j2"
        if not template_path.exists():
            result = ({}, "")
            self._frontmatter_cache[cache_key] = result
            return result
        text = template_path.read_text(encoding="utf-8")
        if text.startswith("---\n"):
            parts = text.split("---\n", 2)
            if len(parts) >= 3:
                fm = yaml.safe_load(parts[1]) or {}
                content = parts[2]
                result = (fm, content)
                self._frontmatter_cache[cache_key] = result
                return result
        result = ({}, text)
        self._frontmatter_cache[cache_key] = result
        return result

    def clear_cache(self) -> None:
        self._cache.clear()
        self._frontmatter_cache.clear()
