"""
Domains 模块 - 工具包(pack)的自动发现与加载器。

【模块定位】
这是"插件化"架构的入口。系统启动时不需要硬编码"加载哪些工具",而是扫描
domains/ 目录,发现所有符合约定的子目录(工具包),动态导入并合并它们的工具。
新增一个业务领域(如请假、报销),只需新建一个目录 + 写 pack.py,无需改这里。

【pack(工具包)约定】
每个工具包应该:
1. 在 domains/ 下创建独立目录(如 domains/leave_application/)
2. 提供一个 pack.py 文件,导出两个工厂函数:
   - create_registry(): 必须提供,返回装好工具的 ToolRegistry
   - create_prompt_loader(): 可选,返回 PromptLoader 或 None
3. 系统启动时自动发现并加载(本模块负责)

【插件化约定(详解)】
  - create_registry() 必须提供,返回 ToolRegistry —— 这是 pack 的核心职责。
  - create_prompt_loader() 可选,返回 PromptLoader 或 None
    (纯数据类插件如 leave_application 不需要自定义 prompt)
  - 至少一个 pack 需要提供 prompt_loader,否则系统无法构建意图识别 prompt
    (但不会崩溃,二级路由使用 sdk 的 DefaultPackRouter 中性框架)

【Java 类比】
对标 Java 的 SPI(Service Provider Interface)+ 自动扫描:
- Java 里 META-INF/services 注册实现,ServiceLoader.load(...) 发现;
  这里是"目录里有 pack.py 就算一个 pack",约定优于配置。
- importlib.import_module(...) 动态加载模块,等价 Java 的 Class.forName(...)。
- 合并多个 pack 的 registry,类似 Spring 把多个 @Configuration 的 Bean
  合并进同一个 ApplicationContext。

【为什么用自动发现】
开闭原则 —— 对扩展开放,对修改关闭。新增 pack 不改本文件;
移除 pack 只需删目录或改名(下划线开头会被跳过,见 _is_hidden 说明)。
"""
import importlib
import logging
import os
from pathlib import Path
from typing import List, Optional, Tuple

from sdk.registry import ToolRegistry
from engine.prompt_loader import PromptLoader

# 模块级 logger。Python 用 logging.getLogger(__name__),name 形如 "domains"。
# 等价 Java 的 private static final Logger log = LoggerFactory.getLogger(...)。
logger = logging.getLogger(__name__)


def discover_packs() -> List[str]:
    """
    自动发现 domains 目录下的工具包（可被 PACKS_ENABLED 上层配置过滤）。

    扫描本文件所在目录(domains/)下的每个子目录,符合"包含 pack.py"
    即视为一个工具包。

    识别规则:
    - 必须是目录(跳过普通文件/__pycache__ 等)
    - 目录名不以 '_' 开头(_ 前缀视为内部/隐藏目录,如 __pycache__、_common)
    - 目录内存在 pack.py 文件

    上层配置（部署方决定加载哪些插件，未配置 = 全部加载的缺省逻辑）:
    - 环境变量 PACKS_ENABLED（逗号分隔 pack 名）：白名单过滤。
      生产部署只启用业务需要的 pack（如 "njmind_form"），演示 pack
      （leave_application）不进生产；缺省不设 = 加载全部（向后兼容）。
      配了但名单里的 pack 不存在 → 告警并忽略该名字（Fail-Open 到存在的）。

    Returns:
        发现的 pack 名称列表(即目录名,如 ["njmind_form", "leave_application"])。
        顺序由文件系统返回顺序决定,不保证固定;依赖顺序的逻辑应避免。

    【Java 类比】
    类似用 Files.walk 或 File.listFiles 扫描 classpath 目录,
    再按约定过滤出"插件目录";PACKS_ENABLED ≈ Spring 的
    spring.profiles.include 式白名单。
    """
    # __file__ 是本文件路径,Path(__file__).parent 取所在目录(domains/)。
    # Python 里靠运行时路径定位,而非 Java 的 classpath 抽象。
    domains_dir = Path(__file__).parent
    packs = []

    # 遍历 domains/ 下每一项(文件或目录)。iterdir() 等价 Java 的 listFiles()。
    for item in domains_dir.iterdir():
        # 跳过非目录(如 __init__.py 本身、README 等)。
        if not item.is_dir():
            continue
        # 下划线开头的目录视为内部/隐藏(约定),如 __pycache__、_common 工具库。
        # 这是"下划线 = 内部"的 Python 惯例,类似 Java 里 internal 包。
        if item.name.startswith('_'):
            continue

        # 拼出 pack.py 的预期路径并检查是否存在。
        pack_file = item / 'pack.py'
        if pack_file.exists():
            packs.append(item.name)
            logger.info(f"发现工具包: {item.name}")

    # 上层白名单过滤：部署方声明 PACKS_ENABLED 时只加载名单内的
    enabled = _packs_whitelist()
    if enabled is not None:
        unknown = enabled - set(packs)
        if unknown:
            logger.warning(f"PACKS_ENABLED 含未发现的 pack（忽略）: {sorted(unknown)}")
        packs = [p for p in packs if p in enabled]
        logger.info(f"PACKS_ENABLED 白名单生效，加载: {packs}")

    return packs


def _packs_whitelist() -> Optional[set]:
    """读取部署方白名单 PACKS_ENABLED（逗号分隔 pack 名）。

    未配置返回 None（= 加载全部，含演示 pack）；配置后只允许名单内的 pack。
    discover_packs() 与 load_pack_configs() 共享同一套过滤，
    保证 /api/meta/packs 不暴露禁用插件的 manifest。
    """
    raw = os.getenv("PACKS_ENABLED", "").strip()
    if not raw:
        return None
    return {n.strip() for n in raw.split(",") if n.strip()}


def load_pack(pack_name: str) -> Tuple[ToolRegistry, Optional[PromptLoader], object]:
    """
    加载指定的工具包:动态导入它的 pack.py,调用工厂函数取出 registry 和 loader。

    流程:
      1. importlib.import_module("domains.<pack_name>.pack") 动态导入模块
      2. 调用模块的 create_registry() —— 必须,返回工具集合
      3. 若模块定义了 create_prompt_loader(),也调用它 —— 可选

    Args:
        pack_name: 工具包名称(目录名,如 "njmind_form")。

    Returns:
        (registry, prompt_loader) 二元组。
        - registry: 该 pack 的 ToolRegistry(非空)
        - prompt_loader: 该 pack 的 PromptLoader,或 None(纯数据类 pack 不提供)

    Raises:
        AttributeError: pack.py 缺少必须的 create_registry 函数(契约违反)。
        原始异常:  importlib 或工厂函数内部抛出的任何异常都会原样上抛(见下方 try)。

    【Java 类比】
    Class<?> cls = Class.forName("domains." + packName + ".pack");
    Method m = cls.getMethod("create_registry");
    ToolRegistry reg = (ToolRegistry) m.invoke(null);  // 类似静态工厂调用
    """
    # 模块的点分路径,如 "domains.njmind_form.pack"。
    # Python 的 import 路径用点号分隔,等价 Java 的全限定类名。
    module_path = f"domains.{pack_name}.pack"

    try:
        # 动态导入:首次调用会执行模块顶层代码(注册类、初始化常量等),
        # 后续调用从 sys.modules 缓存返回。等价 Java 的 Class.forName + 类初始化。
        module = importlib.import_module(module_path)

        # 调用 create_registry —— pack 契约的硬性要求。
        # hasattr(obj, name) 检查属性是否存在,等价 Java 反射的 getMethod + null 判断。
        if not hasattr(module, 'create_registry'):
            raise AttributeError(f"{pack_name}.pack 缺少 create_registry 函数")

        registry = module.create_registry()

        # 调用 create_prompt_loader(如果存在)— 返回 None 表示不需要自定义 prompt。
        # 这是可选契约,所以先 hasattr 判定再调用,缺失时给 None 默认值。
        prompt_loader = None
        if hasattr(module, 'create_prompt_loader'):
            prompt_loader = module.create_prompt_loader()

        # 可选契约 create_router：pack 自带二级路由（领域知识归 pack）。
        # 缺失时回退 DefaultPackRouter（中性框架，行为同旧扁平路由）。
        from sdk.pack_router import DefaultPackRouter
        router = None
        if hasattr(module, 'create_router'):
            try:
                router = module.create_router(registry)
            except TypeError:
                # 工厂签名不含参（如无参 create_router()），重试无参调用
                router = module.create_router()
        if router is None:
            router = DefaultPackRouter(registry)

        logger.info(f"成功加载工具包: {pack_name}")
        return registry, prompt_loader, router

    except Exception as e:
        # 捕获后打日志再 raise —— 记录上下文便于排查,但异常原样上抛由调用方决策。
        # 注意:这里不吞异常(Fail-Fast),因为单个 pack 加载失败通常意味着系统不可用。
        logger.error(f"加载工具包 {pack_name} 失败: {e}")
        raise


def load_all_packs() -> Tuple[ToolRegistry, Optional[PromptLoader], dict]:
    """
    加载所有已发现的工具包,合并它们的 registry 成一个全局注册表。

    流程:
      1. discover_packs() 扫出所有 pack 名
      2. 逐个 load_pack 加载
      3. 把每个 pack 的工具合并进同一个 merged_registry
      4. 选第一个非空的 prompt_loader 作为 primary

    Returns:
        (merged_registry, primary_prompt_loader) 元组。
        - merged_registry: 合并后的全局工具注册表(至少 1 个工具)
        - primary_prompt_loader: 主 prompt 加载器,可能为 None
          (所有 pack 都不提供时),此时二级路由使用 DefaultPackRouter 中性框架。

    Raises:
        RuntimeError: 一个 pack 都没发现(系统无法工作)。

    【容错策略】
    - 单个 pack 加载失败不会让整个系统启动失败:catch 后 continue 跳过,
      只记录错误日志。这是"尽力而为"策略,保证可用 pack 仍能服务。
    - 但没有任何可用 pack 时,抛 RuntimeError 终止启动。

    【Java 类比】
    类似 Spring 启动时把所有 @Component 扫进同一个 ApplicationContext:
    某个 Bean 加载失败通常只影响自身(可降级),全失败才中止。
    """
    pack_names = discover_packs()

    if not pack_names:
        # 一个 pack 都没有 —— 系统无工具可用,直接终止启动(Fail-Closed)。
        raise RuntimeError("未发现任何工具包")

    # 合并所有 registry:新建一个空 registry,逐个把工具搬进来。
    merged_registry = ToolRegistry()
    primary_prompt_loader = None
    # pack → 二级路由映射（引擎一级路由选中领域后调它选工具）
    pack_routers: dict = {}

    for pack_name in pack_names:
        try:
            registry, prompt_loader, router = load_pack(pack_name)
            pack_routers[pack_name] = router

            # 合并工具:遍历该 pack 的所有工具,注册进全局 registry。
            # 注意 tool.name 全局唯一,同名会覆盖 —— pack 之间工具名不能冲突。
            for tool in registry.all():
                merged_registry.register(tool)
                logger.debug(f"注册工具: {tool.name} (来自 {pack_name})")

            # 使用第一个 pack 的 prompt_loader 作为主要的。
            # 设计取舍:prompt_loader 只取一个而非合并,因为意图识别 prompt
            # 通常只需一份通用模板;第一个提供者(字典序靠前)优先。
            if primary_prompt_loader is None and prompt_loader:
                primary_prompt_loader = prompt_loader

        except Exception as e:
            # 单个 pack 失败:打日志后跳过,不中断整体启动(尽力而为)。
            logger.error(f"跳过工具包 {pack_name}: {e}")
            continue

    # 不再强制要求 prompt_loader — 二级路由有 DefaultPackRouter 兜底。
    # 即所有 pack 都没提供 prompt_loader 也能工作,只是意图识别精度可能下降。
    if primary_prompt_loader is None:
        logger.warning(
            "没有工具包提供 prompt_loader,二级路由将使用 DefaultPackRouter 中性框架。"
            "如需自定义 prompt,请在 pack.py 中实现 create_prompt_loader()。"
        )

    logger.info(f"成功加载 {len(pack_names)} 个工具包，共 {len(merged_registry.all())} 个工具")
    return merged_registry, primary_prompt_loader, pack_routers


def load_pack_configs() -> dict:
    """加载各 pack 的 config.yaml 为只读 dict（供 /api/meta/packs 暴露 manifest）。

    pack 目录约定:``domains/<pack_name>/config.yaml`` 存在则读为 dict,
    不存在返回空 dict(纯数据类 pack 可以没有静态配置)。
    每个 pack 的 config.yaml 就是它的 manifest 声明(制品类型/diff 对齐键/服务依赖)。

    与 discover_packs 共享同一白名单过滤:部署方设置 PACKS_ENABLED 时,
    /api/meta/packs 只暴露名单内的 pack,避免禁用插件的信息(工具/服务依赖)外泄。
    """
    import yaml

    allowed = _packs_whitelist()
    configs = {}
    domains_dir = Path(__file__).parent
    for item in domains_dir.iterdir():
        if not item.is_dir() or item.name.startswith('_'):
            continue
        if allowed is not None and item.name not in allowed:
            continue
        cfg_path = item / "config.yaml"
        if cfg_path.exists():
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    configs[item.name] = yaml.safe_load(f) or {}
            except Exception as e:
                logger.error(f"加载 {item.name} config.yaml 失败: {e}")
    return configs
