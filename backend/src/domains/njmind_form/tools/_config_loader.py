"""config.yaml 加载器 - 从 njmind_form/config.yaml 读取类型映射。

【模块定位】
本模块负责读取并缓存 njmind_form/config.yaml 里的"类型映射表":
- type_to_template: 字段类型 → 该类型的字段模板片段
- type_names:        字段类型 → 类型名称(供日志/LLM 理解)

供 CreateFormTool / ModifyFormTool 使用 —— 这两个工具生成表单字段时,
需要根据字段类型(int code)查出对应的模板片段和可读名称。

【文件名下划线前缀】
模块名以 `_` 开头(_config_loader),按 Python 惯例表示"模块内部使用",
不对外暴露(类似 Java 的 internal 包)。

【缓存策略】
配置文件启动后不变,因此只读一次并缓存在模块级变量 _CACHE 里,
后续调用直接返回缓存,避免重复磁盘 IO 和 YAML 解析。

【Java 类比】
类似一个用 static holder 实现的单例配置仓库:
    private static volatile Map<...> CACHE;
    public static synchronized Map<...> load() { if (CACHE==null) ...; return CACHE; }
Python 单线程内赋值即"发布",这里不考虑多线程并发首写(服务启动时单线程加载)。
"""
from pathlib import Path
from typing import Any, Dict, Tuple, Optional

import yaml

# 配置文件路径:本文件在 tools/ 下,上溯两级到 njmind_form/ 目录。
# Path(__file__).resolve() 取本 py 文件绝对路径;.parent.parent 上溯两级。
_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"
# 缓存槽:None 表示尚未加载;非 None 表示已缓存两个映射表。
# 模块级变量,等价 Java 的 static 字段。
_CACHE: Optional[Tuple[Dict[int, str], Dict[int, str]]] = None
# 校验机械修复的属性兜底值缓存：{类型code: {属性名: 默认值}}
_PROP_DEFAULTS: Optional[Dict[int, Dict[str, Any]]] = None


def load_type_mappings() -> Tuple[Dict[int, str], Dict[int, str]]:
    """加载 type_to_template 和 type_names 两个映射表。

    首次调用读取磁盘 config.yaml 并解析;之后直接返回缓存。
    key 统一转成 int(字段类型 code)。

    Returns:
        二元组 (type_to_template, type_names):
        - type_to_template: Dict[int, str] —— 类型 code → 字段模板片段(JSON 字符串)
        - type_names:        Dict[int, str] —— 类型 code → 类型名称(如 "TEXT")

    Note:
        结果缓存到模块级 _CACHE,只读一次文件。配置运行期不变更;
        如需热更新配置需重启进程(或显式清空 _CACHE)。

    【Java 类比】
    一个 lazy-init 的 static getter:首次进入同步初始化,之后无锁返回缓存。
    """
    # 声明引用模块级 _CACHE(global),否则赋值会创建局部变量而非改模块级。
    # 这是 Python 的作用域规则:函数内赋值默认是局部变量,要改全局需显式 global。
    global _CACHE
    if _CACHE is not None:
        # 命中缓存,直接返回 —— 跳过磁盘 IO。
        return _CACHE

    # 首次加载:打开文件并解析 YAML。
    # encoding="utf-8" 显式指定编码 —— Windows 默认 GBK,会导致中文配置读出乱码,
    # 这是不熟悉 Python 的 Java 开发者常踩的坑(Java 默认跟随系统编码,同样有此问题)。
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        # yaml.safe_load 把 YAML 文本解析成 Python dict/list(等价 Jackson 读 YAML)。
        # 用 safe_load 而非 load:避免 YAML 的任意对象反序列化漏洞(安全)。
        cfg = yaml.safe_load(f)

    # YAML 的 key 读出来可能是 str(即使写的是数字),这里转成 int 统一 key 类型。
    # dict 推导式:遍历原 dict 的 k/v,生成新 dict。等价 Java 的 stream + collect(toMap)。
    # cfg.get("type_to_template", {}) 容错:缺失节时返回空 dict 而非抛 KeyError。
    t2t = {int(k): v for k, v in cfg.get("type_to_template", {}).items()}
    tn = {int(k): v for k, v in cfg.get("type_names", {}).items()}

    # 写入缓存并返回。后续所有调用走 if _CACHE is not None 分支。
    _CACHE = (t2t, tn)

    # 顺带解析 required_prop_defaults（key 转 int，值保持原样）
    global _PROP_DEFAULTS
    _PROP_DEFAULTS = {
        int(k): dict(v)
        for k, v in (cfg.get("required_prop_defaults") or {}).items()
    }
    return _CACHE


def field_template_stem(type_code: int, type_name: str) -> str:
    """类型 → 上游字段模板 stem（pack 内共用：create 拉模板 / modify 增量
    add_field 骨架 fallback 两处同源）。

    规则：config.yaml 的 type_to_template 例外表（按 code）优先，
    默认按类型名小写推导（TEXT → text_field）。例外表与旧 create_form 的
    _TYPE_OVERRIDES（按名）内容等价，统一收敛到这里单一事实源。
    """
    t2t, _ = load_type_mappings()
    return t2t.get(int(type_code)) or type_name.lower()


def load_prop_defaults() -> Dict[int, Dict[str, Any]]:
    """类型 → 必填属性兜底值（校验机械修复第③级来源）。"""
    if _PROP_DEFAULTS is None:
        load_type_mappings()
    return _PROP_DEFAULTS or {}
