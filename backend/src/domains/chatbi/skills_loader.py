"""Skills 业务规则加载 —— SKILL.md 格式 + 方言 reference + mtime 热更新 (SKL-001)。

移植来源: chat-bi backend/app/services/skills_loader.py (300 行)。
适配: 规则文件随 pack 分发(domains/chatbi/skills/);其余逻辑 1:1。

格式: skills/<name>/SKILL.md (YAML frontmatter + Markdown 正文)
      + skills/<name>/reference/<db_type>.md (按数据源方言自动匹配)
热更新: 每次加载比对目录 mtime, 变化即失效缓存(规则文件改完立即生效)。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# 规则目录: 随 pack 分发
_SKILLS_DIR = Path(__file__).resolve().parent / "skills"


@dataclass
class Skill:
    """单个 Skill: frontmatter(name/description/version) + Markdown 正文 + reference。"""
    name: str
    description: str = ""
    version: str = ""
    content: str = ""
    references: dict = field(default_factory=dict)  # {db_type: markdown 文本}

    @classmethod
    def from_file(cls, path: Path) -> "Skill":
        text = path.read_text(encoding="utf-8")
        name, description, version = path.parent.name, "", ""
        content = text

        # frontmatter (--- ... ---): 简单 key: value 解析(不引 PyYAML)
        fm_match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.DOTALL)
        if fm_match:
            frontmatter, body = fm_match.group(1), fm_match.group(2)
            for line in frontmatter.split("\n"):
                if ":" in line:
                    k, _, v = line.partition(":")
                    k, v = k.strip(), v.strip()
                    if k == "name":
                        name = v
                    elif k == "description":
                        description = v
                    elif k == "version":
                        version = v
            content = body.strip()

        # reference/*.md: 文件名(不含 .md) 作为 db_type key
        references: dict = {}
        ref_dir = path.parent / "reference"
        if ref_dir.is_dir():
            for ref in sorted(ref_dir.glob("*.md")):
                references[ref.stem.lower()] = ref.read_text(encoding="utf-8")

        return cls(name=name, description=description, version=version,
                   content=content, references=references)


class SkillsLoader:
    """全量加载 + mtime 热更新缓存。"""

    def __init__(self, base_dir: Path = _SKILLS_DIR):
        self._base_dir = Path(base_dir)
        self._cache: dict | None = None   # {name: Skill}
        self._cache_mtime: float = -1.0

    def _dir_mtime(self) -> float:
        latest = 0.0
        if not self._base_dir.is_dir():
            return 0.0
        for p in self._base_dir.rglob("*"):
            try:
                latest = max(latest, p.stat().st_mtime)
            except OSError:
                continue
        return latest

    def load_all(self) -> dict:
        """加载全部 Skill(mtime 变化即重载——热更新)。"""
        mtime = self._dir_mtime()
        if self._cache is not None and mtime == self._cache_mtime:
            return self._cache
        skills: dict = {}
        if self._base_dir.is_dir():
            for skill_md in sorted(self._base_dir.glob("*/SKILL.md")):
                try:
                    skill = Skill.from_file(skill_md)
                    skills[skill.name] = skill
                except Exception as e:
                    logger.warning("Skill 加载失败 %s: %s", skill_md, e)
        self._cache = skills
        self._cache_mtime = mtime
        logger.info("Skills loaded: %d 个 (%s)", len(skills),
                    ", ".join(skills.keys()) or "无")
        return skills

    def format_for_prompt(self, db_type: str | None = None) -> str:
        """拼接为 prompt 约束文本;db_type 匹配时附对应方言 reference。"""
        skills = self.load_all()
        if not skills:
            return ""
        parts = []
        for skill in skills.values():
            body = skill.content
            if db_type and db_type.lower() in skill.references:
                body = f"{body}\n\n{skill.references[db_type.lower()]}"
            header = f"### {skill.description or skill.name}"
            parts.append(f"{header}\n\n{body}")
        return "\n\n---\n\n".join(parts)


_loader: SkillsLoader | None = None


def load_skills_text(db_type: str | None = None) -> str:
    """模块级入口(ask_data 注入【业务规则】段用)。"""
    global _loader
    if _loader is None:
        _loader = SkillsLoader()
    return _loader.format_for_prompt(db_type)
