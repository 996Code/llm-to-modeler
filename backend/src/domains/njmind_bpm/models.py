"""njmind_bpm 领域模型说明。

Task 6 管线对 draft/topology/规则统一操作纯 dict(与上游 JSON 同构,
经 keys.py 常量取键),不引入 pydantic 强类型层——draft 字段集由上游
bpm_*.schema.json(编译期生成)约束,运行时校验在 Java 侧 render 端点,
Python 侧再包一层模型只会制造双份契约。

曾有 TopologyNode/TopologyEdge/Topology/BpmNodeIntent 模型类,因无消费方
已删(管线即 dict);若未来需要 checkpoint 强类型,从这里重建并同步
config.yaml msgpack_classes。
"""
