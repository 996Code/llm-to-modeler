# Implementation Tasks

> 任务按 v4 设计文档的绞杀者模式 5 阶段组织（详见 `docs/superpowers/specs/2026-07-18-tool-assistant-architecture-design.md` §7）。
> 每阶段应用都能跑、能测、能回滚。阶段 2-4 的 C.2 增强项可并行推进。

## 1. 阶段 0:骨架搭建（无功能改动）

- [ ] 1.1 创建 `engine/`、`sdk/`、`domains/njmind_form/`、`adapters/` 目录结构
- [ ] 1.2 在 `sdk/tool.py` 实现 `Tool` ABC（含 Fail-Closed 默认值：is_destructive=True/is_read_only=False/is_concurrency_safe=False）+ `validate_input`/`requires_follow_up`/`summarize_artifact`/`title_for` 默认实现
- [ ] 1.3 在 `sdk/tool.py` 实现 `CompositeTool`（steps + run_pipeline + step 异常短路约定）
- [ ] 1.4 在 `sdk/tool.py` 实现 `ToolContext`/`ToolResult`/`AskSpec`/`AskQuestion`/`AskOption` 数据类（含 model_rebuild 前向引用）
- [ ] 1.5 在 `sdk/asset_client.py` 实现 `AssetClient` ABC
- [ ] 1.6 在 `sdk/registry.py` 实现 `ToolRegistry`（register/all/get/describe_for_llm）
- [ ] 1.7 搭出 `engine/dispatcher.py` ToolDispatcher 骨架（空实现，委托旧 graph 代码）
- [ ] 1.8 搭出 `engine/conversation.py` ConversationManager 骨架（空实现，委托旧 conversation_store）
- [ ] 1.9 搭出 `engine/stream.py` StreamManager 骨架（委托旧 sse.py）
- [ ] 1.10 写冒烟测试：确认现有 LangGraph 流程仍工作（无回归）

## 2. 阶段 1:抽 AssetClient + 安全清洗

- [ ] 2.1 把 `backend/src/services/upstream_client.py` 的 9 个端点路径挪到 `domains/njmind_form/config.yaml` 的 `paths` 表
- [ ] 2.2 在 `adapters/http_asset_client.py` 实现 `HttpAssetClient`（配 base_url + path_map，委托现有 UpstreamClient）
- [ ] 2.3 在 `sdk/sanitize.py` 实现 `sanitize_text` + `sanitize_obj`（NFKC + 删零宽 `\u200B-F` + 方向反转 `\u202A-E` + BOM + PUA）
- [ ] 2.4 HttpAssetClient 所有 `get_*` 方法返回前调 `sanitize_obj`
- [ ] 2.5 HttpAssetClient 加连接 memoize + 超时控制
- [ ] 2.6 njmind_form pack 提供 `HttpAssetClient` 实例，Engine 通过 `ToolContext.asset_client` 注入
- [ ] 2.7 删除 `upstream_client.py` 里的路径常量
- [ ] 2.8 单元测试：AssetClient 取模板/schema/guide 正常 + Unicode 清洗生效（含零宽字符用例）

## 3. 阶段 2:抽 Prompt + C.2-C section 缓存 + C.2-E override/append

- [ ] 3.1 把 `prompt_builder.py` 的 4 套 prompt 拆成 `njmind_form/prompts/*.j2`（parse/generate/modify/chat/compact）
- [ ] 3.2 创建 `njmind_form/prompts/_sections/`（intro/field_types/output_rules/safety），工具 prompt 用 `{% include %}` 引用
- [ ] 3.3 在 `engine/prompt_loader.py` 实现 `PromptLoader.render`（Jinja2 渲染 + section 级缓存，cache key=(pack,name,vars 可哈希部分)）
- [ ] 3.4 支持 frontmatter `cacheable: false` 强制重算（如含时间戳的段）
- [ ] 3.5 实现 `PromptLoader.assemble`（override→静态主干→动态段→append 优先级拼装，对标 CC buildEffectiveSystemPrompt）
- [ ] 3.6 实现 `PromptOverrides` 数据类（override/append 字段，当前为空留口子）
- [ ] 3.7 注入防护：pack 领域 prompt 视为 trusted；AssetClient 返回数据作为 user-role 独立 section 注入，绝不进 Jinja2 变量渲染；override/append 不走 Jinja2
- [ ] 3.8 删除 `prompt_builder.py`（逻辑搬进工具的 `_step_*` 方法）
- [ ] 3.9 单元测试：section 渲染 + 缓存命中 + cacheable=false 强制重算 + override/append 拼装顺序

## 4. 阶段 3:管线搬进工具 + C.2-A 追问 + C.2-B 并发

- [ ] 4.1 把 `nodes.py` 的 `TYPE_TO_TEMPLATE`/`TYPE_NAMES` 挪到 `config.yaml`
- [ ] 4.2 实现 `njmind_form/models.py`（FormConfig/ParsedField Pydantic）
- [ ] 4.3 实现 `CreateFormTool`（6 步 steps，内部 `_step_*` 调现有 nodes 节点函数，先搬位置不重写）
- [ ] 4.4 实现 `ModifyFormTool`（3 步 steps，从 source_artifact 出发保留原字段）
- [ ] 4.5 实现 `ChatTool`（is_concurrency_safe=True/is_read_only=True，调 LLM 返回 reply）
- [ ] 4.6 `graph.py` 的拓扑挪到 `CompositeTool.steps` + `run_pipeline`
- [ ] 4.7 实现 `ToolDispatcher._select_tools`（LLM 返回 1..N 工具名，参数从 state 抽取）
- [ ] 4.8 实现 `ToolDispatcher._partition_tool_calls`（按 is_concurrency_safe 分批：连续 safe 并发、unsafe 串行）
- [ ] 4.9 实现 `ToolDispatcher._run_concurrent`（ThreadPoolExecutor，context 修改延迟 apply）
- [ ] 4.10 实现 `ToolDispatcher._run_single`（validate_input 拦截 + execute + 三态分流）
- [ ] 4.11 C.2-A:工具产出 `ToolResult.ask` 时，保存 pending_ask 到 state + emit `type=ask` SSE
- [ ] 4.12 C.2-A:实现 `_resume_ask`（load_state 检测 pending_ask + answers 重跑工具）
- [ ] 4.13 C.2-A:`/api/chat` 新增 `answers` 参数；追问上限 3 轮防死循环
- [ ] 4.14 兼容 `ClarificationRaised` 异常（转成 ToolResult.ask）
- [ ] 4.15 只读工具（fetch_guide/list_assets）声明 `is_concurrency_safe=True`
- [ ] 4.16 persist 类工具实现 `validate_input`（附录 D.5）
- [ ] 4.17 落库前确认：persist 步骤前 emit `confirm` SSE，ToolContext 支持 `dry_run` 跳过 persist 返回预览
- [ ] 4.18 切换 `/api/chat` 走 ToolDispatcher 而非旧 graph
- [ ] 4.19 端到端测试：三意图（create 6 步 / modify 3 步 / chat）+ 追问重跑 + 并发批次

## 5. 阶段 4:存储重建 + 日志安全 + C.2-D 压缩 sidechain + 清理 + 全回归

- [ ] 5.1 新建 `events` 表 schema（id/conv_id/kind/payload/created_at，kind 含 user/assistant/tool_result/compacted/compact_trace/checkpoint/ask）
- [ ] 5.2 新建 `session_meta` 表 schema（conv_id/title/summary/updated_at）
- [ ] 5.3 旧 conversations/messages 表 `ALTER RENAME TO _legacy_*` 留档，不导入数据
- [ ] 5.4 实现 `ConversationManager.append`（只追加不覆盖，按 kind 分流）
- [ ] 5.5 实现 `ConversationManager.load`（按 kind 重建 messages + checkpoint + pending_ask）
- [ ] 5.6 实现 `ConversationManager.save`（ToolResult.summary 入历史，extra 不入，artifact 写 checkpoint）
- [ ] 5.7 实现 `ConversationManager.list_meta`（只查 session_meta，不 JOIN events）
- [ ] 5.8 在 `engine/logging_filter.py` 实现 RedactFilter（正则 redact Bearer/sk-/cookie），Engine 启动挂载
- [ ] 5.9 C.2-D:压缩改 forked 线程执行（独立超时/重试，主对话流不等待，先返回 keep-recent）
- [ ] 5.10 实现 `compact_trace` 条目写入（压缩前后 token 数、摘要、降级标记）
- [ ] 5.11 压缩升级：加 Summary Token 预留（有效窗口 = 总窗口 - 20K）
- [ ] 5.12 压缩升级：PTL 防御（摘要超限时剥 20% 旧分组重试，上限 3 次）
- [ ] 5.13 压缩升级：状态重启补偿（调 `tool.summarize_artifact` + 工具能力复灌重建 tool schema 注入）
- [ ] 5.14 压缩器内容钩子化：Engine 调 `tool.summarize_artifact()`，compact prompt 由 pack 提供
- [ ] 5.15 SSE result payload 钩子化：Engine 调 `tool.format_result()`，不再硬读 formFieldConfigVos
- [ ] 5.16 删除 `graph.py`、`nodes.py`（已迁完）
- [ ] 5.17 验证架构试金石：`grep -rE "form|formCode|template|field" engine/` 应无结果
- [ ] 5.18 端到端全回归：三意图 + 追问 + 并发 + 压缩（sidechain + PTL + 能力复灌）+ header 透传（日志不泄漏）+ 落库确认 + SSE 新格式 + 会话恢复 + 崩溃重放

## 6. 前端配套（接口同步改，一个版本内发齐）

- [ ] 6.1 SSE result 处理：按 `type` 路由（config/ask/reply/error），payload 字段统一
- [ ] 6.2 `type=ask` 渲染追问 UI（AskQuestion → 选项卡片，用户回答后带 `answers` 重发 /api/chat）
- [ ] 6.3 `type=error` 渲染错误提示 + 重试按钮
- [ ] 6.4 `api.chat()` 新增 `answers` 参数透传
- [ ] 6.5 pipeline 阶段展示适配：CREATE_STAGES/MODIFY_STAGES 起始加 `classify_intent`（或 select_tools）步
- [ ] 6.6 前后端契约测试：SSE 事件 schema 一致性校验

## 7. 验收（架构试金石）

- [ ] 7.1 `grep -rE "form|formCode|template|field" engine/` 无结果
- [ ] 7.2 `domains/njmind_form/` 包含全部 njmind 业务知识
- [ ] 7.3 DummyTool + DummyPack 能跑通端到端（证明 Engine 不绑 njmind）
- [ ] 7.4 三个意图行为与改造前一致（无功能回归）
- [ ] 7.5 C.2 五项全部可用：追问重跑 / 工具并发 / section 缓存 / 压缩 sidechain / override-append
- [ ] 7.6 接口约束达成：后端 SSE/请求体重构为 {type,tool,payload,summary}，前端配套同步改
- [ ] 7.7 老数据不迁移：旧表重命名 _legacy_，新表从零开始
- [ ] 7.8 安全防护：Unicode 清洗 + 日志 redact + 落库确认全部生效
