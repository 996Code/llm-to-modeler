"""njmind_list 领域模型说明。

B5 管线对 ListConfigVo 保存形态统一操作纯 dict（与上游 JSON 同构，经
keys.py 常量取键），不引入 pydantic 强类型层——VO 字段集由 Java 侧
ListConfigValidator 契约约束（错误码与桥接机械修复一一对应），运行时
校验在上游 validate 端点，Python 侧再包一层模型只会制造双份契约。
"""
