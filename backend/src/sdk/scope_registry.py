"""ScopeRegistry —— SDK 存储设施的命名空间隔离机制。

【模块定位】
图/向量存储下沉 SDK 后,多个插件(knowledge_graph、未来的 NL2BI、
RAG、记忆库…)共用同一套 Neo4j/Milvus 实例。隔离从"巧合"(恰好
kb_id 是 UUID、恰好 collection 叫 kg_*)升级为"机制":

  1. 前缀登记:每个使用 SDK 存储的插件声明一个全局唯一前缀,登记进
     进程级注册表;两个插件撞前缀在装配期就 fail-fast,而不是运行期
     互相读写对方的数据。
  2. scope 签发:传给 store 的 scope_id(图属性/Milvus collection 名
     的组成部分)必须是服务端签发的不可预测 ID——本模块的
     new_scope_id() 或调用方自己的 UUID 生成。契约见 new_scope_id
     的 docstring。

【为什么前缀登记是进程级的】
前缀由插件代码声明(不是运行时数据),进程内登记表足够;跨进程的
唯一性由"前缀写在插件源码里、代码评审保证"承接——两个插件用同一
前缀本身就是代码缺陷,启动即炸是正确行为。

【Java 类比】
JNDI 命名空间 + JMX MBean 的 domain 登记:名字先注册后使用,重名
在部署期暴露。
"""
import threading
import uuid
from typing import Dict

# 进程级登记表:prefix -> owner(声明方标识,通常传 pack 名)
_PREFIXES: Dict[str, str] = {}
_LOCK = threading.Lock()


class PrefixConflictError(RuntimeError):
    """两个使用方声明了相同的前缀——fail-fast,装配期暴露。"""


def register_prefix(prefix: str, owner: str) -> None:
    """登记一个存储命名前缀(幂等;同 owner 重复登记合法)。

    Args:
        prefix: 前缀标识(建议短小、全小写,如 "kg" / "bi" / "rag")。
            它会出现在 Neo4j 约束/索引名与 Milvus collection 名里,
            是该插件全部存储对象的命名空间边界。
        owner: 声明方标识(约定传 pack 名,报错信息可定位)。

    Raises:
        PrefixConflictError: 前缀已被其他 owner 登记。
        ValueError: prefix 为空或不合法(仅允许 [a-z0-9_])。
    """
    if not prefix or not prefix.islower() or not all(c.isalnum() or c == "_" for c in prefix):
        raise ValueError(f"非法前缀: {prefix!r}(仅允许小写字母/数字/下划线)")
    with _LOCK:
        existing = _PREFIXES.get(prefix)
        if existing is not None and existing != owner:
            raise PrefixConflictError(
                f"存储前缀冲突: {owner!r} 与已登记的 {existing!r} 都声明了 "
                f"{prefix!r}——前缀是存储命名空间边界,撞名会导致互相读写"
                f"对方数据;请为其中一个插件更换前缀")
        _PREFIXES[prefix] = owner


def unregister_prefix(prefix: str, owner: str) -> None:
    """注销前缀(插件卸载钩子调用;owner 不匹配则忽略,防误删别人的)。"""
    with _LOCK:
        if _PREFIXES.get(prefix) == owner:
            _PREFIXES.pop(prefix, None)


def registered_prefixes() -> Dict[str, str]:
    """当前登记的前缀快照(诊断/管理端展示用)。"""
    with _LOCK:
        return dict(_PREFIXES)


def new_scope_id() -> str:
    """签发一个新的 scope_id(UUID4)。

    【SDK 存储契约——调用方必须遵守】
    传给 GraphStore / VectorStore 各方法的 scope_id 必须是本函数或调用
    方服务端生成的不可预测 ID,禁止把用户输入直接当 scope_id 用:
    scope_id 是 collection 名/Cypher 属性值的组成部分,用户可控值会
    带来两类风险——
      1. 命名碰撞/越权(用户构造特定 ID 读写别人的 scope);
      2. 命名字符注入(非法字符进 collection 名或 Cypher 字符串)。
    调用方的业务键(如"库名")应存在自己的元数据表里,与物理 scope_id
    做映射;删除时按映射找到 scope_id 再调 store。
    """
    return str(uuid.uuid4())


_UUID_STRICT = __import__("re").compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_scope_id_safe(scope_id: str) -> bool:
    """scope_id 是否符合契约(严格小写连字符 UUID 形态)。

    store 实现层用它做入口防御(不满足即拒收),把契约从文档变成
    代码强制——用户输入直传 scope_id 在 store 门口就被拦下。
    不用 uuid.UUID() 宽松解析:它接受花括号/URN/大写/无连字符等形态,
    其中花括号形态会进 collection 名(Milvus 命名受限),大写形态
    破坏归一化唯一性。
    """
    return isinstance(scope_id, str) and bool(_UUID_STRICT.fullmatch(scope_id))
