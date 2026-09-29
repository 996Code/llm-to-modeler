"""njmind_bpm pack 内部高频 JSON key 常量 —— 插件私有契约唯一定义处。

draft/topology/规则 JSON 的结构约定（pack 私有，engine 不感知）。
集中定义后拼错在 import 处报错，而不是运行时静默 None。
"""

# 拓扑
NODES = "nodes"
EDGES = "edges"
NODE_KEY = "key"
NODE_TYPE = "type"
NODE_NAME = "name"
EDGE_FROM = "from"
EDGE_TO = "to"

# 节点类型(与 Java BpmTopologyParser NODE_TYPE_* 对齐)
NODE_TYPE_START = "START_EVENT"
NODE_TYPE_USER_TASK = "USER_TASK"
NODE_TYPE_CC = "CC_TASK"
NODE_TYPE_GATEWAY = "EXCLUSIVE_GATEWAY"
NODE_TYPE_END = "END_EVENT"

# draft 顶层
TOPOLOGY = "topology"
PROCESS_KEY = "processKey"        # 宿主提供，LLM 不填
PROCESS_NAME = "processName"      # 宿主提供，LLM 不填
FORM_KEY = "formKey"

# 规则引用（draft 用语义 key；最终规则才是 taskId/nodeId/sequenceFlowId）
APPROVE_RULES = "approveRules"
CC_RULES = "ccRules"
BRANCH_RULES = "branchRules"
NODE_KEY_REF = "nodeKey"
EDGE_KEY_REF = "edgeKey"
TASK_ID = "taskId"
SEQUENCE_FLOW_ID = "sequenceFlowId"

# render 响应
BPMN_XML = "bpmnXml"
RULES = "rules"
VALIDATION = "validation"
WARNINGS = "warnings"
