"""文档解析与结构感知切块 —— SDK 通用文档设施。

【模块定位】
从 knowledge_graph 插件下沉的纯文档能力:文件字节 → 纯文本 → 结构感知
切块。零领域知识——不认识知识库/图谱/向量,输出只有 seq/text/char_count
纯文本结构,任何需要"解析文档再处理"的插件(知识图谱/RAG/报表语料
入库…)都可复用。

【支持格式】md/txt(UTF-8/GBK/BOM/宽字符容错解码)、pdf(pypdf)、
docx(python-docx,标题层级还原为 # 前缀)。驱动库延迟 import——不装
pypdf/python-docx 的环境仍可用 md/txt 能力。

【安全边界】
- 扩展名白名单(ALLOWED_EXTENSIONS)+ 解析失败抛 ValueError(调用方
  上传预检当场拒绝);
- _decode_text 对 UTF-16/乱码做探测拒收,不让垃圾文本流入下游。
"""
import io
import logging
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# 扩展名白名单(上传校验与解析分发共用)
ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt", ".pdf", ".docx"}

MIME_BY_EXT = {
    ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}

_HEADING_RE = re.compile(r"^(#{1,6})\s+")

# 纯标题缓冲(目录区)被正文到达触发结算的最小长度:低于此值的标题串
# (如单独一行"目录")不值得独立成块,并入后续正文
_HEADING_FLUSH_MIN = 40

# 通用章节标题(纯文本书籍的软边界):"第一章 xxx"/"第 12 节"/"序章"/"尾声"等。
# 约束:标题行须短(<40 字)且行首允许全角空格缩进——正文里"第二天"这类
# 普通叙述不会被误判(不带"章/节/卷/部"量词单位)。
_CHAPTER_RE = re.compile(
    r"^[\s\u3000]*(第[一二三四五六七八九十百千零〇\d]{1,7}[章節节卷部回])"
    r"([\s\u3000]+\S[^。!?!?\n]{0,29})?$"
)
_SPECIAL_CHAPTER_RE = re.compile(
    r"^[\s\u3000]*(序章|序言|楔子|前言|引子|尾声|後記|后记|番外|主目录|目录|正文|附录)"
    r"([\s\u3000:.:]\S.{0,30})?$")


def _is_heading_line(stripped: str) -> bool:
    """是否为结构标题(markdown # 或通用章节标题)——切块软边界判定。"""
    if _HEADING_RE.match(stripped):
        return True
    if len(stripped) > 40:
        return False
    return bool(_CHAPTER_RE.match(stripped) or _SPECIAL_CHAPTER_RE.match(stripped))


def allowed_extension(filename: str) -> bool:
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS)


def mime_for(filename: str) -> str:
    name = (filename or "").lower()
    for ext, mime in MIME_BY_EXT.items():
        if name.endswith(ext):
            return mime
    return "application/octet-stream"


# ── 解析:文件字节 → 纯文本 ─────────────────────────────────

def parse_to_text(filename: str, data: bytes) -> str:
    """按扩展名解析文件为纯文本(无法解析抛 ValueError,由任务层记失败)。"""
    name = (filename or "").lower()
    if name.endswith((".md", ".markdown", ".txt")):
        return _decode_text(data)
    if name.endswith(".pdf"):
        return _parse_pdf(data)
    if name.endswith(".docx"):
        return _parse_docx(data)
    raise ValueError(f"不支持的文件类型: {filename}(支持 md/txt/pdf/docx)")


def _decode_text(data: bytes) -> str:
    """UTF-8 优先,失败回退 GBK(中文办公环境常见),再失败用 errors=replace。

    BOM/零字节探测:UTF-16/UTF-32 字节流常能"成功"通过 GBK 解码成含 NUL
    的乱码(不抛异常),这类垃圾进切块再进 LLM 就是一张废图谱——所以
    GBK 解出来后若含 NUL 或高占比替换符,按解码失败处理。
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", errors="replace")
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if data[:4] in (b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff"):
        return data.decode("utf-32", errors="replace")
    if b"\x00" in data[:200]:
        # 无 BOM 但含零字节:未知宽字符编码,按 UTF-16 兜底再校验
        guess = data.decode("utf-16", errors="ignore")
        if guess and "\x00" not in guess:
            return guess
        raise ValueError("无法识别的文本编码(疑似宽字符/二进制内容)")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("gbk")
            if "\x00" in text:
                raise ValueError("无法识别的文本编码(疑似宽字符/二进制内容)")
            return text
        except UnicodeDecodeError:
            fallback = data.decode("utf-8", errors="replace")
            # 替换符占比过高 = 实际上没解出来,别把乱码当文本
            if fallback.count("\ufffd") > len(fallback) * 0.05:
                raise ValueError("无法识别的文本编码")
            return fallback


def _parse_pdf(data: bytes) -> str:
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for i, page in enumerate(reader.pages):
        try:
            text = page.extract_text() or ""
        except Exception as e:  # 单页损坏不整体失败
            logger.warning(f"pdf 第 {i + 1} 页解析失败(跳过): {e}")
            text = ""
        if text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)


def _parse_docx(data: bytes) -> str:
    import docx
    document = docx.Document(io.BytesIO(data))
    parts: List[str] = []
    for para in document.paragraphs:
        text = (para.text or "").strip()
        if text:
            style = (para.style.name or "").lower()
            # 保留标题层级信息(切块的标题边界用)
            if "heading 1" in style:
                parts.append("# " + text)
            elif "heading 2" in style:
                parts.append("## " + text)
            elif "heading 3" in style:
                parts.append("### " + text)
            else:
                parts.append(text)
    for table in document.tables:
        for row in table.rows:
            cells = [(c.text or "").strip() for c in row.cells]
            line = " | ".join(c for c in cells if c)
            if line:
                parts.append(line)
    return "\n\n".join(parts)


# ── 切块:纯文本 → chunks ────────────────────────────────────

def _split_blocks(text: str) -> List[str]:
    """切成"结构块":标题行独立成块(保留 # 前缀),其余按空行分段。"""
    blocks: List[str] = []
    current: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if _is_heading_line(stripped):
            if current:
                blocks.append("\n".join(current).strip())
                current = []
            blocks.append(stripped)  # 标题独立成块(软边界)
        elif stripped == "":
            if current:
                blocks.append("\n".join(current).strip())
                current = []
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def _hard_split(block: str, max_chars: int) -> List[str]:
    """超长块按句读边界细切(句号/问叹号/分号/换行),仍超长则暴力截断。"""
    if len(block) <= max_chars:
        return [block]
    sentences = re.split(r"(?<=[。！？；;!?])\s*|\n", block)
    parts: List[str] = []
    buf = ""
    for sent in sentences:
        if not sent:
            continue
        while len(sent) > max_chars:  # 单句超长:暴力切
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(sent[:max_chars])
            sent = sent[max_chars:]
        if len(buf) + len(sent) > max_chars and buf:
            parts.append(buf)
            buf = sent
        else:
            buf = buf + ("\n" if buf else "") + sent if buf else sent
    if buf:
        parts.append(buf)
    return parts


def sub_chunks_for_embedding(text: str, size: int = 1200) -> List[str]:
    """向量化子切:按句读边界切成 ≤size 的段。

    抽取块可以是整章(structural_max 上限万级),而 embedding 输入有限
    (本地 bge-m3 实际按 4096 token 截断,超长部分对向量是隐形的;且单
    向量表达长文会语义稀释)。抽取按章、召回按段——子块 id 由调用方
    挂父块(f"{chunk_id}#i"),引用与去重按父块聚合。
    """
    size = max(300, int(size or 1200))
    return [p for p in _hard_split(text or "", size) if p.strip()]


def chunk_text(
    text: str,
    target_chars: int = 1200,
    overlap_chars: int = 100,
    max_chars: int = 3000,
    structural_max_chars: int = 10000,
) -> List[Dict[str, Any]]:
    """结构感知切块。Returns: [{seq, text, char_count}](seq 从 0 连续)。

    粒度策略(抽取与检索解耦的前提):
    - 结构块(标题开头:一章/一节)按 structural_max_chars 上限——能识别
      章节就不切,整章一块,超长章才在句读处兜底切;
    - 无结构纯文本维持 target_chars 软目标,max_chars 为其硬上限;
    - 向量化侧按需把结构块再子切(local_embeddings 实际仅 4096 token,
      长块尾部对向量是隐形的——抽取大块 + 向量小块,两头都最优)。
    """
    if not text or not text.strip():
        return []

    # 归一参数(防呆:非法配置不至于炸掉导入)
    target_chars = max(200, int(target_chars or 1200))
    max_chars = max(target_chars, int(max_chars or 3000))
    structural_max = max(max_chars, int(structural_max_chars or 10000))
    overlap_chars = max(0, min(int(overlap_chars or 0), target_chars // 2))

    chunks: List[str] = []
    buf = ""
    buf_structural = False   # 缓冲是否以标题开头(结构块:目录区/一章)
    buf_has_content = False  # 缓冲里是否混入过非标题正文(纯标题=目录行)
    for block in _split_blocks(text):
        is_head = _is_heading_line(block)
        # 标题 = 章节边界:上一缓冲含实质内容时结算,新章从标题重新起块。
        # 短于 _HEADING_FLUSH_MIN 的缓冲(孤立短行/单独"目录"两字)不值得
        # 独立成块,并入下一章。纯标题缓冲(目录页)不结算——目录行合并
        # 成尽量少的大块,而不是每行一个碎块(诛仙实测:逐行碎块让前 60+
        # 批 LLM 调用全为 0 实体)。
        if (is_head and buf and buf_has_content
                and len(buf) >= _HEADING_FLUSH_MIN):
            chunks.append(buf)
            buf = ""
            buf_structural = False
            buf_has_content = False
        if not is_head:
            # 正文到达而缓冲还是纯标题(目录区结束) → 目录自成一块结算,
            # 不与第一章正文粘连;短标题串(<40 字)并入正文。
            # 【坑位记录】标记必须在 pieces 循环前置位:放在循环后的话,
            # 章内第二个分段会把"标题+首段"误判成纯目录缓冲冲掉(实测踩过)
            if buf and not buf_has_content and len(buf) >= _HEADING_FLUSH_MIN:
                chunks.append(buf)
                buf = ""
                buf_structural = False
            buf_has_content = True
        for piece in _hard_split(block, max_chars):
            # 预切粒度 = max_chars:无结构路径的块硬上限不被突破;
            # 结构路径靠缓冲合并到 structural_max(碎件重组,不影响)
            # 结构块(标题开头)按 structural_max 上限:一章尽量一块——章内
            # 上下文完整,实体/关系不被章内切块打断;无结构纯文本维持
            # target_chars 软目标。超上限仍按句读细切兜底。
            ceiling = structural_max if (buf_structural or is_head) else target_chars
            if len(buf) + len(piece) + 1 > ceiling and buf:
                chunks.append(buf)
                buf = piece
                # 超限续段仍属同一章(结构上下文延续,标题在首块):
                # 保持结构标记,续段继续用 structural_max 上限,
                # 否则一章的后半会跌回 target 粒度被切碎
                buf_structural = buf_structural or is_head
            else:
                buf = buf + "\n" + piece if buf else piece
                buf_structural = buf_structural or is_head
    if buf:
        chunks.append(buf)

    # 相邻块重叠:后块带上前块尾部(跨块语义连续;标题块不加,避免污染)
    if overlap_chars > 0:
        overlapped: List[str] = []
        for i, c in enumerate(chunks):
            # 标题开头的块不加前块尾部。判定与切块软边界同源(_is_heading_line):
            # 曾只判 markdown #,中文章节标题块("第一章 xxx")被拼上上一章结尾,
            # 表现为"章标题在块中间"的假跨章
            if i == 0 or _is_heading_line(c.split("\n", 1)[0].strip()):
                overlapped.append(c)
                continue
            prev_tail = chunks[i - 1][-overlap_chars:]
            overlapped.append(prev_tail + "\n" + c)
        chunks = overlapped

    # 硬上限最终兜底(重叠可能超)
    final: List[str] = []
    for c in chunks:
        # 结构块允许到 structural_max,兜底切分用同一上限(无结构块
        # 本就 ≤ max_chars ≤ structural_max,不会被这里放大)
        final.extend(_hard_split(c, structural_max))

    return [
        {"seq": i, "text": t, "char_count": len(t)}
        for i, t in enumerate(final) if t.strip()
    ]
