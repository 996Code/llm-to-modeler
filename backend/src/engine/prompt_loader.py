"""PromptLoader — Prompt section 装配器。

模块定位
========

负责把散落在磁盘上的 Jinja2 模板片段按规则拼装成发给 LLM 的完整 system prompt。
对标 Claude Code 的 ``systemPrompt.ts`` + ``systemPromptSections.ts``。

设计哲学:**机制归 Engine,模板内容归 pack**。
本模块只管“怎么拼”(加载、缓存、合并优先级),不关心“拼了什么”(具体文案)。
模板文件放在外部 ``pack`` 目录里(类比 Java 里 ``/resources`` 模板 + 代码分离),
换 pack(如换行业/租户)不用改代码。

目录结构(典型)
================

::

    <packs_root>/
      <pack_name>/prompts/
        system_prompt.j2            # 主模板
        _sections/
          intro.j2                  # 静态片段(角色介绍等)
          field_types.j2            # 静态片段(字段类型说明)
          ...
      <pack_name>/prompts/xxx.j2    # 工具用 prompt

文件头部可选 YAML frontmatter(三个 ``---`` 之间),目前唯一支持的键是
``cacheable``(默认 ``true``),用来声明“本片段渲染结果能否跨请求缓存”。

两项增强(v4)
============

- **C.2-C — section 级缓存**:缓存粒度是单个 section(片段),不是整份 prompt。
  静态片段(intro/field_types)无变量、渲染一次后永久复用;动态段(当前 artifact、
  压缩历史)每请求重算。Java 类比:类似 Spring 的 ``@Cacheable`` 按 key 缓存方法结果,
  但只在“输入不变”时命中。
- **C.2-E — override/append 区分**:宿主(embed 模式)可注入 ``override``(整体替换
  默认 prompt,如换品牌身份)或 ``append``(挂到末尾,如追加企业合规规则),优先级见
  ``assemble`` 文档。

Java 类比
========

- ``PromptLoader`` ≈ Spring 的 ``TemplateEngine``(Thymeleaf/FreeMarker)装配器。
- Jinja2 ``Environment`` ≈ ``Configuration`` 对象;``get_template`` ≈ ``getTemplate``。
- frontmatter ≈ Jekyll/Hugo 的页面元数据,或 Maven POM 里声明性的配置块。
"""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml
import json
# Environment:Jinja2 模板引擎入口;FileSystemLoader:从文件系统加载模板
# StrictUndefined:模板里用了未传的变量直接抛错(而非静默成空),Fail-Fast 利于发现漏传参
from jinja2 import Environment, FileSystemLoader, StrictUndefined


@dataclass
class PromptOverrides:
    """宿主对 prompt 的覆盖/追加指令(C.2-E)。

    设计模式:策略对象 / 参数对象(Java 的 ``@Value`` 风格 immutable DTO)。
    当前只有 pack 一种来源(两个字段都是 None),保留口子供未来 embed 宿主使用:
    - override:整体替换默认 prompt(如换品牌身份、换语言),走纯文本不走 Jinja2
    - append:   追加到 prompt 末尾(如企业合规规则),走纯文本不走 Jinja2

    两者可同时给(append 不会与 override 冲突,因为 override 命中后直接 return;
    若要 append 生效就不能同时设 override,见 ``assemble`` 优先级)。
    """
    override: Optional[str] = None
    append: Optional[str] = None


class PromptLoader:
    """Prompt section 装配器。

    职责
    ====

    1. ``render``:渲染单个 Jinja2 模板片段,带 section 级缓存。
    2. ``assemble``:按优先级把若干 section + 主模板 + override/append 拼成完整 prompt。
    3. ``_read_frontmatter``:解析模板头部的 YAML frontmatter(决定可缓存性)。

    设计模式:门面(Facade)+ 缓存代理(Caching Proxy)。对调用方屏蔽 Jinja2 +
    缓存 + frontmatter 解析细节。Java 类比:像 Spring 的 ``@Cacheable`` + ``TemplateEngine``
    组合的服务类。

    缓存策略(关键)
    ================

    缓存粒度是 **section,不是整份 prompt**:
    - 静态片段(intro/field_types 等)**无变量 + cacheable=True** → 渲染一次后缓存,
      后续命中直接返回,省掉 Jinja2 解析 + 磁盘 IO。
    - 动态段(当前 artifact、压缩历史)带了变量 → 不缓存,每次重算。

    cache key = ``(pack_name, template_name)``,即“哪个 pack 的哪个模板”。
    所以**同一个模板在不同变量下只缓存“无变量版本”**,避免缓存污染。

    frontmatter 里可显式写 ``cacheable: false`` 来禁用某模板缓存(默认 true),
    用于“模板虽无变量但内容可能随时被外部改”的场景(如热更新)。
    """

    def __init__(self, packs_root: Path):
        """初始化装配器。

        Args:
            packs_root: pack 根目录,其下结构为 ``<pack_name>/prompts/*.j2``。
        """
        self._packs_root = Path(packs_root)
        # section 级渲染结果缓存:cache_key=(pack, name) -> 渲染后的字符串
        # 只缓存“无变量 + cacheable=True”的片段
        self._cache: dict[tuple, str] = {}
        # Jinja2 环境:类比 Spring 的 FreeMarker Configuration,单例复用
        self._jinja_env = Environment(
            loader=FileSystemLoader(str(self._packs_root)),
            undefined=StrictUndefined,  # 漏传变量直接报错,Fali-Fast
            autoescape=False,  # prompt 是受信任内容,不做 HTML 转义(否则 < > & 会被破坏)
        )
        # 注册自定义 tojson 过滤器:模板里写 {{ obj | tojson }} 可把 dict 序列化成 JSON。
        # 关键点:ensure_ascii=False 保留中文可读(默认 True 会把中文转成 \uXXXX 转义,LLM 读着是噪声)
        # Java 类比:相当于给 ObjectMapper 配置 ``JsonGenerator.Feature`` 或自定义 JsonSerializer
        self._jinja_env.filters['tojson'] = lambda obj, indent=None: json.dumps(
            obj, indent=indent, ensure_ascii=False
        )
        # frontmatter 解析缓存:cache_key=(pack, name) -> (frontmatter dict, 模板正文)
        # frontmatter 一旦读盘就不变,缓存避免每次 render 都 split 文本
        self._frontmatter_cache: dict[tuple, tuple[dict, str]] = {}

    def render(self, pack_name: str, template_name: str, **vars) -> str:
        """渲染单个 prompt 模板,带 section 级缓存。

        Args:
            pack_name:    pack 名(定位 ``<packs_root>/<pack_name>/prompts/``)
            template_name:模板名(不含 .j2 后缀;section 用 ``_sections/<name>`` 形式)
            **vars:       传给 Jinja2 的变量。**有 vars 一定不缓存**(动态段)。

        Returns:
            渲染后的字符串。

        Failure:
            - 模板文件不存在 → Jinja2 抛 ``TemplateNotFound``。
            - 模板里引用了未传的变量 → ``StrictUndefined`` 抛 ``UndefinedError``。
        """
        # 先读 frontmatter 判断是否可缓存(读盘走 frontmatter_cache,不重复 IO)
        frontmatter, _ = self._read_frontmatter(pack_name, template_name)
        # cacheable 默认 True,模板可在 frontmatter 里写 ``cacheable: false`` 关闭
        cacheable = frontmatter.get("cacheable", True)

        # 缓存命中条件:**无变量 + cacheable=True**。
        # 有变量时不缓存(动态段每次值不同),避免缓存污染。
        if cacheable and not vars:
            cache_key = (pack_name, template_name)
            # walrus := :命中就返回,Python 3.8+ 语法,Java 无对应,等价于先 get 再判 None
            if cached := self._cache.get(cache_key):
                return cached

        # 渲染(Jinja2 路径:<pack>/prompts/<name>.j2)
        template_path = f"{pack_name}/prompts/{template_name}.j2"
        template = self._jinja_env.get_template(template_path)
        # 有变量传 vars,无变量直接 render(避免传空 dict 触发 StrictUndefined)
        rendered = template.render(**vars) if vars else template.render()

        # 写缓存:同样要求“无变量 + cacheable=True”
        if cacheable and not vars:
            self._cache[(pack_name, template_name)] = rendered
        return rendered

    def assemble(self, pack_name: str, template_name: str, sections: list[str],
                 dynamic: dict, overrides: Optional[PromptOverrides] = None) -> str:
        """组装完整 prompt。

        对标 Claude Code ``buildEffectiveSystemPrompt`` 的合并优先级。最终 prompt 由
        至多四部分按固定顺序拼成:

        合并优先级(数字越小优先级越高)
        ====================================

        ::

            0. override        — 若提供,则**完全替换**其余所有部分,直接 return(短路)
            1. 静态 sections   — 依次拼接 ``_sections/<name>`` 渲染结果(无变量,走缓存)
            2. 主模板          — ``template_name`` 渲染,变量来自 ``dynamic``
            3. append          — 若提供,挂到最末(纯文本,不走 Jinja2)

        WHY override 短路:override 表示“宿主完全接管 prompt”(如品牌定制),任何
        默认内容都不该出现,所以直接 return,不再渲染 sections/主模板。
        WHY append 走最后:append 多为合规规则、安全约束,放末尾让 LLM 最近上下文,
        遵循“重要指令放系统提示末尾”的 prompt 工程惯例。

        Args:
            pack_name:     pack 名
            template_name: 主模板名
            sections:      静态 section 名列表(如 ``["intro", "field_types"]``)
            dynamic:       主模板的动态变量(如 ``{"artifact": ..., "history": ...}``)
            overrides:     可选的 override/append 指令

        Returns:
            拼好的完整 prompt 字符串,各段之间用 ``\\n\\n`` 分隔(空段会被过滤掉)。
        """
        # 0. override 完全替换(不走 Jinja2,纯文本)。短路返回,不渲染其余部分。
        if overrides and overrides.override:
            return overrides.override

        # 1. 静态 sections:依次渲染 _sections/<name>(无变量,命中缓存或首次渲染后入缓存)
        parts = []
        for section in sections:
            parts.append(self.render(pack_name, f"_sections/{section}"))

        # 2. 主模板:渲染时传入 dynamic 动态变量(有变量不缓存,每次重算)
        parts.append(self.render(pack_name, template_name, **dynamic))

        # 3. append:纯文本直接挂尾,不走 Jinja2(避免宿主内容被模板语法误解析)
        if overrides and overrides.append:
            parts.append(overrides.append)

        # 过滤空串(某段渲染成空就被跳过),段间用空行分隔提升 LLM 可读性
        return "\n\n".join(p for p in parts if p.strip())

    def _read_frontmatter(self, pack_name: str, template_name: str) -> tuple[dict, str]:
        """读取模板的 YAML frontmatter + 正文。

        frontmatter 格式(Hugo/Jekyll 风格):文件以 ``---\\n`` 开头,到下一个 ``---\\n``
        之间是 YAML 元数据,之后是模板正文。例::

            ---
            cacheable: false
            ---
            你是一个 {{ role }}...

        Args:
            pack_name:     pack 名
            template_name: 模板名(不含 .j2)

        Returns:
            ``(frontmatter_dict, content)``:
            - 文件不存在 → ``({}, "")``
            - 无 frontmatter(不以 ``---\\n`` 开头)→ ``({}, 整个文本)``
            - 有 frontmatter 但 YAML 解析为空 → ``({}, 正文)``

        缓存:结果按 ``(pack_name, template_name)`` 缓存,frontmatter 不变所以终生有效。
        """
        cache_key = (pack_name, template_name)
        if cache_key in self._frontmatter_cache:
            return self._frontmatter_cache[cache_key]
        template_path = self._packs_root / pack_name / "prompts" / f"{template_name}.j2"
        # 文件不存在 → 返回空结构(Fail-Closed:当作无 frontmatter,cacheable 默认 True)
        if not template_path.exists():
            result = ({}, "")
            self._frontmatter_cache[cache_key] = result
            return result
        text = template_path.read_text(encoding="utf-8")
        # 严格判断以 "---\n" 开头,避免误把正文里的分隔线当成 frontmatter
        if text.startswith("---\n"):
            # split("---\n", 2):最多切 3 段 —— ["", frontmatter_yaml, content]
            # parts[0] 是开头空串(parts[0]=="" 因为以分隔符开头)
            # parts[1] 是 YAML,parts[2] 是模板正文
            parts = text.split("---\n", 2)
            if len(parts) >= 3:
                # yaml.safe_load 解析 frontmatter;若 YAML 为空(None)兜底成 {}
                fm = yaml.safe_load(parts[1]) or {}
                content = parts[2]
                result = (fm, content)
                self._frontmatter_cache[cache_key] = result
                return result
        # 无 frontmatter:整个文本当正文,frontmatter 为空 dict
        result = ({}, text)
        self._frontmatter_cache[cache_key] = result
        return result

    def clear_cache(self) -> None:
        """清空所有缓存(渲染缓存 + frontmatter 缓存)。

        用途:热更新模板后手动调用,或单测间隔离。Java 类比:``CacheManager.clear()``。
        """
        self._cache.clear()
        self._frontmatter_cache.clear()
