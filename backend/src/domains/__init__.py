"""
Domains 模块 - 自动发现和加载工具包

每个工具包（pack）应该：
1. 在 domains/ 下创建独立目录
2. 提供 pack.py 文件，导出 create_registry() 和 create_prompt_loader()
3. 系统启动时自动发现并加载

插件化约定:
  - create_registry() 必须提供,返回 ToolRegistry
  - create_prompt_loader() 可选,返回 PromptLoader 或 None
    (纯数据类插件如 leave_application 不需要自定义 prompt)
  - 至少一个 pack 需要提供 prompt_loader,否则系统无法构建意图识别 prompt
    (但不会崩溃,会使用 dispatcher 内置的动态 prompt 生成)
"""
import importlib
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from sdk.registry import ToolRegistry
from engine.prompt_loader import PromptLoader

logger = logging.getLogger(__name__)


def discover_packs() -> List[str]:
    """
    自动发现 domains 目录下的所有工具包
    
    识别规则：
    - 目录包含 pack.py 文件
    - pack.py 导出 create_registry 函数
    
    Returns:
        发现的 pack 名称列表
    """
    domains_dir = Path(__file__).parent
    packs = []
    
    for item in domains_dir.iterdir():
        if not item.is_dir():
            continue
        if item.name.startswith('_'):
            continue
            
        pack_file = item / 'pack.py'
        if pack_file.exists():
            packs.append(item.name)
            logger.info(f"发现工具包: {item.name}")
    
    return packs


def load_pack(pack_name: str) -> Tuple[ToolRegistry, Optional[PromptLoader]]:
    """
    加载指定的工具包
    
    Args:
        pack_name: 工具包名称（目录名）
        
    Returns:
        (registry, prompt_loader) 元组。prompt_loader 可能为 None。
    """
    module_path = f"domains.{pack_name}.pack"
    
    try:
        module = importlib.import_module(module_path)
        
        # 调用 create_registry
        if not hasattr(module, 'create_registry'):
            raise AttributeError(f"{pack_name}.pack 缺少 create_registry 函数")
        
        registry = module.create_registry()
        
        # 调用 create_prompt_loader（如果存在）— 返回 None 表示不需要自定义 prompt
        prompt_loader = None
        if hasattr(module, 'create_prompt_loader'):
            prompt_loader = module.create_prompt_loader()
        
        logger.info(f"成功加载工具包: {pack_name}")
        return registry, prompt_loader
        
    except Exception as e:
        logger.error(f"加载工具包 {pack_name} 失败: {e}")
        raise


def load_all_packs() -> Tuple[ToolRegistry, Optional[PromptLoader]]:
    """
    加载所有发现的工具包，合并它们的 registry
    
    Returns:
        (merged_registry, primary_prompt_loader) 元组。
        primary_prompt_loader 可能为 None(所有 pack 都不提供时),
        此时 dispatcher 使用内置的动态 prompt 生成。
    """
    pack_names = discover_packs()
    
    if not pack_names:
        raise RuntimeError("未发现任何工具包")
    
    # 合并所有 registry
    merged_registry = ToolRegistry()
    primary_prompt_loader = None
    
    for pack_name in pack_names:
        try:
            registry, prompt_loader = load_pack(pack_name)
            
            # 合并工具
            for tool in registry.all():
                merged_registry.register(tool)
                logger.debug(f"注册工具: {tool.name} (来自 {pack_name})")
            
            # 使用第一个 pack 的 prompt_loader 作为主要的
            if primary_prompt_loader is None and prompt_loader:
                primary_prompt_loader = prompt_loader
                
        except Exception as e:
            logger.error(f"跳过工具包 {pack_name}: {e}")
            continue
    
    # 不再强制要求 prompt_loader — dispatcher 有内置动态 prompt 生成
    if primary_prompt_loader is None:
        logger.warning(
            "没有工具包提供 prompt_loader,将使用 dispatcher 内置的动态 prompt 生成。"
            "如需自定义 prompt,请在 pack.py 中实现 create_prompt_loader()。"
        )
    
    logger.info(f"成功加载 {len(pack_names)} 个工具包，共 {len(merged_registry.all())} 个工具")
    return merged_registry, primary_prompt_loader
