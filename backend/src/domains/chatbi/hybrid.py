"""P1#6 检索混合化 —— 关键词路与 RRF 融合(纯函数,零外部依赖)。

蓝图 A1 裁决:RRF 默认(k=60)/双向包含/应用层融合(引擎配比不抄)。
分词不引 jieba:中文 2-gram + 连续英文数字词足以覆盖表名/列中文名匹配。
"""
import re
from typing import Dict, List

_WORD_RE = re.compile(r"[A-Za-z0-9_]+")
_CN_RE = re.compile(r"[\u4e00-\u9fff]")

_STOPWORDS = {"查询", "所有", "一下", "哪些", "什么", "怎么", "请", "帮",
              "的", "了", "是", "在", "和", "与", "及", "各", "个", "有",
              "统计", "显示", "列出", "看看", "查查", "请问"}


def tokenize_query(query: str) -> List[str]:
    """query → 词袋:连续英文/数字整词 + 中文 2-gram(滑窗,停用词过滤)。"""
    q = (query or "").strip()
    if not q:
        return []
    toks: List[str] = list(_WORD_RE.findall(q))
    # 中文滑窗段:连续中文串切 2-gram;单字残段丢弃(信息量不足)
    for seg in re.findall(r"[\u4e00-\u9fff]+", q):
        if len(seg) == 1:
            continue
        toks.extend(seg[i:i + 2] for i in range(len(seg) - 1))
    return [t for t in toks if t not in _STOPWORDS]


def keyword_scores(query: str, models: List) -> Dict[str, float]:
    """双向包含计分:{model_name: score}(仅命中项)。

    正向:表文本(model_to_text 同源:描述/display_name/列中文名/表名)
      含查询词 → 每词 +1;
    反向:表名(英文)/display_name(中文) 作为词出现在 query 里 → +3
      (点名精确,权重高于正向包含)。
    """
    from domains.chatbi.indexing import model_to_text
    toks = tokenize_query(query)
    if not toks:
        return {}
    # query 的英文/数字 token 集(词级相等判定用——裸子串会让 order
    # 命中 order_status、user 命中 user_id,各吃最高权重)
    q_words = set(t.lower() for t in _WORD_RE.findall(query))
    out: Dict[str, float] = {}
    for m in models:
        text = model_to_text(m).lower()
        score = sum(1.0 for t in toks if t.lower() in text)
        # 反向点名:英文表名须是 query 的完整词(词边界,防子串误命中);
        # 中文 display_name 整串出现在 query(中文无词边界,整串已够严)
        if m.name and m.name.lower() in q_words:
            score += 3.0
        if m.display_name and m.display_name in query:
            score += 3.0
        if score > 0:
            out[m.name] = score
    return out


def rrf_fuse(vector_ranked: List[str], keyword_ranked: List[str],
             k: int = 60) -> List[str]:
    """RRF 融合:score(d)=Σ 1/(k+rank_i(d));未入任一路者不出现。"""
    scores: Dict[str, float] = {}
    for ranked in (vector_ranked, keyword_ranked):
        for rank, name in enumerate(ranked, start=1):
            scores[name] = scores.get(name, 0.0) + 1.0 / (k + rank)
    return [n for n, _ in sorted(scores.items(), key=lambda x: -x[1])]
