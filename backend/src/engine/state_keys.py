"""GraphState 字段名常量 —— 跨模块契约 key 的唯一定义处。

【为什么需要】
GraphState 是 TypedDict（类型安全），但 api 层组装 state、nodes 层读写
state 时用的是字符串 key——两处字符串必须一致，散写就是隐式契约。
常量化后，拼错会在 import 处报错而不是运行时静默 None。

【Java 类比】
类似把 properties 文件的 key 抽成 Constants 接口：
```
interface StateKeys { String CONTEXT_ARTIFACT = "context_artifact"; }
```

【注意】
- 只收 **跨模块** 的 key（api 写 + engine 读 / nodes 写 + 条件边读）；
- pack 内部的领域 key 不在这里——那是插件私有约定，收进各自 pack 的常量文件。
"""

# ── ChatRequest.context 内部的 key（前端写、后端读——跨端契约） ──
# 注意：前端 hostPort.ts 的 CONTEXT_KEY_ARTIFACT 与此值必须一致
CONTEXT_ARTIFACT = "artifact"               # 宿主下发的当前制品

# ── GraphState 的字段名（api/stream 组装 → nodes 读 / nodes 写 → 条件边读） ──
STATE_CONTEXT_ARTIFACT = "context_artifact" # state 里的上下文制品字段（路由判断画布状态）
USER_INPUT = "user_input"                   # 本轮用户消息
COMPRESSED_HISTORY = "compressed_history"   # 压缩后的历史
TOOL_NAME = "tool_name"                     # 路由选中的工具名
