"""njmind_form pack - 表单配置领域的工具注册入口。

【模块定位】
这是 domains/ 自动发现机制约定的"工具包入口文件"。系统启动时,
domains/__init__.py 的 load_pack("njmind_form") 会 import 本模块,
然后调用本模块的 create_registry() 和 create_prompt_loader() 两个工厂函数。

【本 pack 做什么】
面向 njmind-modeler(上游表单设计器),提供一整套表单配置工具:
创建、修改、查询、克隆、图片转表单、以及通用问答。LLM 根据用户意图
选择其中一个工具执行。

【契约】
- 必须提供 create_registry():返回装好工具的 ToolRegistry。
- 可选提供 create_prompt_loader():返回 PromptLoader,供意图识别 prompt 使用。

【Java 类比】
本文件类似一个 Spring 的 @Configuration 配置类,在其中 @Bean 声明一组服务。
- create_registry 相当于把多个 @Bean 收集进一个 registry。
- 顶部的 import 静态导入工具类,等价 Java 的 import 服务实现类。
- 注意所有工具都是 `无参 new` —— 工具的依赖(llm_client 等)不在构造时注入,
  而是在 execute 时由 Engine 通过 ToolContext 传入(参数注入而非构造注入)。
"""
from pathlib import Path

from sdk.registry import ToolRegistry
# 导入本 pack 的各个工具实现类。每个工具是 Tool 的子类(见 sdk/tool.py)。
# 这些 import 必须在 pack 顶层,确保动态 import 本模块时类已被加载。
from domains.njmind_form.tools.create_form import CreateFormTool
from domains.njmind_form.router import NjmindFormRouter
from domains.njmind_form.tools.modify_form import ModifyFormTool
from domains.njmind_form.tools.get_form import GetFormTool
from domains.njmind_form.tools.clone_form import CloneFormTool
from domains.njmind_form.tools.image_form import ImageFormTool
from domains.njmind_form.tools.chat import ChatTool


def create_registry() -> ToolRegistry:
    """创建并注册 njmind_form pack 的全部工具。

    实例化每个工具类并登记进 ToolRegistry。工具名由各类的 name 属性决定
    (LLM 选择工具时看到的就是这些名字),pack 之间工具名需全局唯一。

    Returns:
        装好 6 个工具的 ToolRegistry。

    【Java 类比】
    等价于:
        ToolRegistry r = new ToolRegistry();
        r.register(new CreateFormTool());
        ...
        return r;
    其中每个 XxxTool 都是无状态单例(状态由外部 state dict 传入,不存自身字段)。
    """
    registry = ToolRegistry()
    # 逐个实例化并注册。顺序不影响功能(注册表是按 name 索引的 dict)。
    registry.register(CreateFormTool())   # 创建新表单
    registry.register(ModifyFormTool())   # 修改已有表单
    registry.register(GetFormTool())      # 查询表单详情
    registry.register(CloneFormTool())    # 克隆表单
    registry.register(ImageFormTool())    # 从图片识别生成表单
    registry.register(ChatTool())         # 通用问答/闲聊兜底
    return registry


def create_prompt_loader():
    """创建 PromptLoader,指向 domains 目录。

    PromptLoader 负责从文件系统加载意图识别/工具说明的 prompt 模板。
    本 pack 是系统内主要的(通常也是唯一的)prompt 提供方 ——
    其他 pack(如 leave_application)的 create_prompt_loader 返回 None,
    系统就会回退到本 pack 提供的 loader。

    Returns:
        PromptLoader 实例,packs_root 指向 backend/src/domains 目录。

    Note:
        这里用函数内 import(deferred import)而不是文件顶部 import,
        是为了避免循环依赖:engine.prompt_loader 反过来可能引用 pack 层。
        把 import 放进函数体内,只在真正调用时才加载,绕开 import 时的环。

    【Java 类比】
    类似 Java 里把依赖获取放进方法内(JIT 解析),而非构造器字段 ——
    用于打破类初始化期的循环依赖。
    """
    from engine.prompt_loader import PromptLoader
    # Path(__file__) 是本 pack.py 的路径;.resolve() 转绝对路径;
    # .parent 是 njmind_form 目录;.parent.parent 上溯到 domains 目录。
    # 即 packs_root = backend/src/domains
    domains_dir = Path(__file__).resolve().parent.parent
    return PromptLoader(packs_root=domains_dir)


def create_router(registry: ToolRegistry = None):
    """pack 二级路由工厂（可选契约）：领域工具选择的判断规则归 pack。

    引擎一级路由选中本领域后，由它在本 pack 工具集内选出具体工具
    （create/modify/get/... 的分流依据画布状态与话术，见 router.py）。
    不提供本工厂的 pack 回退 DefaultPackRouter（中性框架，行为同旧扁平路由）。
    """
    if registry is None:
        registry = create_registry()
    return NjmindFormRouter(registry)


def enhance_asset_client(asset_client, upstream):
    """pack 装配钩子：向通用 adapter 注入本 pack 的领域客户端。

    端点表/服务名/凭证策略/响应归一化都是 njmind 领域知识，住在
    domains/njmind_form/upstream.py；adapter（HttpAssetClient）保持零
    领域知识，配置类方法全部委托此处注入的 ModelerAPI。
    pack_manager 装配/热切换时调用（可选钩子，无此函数的 pack 跳过）。
    """
    from domains.njmind_form.upstream import ModelerAPI
    asset_client.set_config_api(ModelerAPI(upstream))
