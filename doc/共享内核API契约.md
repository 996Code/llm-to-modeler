# 编排插件共享内核 API 契约（C2/C3 薄入口）

> 面向"编排型插件"(如 metrology_assistant)消费的两个底层服务契约。
> 设计来源:doc/private/metrology-customer-service-design.md §4(双API
> 最小契约)/§4.4(执行结果不能先表格化后猜业务状态)/§7.5(异常分支)。
> 分层铁律(§3):客服调知识库/问数,问数调 SDK,不另开 SDK 旁路;
> 两个入口都是**同进程公开服务接口**,不绕 HTTP 调自己。

## 为什么是"薄入口"而不是直接用交互工具

| 交互工具(kb_search / ask_data) | 薄入口(retrieve_evidence / execute_controlled) |
|---|---|
| 面向终端用户:含答案生成、标注短路、选库记忆、图表/保存/记忆回流 | 面向编排方:只做检索/执行,业务结论归调用方 |
| execute_mcp_tool 会把 `{"status":"forbidden"}` 归一成 ok=True 的"成功数据行" | **先判业务信封再归一**:forbidden/not_found/invalid_input/unavailable 不冒充成功 |
| PackInvokeError 被抹平成字符串 | 错误类型(credential/retryable/permanent)原样保留 |
| 数据源取"第一个 active" | 显式 datasource_id + 固定工具白名单 |

## C2 · 知识库证据服务 `retrieve_evidence`

**归属**: knowledge_graph 插件(`domains/knowledge_graph/service.py`)。

```python
from domains.knowledge_graph import service as kg_service

evidence = kg_service.retrieve_evidence(
    app_state,               # 平台组件句柄(装配注入)
    kb_name="强检目录",       # 显式知识库:不存在即 ValueError,不猜全库
    query="压力表 贸易结算 强制检定",
    conv_id=conv_id,          # 可选:检索记忆/日志归属
    top_k=None,               # 可选:召回条数
    on_stage=stage_fn,        # 可选:进度回调(编排方翻译成自身阶段)
    metadata_filter=None,     # 可选:文档元数据白名单(空集=零结果,不回退全库)
)
```

**返回**(失败抛异常,不静默降级):

```python
{
  "kb":         {"id", "name"},
  "subgraph":   {"nodes", "edges"},     # 图谱路命中(前端图谱卡)
  "chunks":     [ {...片段, 已按用户级口径瘦身} ],   # 证据,无答案
  "diagnostics": {"kb", "docFilterCount", "chunkCount"},  # 范围观测
}
```

**契约要点**:
- 只返回依据,**不生成答案**——三态结论/业务判断由调用方组(设计 §4.2);
- `metadata_filter` 白名单语义:None=无附加条件;配置但空集=零结果,
  "空范围不是放行全库的理由";
- 片段瘦身口径与 answer_question 一致(childText/parentChars 不出域);
- 库不存在抛 `ValueError`(调用方翻译成 error_for_llm,不猜库)。

## C3 · 问数受控执行服务 `execute_controlled`

**归属**: chatbi 插件(`domains/chatbi/controlled.py`)。

```python
from domains.chatbi.controlled import execute_controlled

result = execute_controlled(
    app_state,
    datasource_id="ae7ebf33-...",   # 显式数据源:不存在/停用/非 MCP → 拒绝
    tool="query_progress",          # 固定工具:须在数据源限定范围内(白名单)
    args={"order_no": "BJ2026"},    # 结构化参数:非 dict 拒绝
)
```

**返回** `ControlledResult`(永不静默成功):

```python
result.ok        # True 仅当业务信封 status=ok 且 data 形状合法
result.status    # ok / forbidden / not_found / invalid_input / unavailable / credential / transport
result.data      # ok 时: {"columns","rows"} 或 {"record"}
result.error     # 失败时: ControlledError(code, message, error_type, retryable)
result.source    # {"datasource", "tool"} 观测用
```

**信封判定顺序**(设计 §4.4,先判信封再归一):
1. 前置校验:参数形状/数据源存在且 active/类型必须 mcp/工具白名单——
   失败立即返回,**不发 MCP 请求**;
2. SDK 调用,`PackInvokeError` 错误类型保留(credential≠transport);
3. 上游返回 dict 且 `status ∈ {ok, forbidden, not_found, invalid_input,
   unavailable, error}` **且带 data/message 载荷键** → 业务信封:
   仅 ok 的 data 归一为表格/记录,其余按业务状态分类返回;
4. 信封形状但状态枚举未知 → 契约错误(invalid_input),**不交给模型补齐**;
5. 非信封:纯表格形状归一(与 execute_mcp_tool 同映射)。

**边界(设计 §4.3/§7.5)**:
- 首批仅支持已批准只读 MCP 数据源;拒绝 SQL 字符串、直连 DB 类型;
- 身份注入(ctx 覆盖身份字段)与原始 inputSchema 深度验参属 P2
  (当前靠编排方确定性槽位解析暂代,见 controlled.py 模块注释);
- 查无/拒权是**正常业务终态**(编排方出通知卡),技术故障是
  error_for_llm(平台错误诊断)——两者不可合成一种"没有"。

## 编排方消费范式(metrology_assistant 为例)

```python
# 场景表驱动的动作绑定(assistant_config 表,管理页维护)
action = {"kind": "chatbi", "env": "METROLOGY_PROGRESS_DATASOURCE_ID",
          "tool": "query_progress", "params": {"order_no": "order_no"},
          "notices": {"forbidden": {...}, "not_found": {...}}}

result = execute_controlled(app_state, ds_id, action["tool"], params)
if not result.ok:
    notice = action["notices"].get(result.status)   # 业务状态→通知卡文案
    if notice:
        return _notice(result.status, notice["title"], notice["message"])
    return ToolResult(error_for_llm=...)            # 技术故障不成功化
```

**插件间依赖声明**(manifest,平台零插件名知识):

```yaml
dependencies:
  knowledge_graph:
    pack: knowledge_graph      # 声明依赖 kg 插件启用
    required: true
  chatbi:
    pack: chatbi
    required: true
```

依赖未满足 → 装配闸门跳过本插件;停用被依赖插件 → 管理端 409 拒绝
(反向扫描 `pack_dependents`,见 services/pack_dependency.py)。
