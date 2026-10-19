#!/usr/bin/env python3
"""render-notes.py — 把 笔记/*.md 渲染成自包含 HTML（iframe 嵌入对应链路图）。
MD 是唯一真源；HTML 是渲染产物（改笔记改 MD 后重跑本脚本）。
用法: python3 doc/RAGFlow项目学习/render-notes.py （在仓库根执行）
"""
import os
import re
import glob
import html as html_mod
import markdown

BASE = os.path.join(os.path.dirname(__file__))
NOTES = os.path.join(BASE, "笔记")

# 角度 → 配图（无图的角度留空）
DIAGRAM = {
    "01-架构与任务体系": "ragflow-system.html",
    "02-检索链路与引擎": "ragflow-chat-retrieval.html",
    "03-GraphRAG与知识编译": "ragflow-graphrag.html",
    "04-文档理解DeepDoc": "ragflow-doc-ingestion.html",
    "05-Agent画布与Agentic检索": "ragflow-agent-canvas.html",
    "06-数据模型与多租户": None,
    "07-连接器与数据源同步": "ragflow-connector-sync.html",
    "08-记忆与上下文": "ragflow-memory.html",
    "09-评测可观测与性能": None,
    "10-安全与沙箱": None,
    "11-Prompt工程": None,
    "12-版本演进与避坑": None,
    "13-业务链路全景": None,
    "14-官方文档地图与盲区补齐": None,
    "15-产品商业战略视角": None,
    "16-NL2SQL竞品对标": None,
    "ragflow-benchmark-and-upgrade-plan": None,
}

TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} · RAGFlow 项目学习</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif; background: #f6f7f9; color: #1f2328; line-height: 1.7; }}
  .wrap {{ max-width: 980px; margin: 0 auto; padding: 32px 24px 64px; }}
  nav.top {{ font-size: 13px; margin-bottom: 20px; }}
  nav.top a {{ color: #4c6ef5; text-decoration: none; }}
  h1 {{ font-size: 24px; margin-bottom: 6px; }}
  .lead {{ color: #57606a; font-size: 14px; margin-bottom: 24px; }}
  .diagram {{ background: #fff; border: 1px solid #d0d7de; border-radius: 10px; margin: 24px 0; overflow: hidden; }}
  .diagram .bar {{ display: flex; justify-content: space-between; align-items: center; padding: 10px 16px; border-bottom: 1px solid #eaeef2; font-size: 13px; color: #57606a; }}
  .diagram .bar a {{ color: #4c6ef5; text-decoration: none; }}
  .diagram iframe {{ width: 100%; height: 640px; border: 0; display: block; }}
  article {{ background: #fff; border: 1px solid #d0d7de; border-radius: 10px; padding: 28px 32px; }}
  article h2 {{ font-size: 19px; margin: 28px 0 10px; padding-left: 10px; border-left: 3px solid #4c6ef5; }}
  article h2:first-child {{ margin-top: 0; }}
  article h3 {{ font-size: 16px; margin: 20px 0 8px; }}
  article p {{ margin: 8px 0; }}
  article ul, article ol {{ margin: 8px 0 8px 22px; }}
  article li {{ margin: 4px 0; }}
  article code {{ background: #eff1f3; border-radius: 4px; padding: 1px 6px; font-size: 0.92em; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
  article pre code {{ display: block; padding: 12px; overflow-x: auto; }}
  article blockquote {{ border-left: 3px solid #d0d7de; padding: 2px 14px; margin: 10px 0; color: #57606a; background: #fafbfc; border-radius: 0 6px 6px 0; }}
  article table {{ border-collapse: collapse; width: 100%; margin: 12px 0; font-size: 14px; }}
  article th, article td {{ border: 1px solid #d0d7de; padding: 7px 12px; text-align: left; }}
  article th {{ background: #f6f8fa; }}
  article a {{ color: #4c6ef5; }}
  article strong {{ color: #111; }}
  nav.bottom {{ display: flex; justify-content: space-between; margin-top: 24px; font-size: 14px; }}
  nav.bottom a {{ color: #4c6ef5; text-decoration: none; background: #fff; border: 1px solid #d0d7de; border-radius: 8px; padding: 8px 16px; }}
  nav.bottom a:hover {{ border-color: #4c6ef5; }}
</style>
</head>
<body>
<div class="wrap">
  <nav class="top"><a href="../index.html">← RAGFlow 项目学习 · 总览</a></nav>
  <h1>{title}</h1>
  <p class="lead">{lead}</p>
  <p class="lead" style="font-size:12px;color:#8b949e;">基于 RAGFlow v0.27.2（快照 ~/cyble-code/ragflow-v0272，行号可现场验证）· 2026-10-13 定稿</p>
  {diagram_block}
  <article>
{body}
  </article>
  <nav class="bottom">
    <a href="{prev}">← {prev_title}</a>
    <a href="{next}">{next_title} →</a>
  </nav>
</div>
</body>
</html>
"""


def render():
    files = sorted(
        f for f in glob.glob(os.path.join(NOTES, "*.md"))
        if os.path.basename(f)[:-3] in DIAGRAM
    )
    # 蓝图放最后
    files.sort(key=lambda f: os.path.basename(f).startswith("ragflow-"))
    count = 0
    for i, path in enumerate(files):
        stem = os.path.basename(path)[:-3]
        md_text = open(path).read()
        # 提取标题与导语
        title = stem
        lead = ""
        lines = md_text.split("\n")
        for j, l in enumerate(lines):
            if l.startswith("# "):
                title = l[2:].strip()
                # 后续 blockquote 作导语
                k = j + 1
                while k < len(lines) and not lines[k].strip():
                    k += 1
                if k < len(lines) and lines[k].startswith(">"):
                    lead = lines[k].lstrip("> ").strip()
                break
        body = markdown.markdown(md_text, extensions=["tables", "fenced_code"])
        diagram = DIAGRAM.get(stem)
        if diagram:
            diagram_block = (
                f'<div class="diagram"><div class="bar"><span>链路图：{html_mod.escape(diagram)}</span>'
                f'<a href="../图/{diagram}" target="_blank">新窗口打开 ↗</a></div>'
                f'<iframe src="../图/{diagram}" title="{html_mod.escape(diagram)}" loading="lazy"></iframe></div>'
            )
        else:
            diagram_block = ""
        prev_f = files[i - 1] if i > 0 else files[-1]
        next_f = files[i + 1] if i < len(files) - 1 else files[0]

        def nav_title(p):
            for l in open(p).read().split("\n"):
                if l.startswith("# "):
                    return l[2:].strip()
            return os.path.basename(p)[:-3]

        out = TEMPLATE.format(
            title=html_mod.escape(title),
            lead=html_mod.escape(lead),
            diagram_block=diagram_block,
            body=body,
            prev=os.path.basename(prev_f)[:-3] + ".html",
            next=os.path.basename(next_f)[:-3] + ".html",
            prev_title=html_mod.escape(nav_title(prev_f)),
            next_title=html_mod.escape(nav_title(next_f)),
        )
        open(os.path.join(NOTES, stem + ".html"), "w").write(out)
        count += 1
    print(f"rendered {count} html -> {NOTES}/")


if __name__ == "__main__":
    render()
