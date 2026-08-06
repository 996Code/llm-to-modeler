"""Unicode 隐写清洗 — 防上游内容携带隐藏指令(prompt 注入防御)。

【模块定位】
这是整个系统的"安全滤网"。所有从上游(AssetClient / HTTP API / 数据库)
取回的文本,在拼进给 LLM 的 prompt 之前,都必须先经过本模块清洗,
剔除可能被攻击者用来"隐写恶意指令"的不可见 Unicode 字符。

【为什么需要它】
大模型的 prompt 是纯文本。攻击者可以在表单字段、模板、guide 等"看似数据"
的内容里,埋入肉眼看不见但 LLM 能读到的 Unicode 字符(零宽空格、方向反转、
私用区字符等),伪装成系统指令,诱导模型执行恶意操作(典型 prompt injection)。
普通字符串校验抓不到这些字符,因为它们在屏幕上根本不显示。

【对标实现】
对标 Claude Code 的 partiallySanitizeUnicode,具体处理:
- NFKC 归一化(全角→半角,兼容等价合并,防止利用外观相似字符绕过黑名单)
- 删零宽字符 \u200B-\u200F(零宽空格/连接符,纯隐写用)
- 删方向反转字符 \u202A-\u202E(RLO/LRO 可让文本在编辑器里显示成 A 但实际是 B)
- 删 BOM \uFEFF(文件头字节序标记,某些场景会被误读)
- 删私用区 \uE000-\uF8FF(PUA,无标准含义,可被滥用编码任意内容)

【核心约定 / Fail-Closed】
任何从上游 AssetClient 返回的内容,在进入 prompt 前必须经过 sanitize_obj。
这是系统级硬约束 —— 宁可误删合法字符(罕见),也不能放过隐写指令。

【Java 类比】
类似一个 HttpServletRequestFilter / XSS 过滤器:在数据进入业务核心前
统一清洗。Java 里通常用 servlet filter 或 @ControllerAdvice 拦截器实现,
这里则是每次取到上游数据后显式调用 sanitize_obj(调用方责任)。
"""
import unicodedata

# 危险字符范围 —— 以 (起始码点, 结束码点) 元组列出闭区间。
# 集中定义在一处,便于审计和扩展。码点即 Unicode 字符的整数编号(ord(ch))。
_DANGEROUS_RANGES = [
    (0x200B, 0x200F),   # 零宽字符:零宽空格/连接符/断字符,屏幕不可见,纯隐写用
    (0x202A, 0x202E),   # 方向反转:LRO/RLO 等可颠倒文字显示顺序,造成"所见非所读"
    (0x2060, 0x206F),   # 词连接符等不可见格式控制符
    (0xE000, 0xF8FF),   # 私用区 PUA:Unicode 未规定标准含义,可被任意自定义编码
]
# BOM 单独处理(用 replace 一次删干净,比逐字符判断快)。
_BOM = "\ufeff"


def sanitize_text(text: str) -> str:
    """清洗字符串:NFKC 归一化 + 删除 BOM 和危险字符。

    处理顺序固定,不可随意调换:
      1. 先 NFKC 归一化 —— 把全角/兼容形式统一成标准形式,
         防止攻击者用"长得像但码点不同"的字符绕过后续黑名单。
      2. 再删 BOM。
      3. 最后逐字符过滤危险区间。

    Args:
        text: 待清洗的原始字符串。

    Returns:
        清洗后的新字符串(原字符串不变,Python 字符串不可变)。
        空串/None 原样返回(None 在调用方 sanitize_obj 处理)。

    【Java 类比】
    对标 Java 的 Normalizer.normalize(text, Normalizer.Form.NFKC)
    + 一串 String.replace / char 过滤逻辑。
    """
    if not text:
        # 空串或 None 直接返回(Python 中 "" is falsy,一并挡掉)
        return text
    # 1. NFKC 归一化 —— unicodedata.normalize 是 Python 标准库,
    #    "NFKC" = 兼容性分解再组合。效果:全角"Ａ"(U+FF21)→ 半角"A"(U+0041)。
    text = unicodedata.normalize("NFKC", text)
    # 2. 删 BOM —— replace 全量替换,O(n)。
    text = text.replace(_BOM, "")
    # 3. 删危险字符范围 —— 逐字符判断码点是否落入黑名单区间。
    #    用列表 + join 比 str += 高效(Python 中字符串拼接在循环里是 O(n²))。
    result = []
    for ch in text:
        # ord(ch) 取该字符的 Unicode 码点(int)。
        cp = ord(ch)
        # any(...) 命中任一区间就跳过此字符(Fail-Closed:宁可删错)。
        if any(lo <= cp <= hi for lo, hi in _DANGEROUS_RANGES):
            continue
        result.append(ch)
    # 把保留下来的字符列表重新拼成字符串。
    return "".join(result)


def sanitize_obj(obj):
    """递归清洗任意嵌套的 dict / list / str 结构。

    上游返回的资产通常是多层嵌套的 JSON(dict/list/str 混合),
    本函数深度优先遍历,对每个字符串叶子调用 sanitize_text。

    Args:
        obj: 任意 Python 对象(常见为 dict / list / str / None / 数字)。

    Returns:
        清洗后的等价结构(新建副本,不修改入参);非字符串原样返回。
        - dict: key 和 value 中的字符串都会被清洗(key 也可能藏隐写)。
        - list: 每个元素递归清洗。
        - str: 调用 sanitize_text。
        - 其他(int/bool/None...):原样返回。

    【Java 类比】
    类似对 Jackson 的 JsonNode 做递归遍历清洗 ——
    每遇到 TextNode 就过滤,遇到 ObjectNode/ArrayNode 就递归下钻。
    Python 用 isinstance 做类型分派,等价于 Java 的 instanceof。
    """
    if obj is None:
        # None 等价 Java 的 null,直接返回不处理。
        return None
    if isinstance(obj, str):
        # 字符串叶子 —— 调用实际清洗逻辑。
        return sanitize_text(obj)
    if isinstance(obj, dict):
        # dict 推导式:类似 Java Stream 的 collect(toMap(...)),
        # 但同时清洗 key 和 value(key 也可能是用户可控字符串)。
        return {sanitize_text(k) if isinstance(k, str) else k: sanitize_obj(v)
                for k, v in obj.items()}
    if isinstance(obj, list):
        # list 推导式:对每个元素递归。等价 Java 的 stream().map(this::sanitizeObj).toList()。
        return [sanitize_obj(item) for item in obj]
    # 数字、布尔、自定义对象等非目标类型,原样返回(它们不可能是 prompt 注入载体)。
    return obj
