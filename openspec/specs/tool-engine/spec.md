# tool-engine Specification

## Purpose
TBD - created by archiving change tool-assistant-refactor. Update Purpose after archive.
## Requirements
### Requirement: 三层六边形分层与依赖反转

系统 SHALL 采用 Engine / SDK / Domain Pack 三层架构，依赖方向永远向内（Engine 只依赖 SDK 抽象，绝不 import 具体 pack）。换领域时 Engine 代码一行不改。

**架构试金石**: `engine/` 目录下 `grep -rE "form|formCode|template|field"` 无结果。任何领域词汇出现在 engine 里即视为抽象泄漏。

#### Scenario: 换领域零改动
- **WHEN** 新增一个 `domains/bi_report/` pack（报表领域），注册到 ToolRegistry
- **THEN** `engine/` 目录下所有文件无需修改即可调度新 pack 的工具

#### Scenario: 架构试金石验证
- **WHEN** 运行 `grep -rE "form|formCode|template|field" engine/`
- **THEN** 输出为空（零领域知识泄漏）

#### Scenario: DummyPack 端到端
- **WHEN** 注册一个仅返回固定制品的 `DummyTool` + `DummyPack`
- **THEN** 能跑通"选工具→执行→发 SSE→存历史"完整链路，证明 Engine 不绑定 njmind

### Requirement: ToolDispatcher 单轮多工具选择

Engine SHALL 通过单次 LLM 调用选择 1..N 个工具（不进入多步循环），按 `is_concurrency_safe` 声明分批执行：连续 safe 的工具并发，unsafe 的串行。

#### Scenario: 单工具执行
- **WHEN** 用户输入"创建请假表"
- **THEN** LLM 返回 `["create_form"]` 一个工具，串行执行

#### Scenario: 多工具并发
- **WHEN** 用户输入"加字段A 和 删字段B"，两个 modify 操作都声明 `is_concurrency_safe=False`
- **THEN** 两个工具串行执行（顺序保持）

#### Scenario: 混合批次分批
- **WHEN** LLM 返回 `[A(safe), B(safe), C(unsafe), D(safe)]`
- **THEN** 分批为 `[[A,B]并发, [C]串行, [D]串行]`

#### Scenario: 并发批次 context 延迟 apply
- **WHEN** 一批 concurrency_safe 工具并发执行
- **THEN** 各工具对 context 的修改延迟到批次全部完成后统一 apply，避免竞态覆盖

### Requirement: 工具执行拦截层（Fail-Closed）

每个工具 SHALL 在 `execute` 前经过 `validate_input` 语义校验。校验失败时跳过 execute，错误写进 `ToolResult.error_for_llm` 回流给下一轮 LLM 选择。

Tool 协议的安全属性 SHALL 默认保守（Fail-Closed）：`is_destructive=True`、`is_read_only=False`、`is_concurrency_safe=False`，需 pack 显式声明才认为安全。

#### Scenario: 校验失败回流
- **WHEN** 工具的 `validate_input` 返回错误文本
- **THEN** 跳过 execute，`ToolResult.error_for_llm` 包含错误，下一轮 LLM 能感知"上次失败了、为什么"

#### Scenario: Fail-Closed 默认值
- **WHEN** 新建一个 Tool 子类未声明 `is_concurrency_safe`
- **THEN** 默认值为 `False`（串行执行，保守）

### Requirement: 失败回流与错误标准化

工具执行抛异常时，Engine SHALL 捕获并包装成 `ToolResult.error_for_llm`（标准化错误文本），让下一轮 LLM 选择能感知失败原因。对标 Claude Code 的错误回流。

#### Scenario: 异常包装
- **WHEN** `tool.execute()` 抛出任意 Exception
- **THEN** Engine 捕获，`ToolResult.error_for_llm=str(e)`，`summary="工具执行失败: ..."`，不向上抛

