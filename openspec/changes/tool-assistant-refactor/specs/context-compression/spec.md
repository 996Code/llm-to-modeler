## ADDED Requirements

### Requirement: 压缩三级保护

压缩系统 SHALL 提供三级保护，应对各种边界情况：

1. **70% 阈值触发**: token 超过有效窗口（总窗口 - 预留 Summary Token 20K）的 70% 时触发压缩
2. **熔断器**: 连续 3 次压缩失败 → 120s 内不再尝试（避免徒劳重试浪费 API）
3. **PTL 防御**: 摘要本身超限（Prompt Too Long）→ 剥掉 20% 旧分组重试，上限 3 次，超限保留原始未压缩版本

有效窗口 = 模型上下文上限 - `MAX_OUTPUT_TOKENS_FOR_SUMMARY`（默认 20K），为压缩 API 自身预留输出空间。

#### Scenario: 70% 阈值触发
- **WHEN** 对话历史 token 数 > 有效窗口的 70%
- **THEN** 触发压缩，旧历史调 LLM 摘要，保留最近 N 轮

#### Scenario: 熔断器触发
- **WHEN** 连续 3 次压缩失败（LLM 异常/超时）
- **THEN** 熔断器跳闸，120s 内不再尝试压缩

#### Scenario: 熔断器半开恢复
- **WHEN** 熔断后经过 120s 冷却期
- **THEN** 半开状态，允许一次尝试；成功则重置，失败则继续熔断

#### Scenario: PTL 剥洋葱
- **WHEN** 压缩 API 报 Prompt Too Long
- **THEN** 剥掉 20% 最旧的分组重试，最多 3 次；超限则停止并保留原始未压缩版本（有损但不锁死）

### Requirement: 压缩 forked sidechain 隔离（C.2-D）

压缩 SHALL 在独立的 forked 线程执行，主对话流不等待压缩完成。主对话流先返回 keep-recent 历史，压缩结果异步写回。

压缩线程有独立超时、独立重试策略，失败由三级保护兜底，**不影响用户当前请求**。

每次压缩 SHALL 写 `compact_trace` 条目，记录：压缩前 token 数、压缩后 token 数、摘要内容、是否触发降级、触发的保护级别。供阈值调优和审计。

#### Scenario: 压缩不阻塞主对话
- **WHEN** 触发压缩时用户正在等待响应
- **THEN** 主对话流先返回 keep-recent 历史，压缩在后台线程执行，用户无需等待

#### Scenario: 压缩轨迹记录
- **WHEN** 一次压缩完成（无论成功或降级）
- **THEN** `compact_trace` 条目写入 events 表，含 token 前后数、摘要、降级标记

### Requirement: 压缩状态重启补偿

压缩完成后，系统 SHALL 重新注入关键状态，确保 LLM 不丢失"在做什么"和"有什么工具"：

1. **制品状态补偿**: 调 `tool.summarize_artifact(artifact)` 拿当前制品摘要（如"当前表单: 请假申请表，字段: 申请人、请假类型..."）
2. **工具能力复灌**: 压缩后重建 tool schema 注入下一轮，否则 LLM 忘记自己有什么工具

压缩 prompt 模板由 pack 提供（因为要提"表单"还是"报表"）。

#### Scenario: 制品状态补偿
- **WHEN** 压缩完成，当前有 formConfig 制品
- **THEN** 下一轮 prompt 含 pack 提供的制品摘要，LLM 知道"当前在做什么表单、有哪些字段"

#### Scenario: 工具能力复灌
- **WHEN** 压缩后历史变短
- **THEN** 下一轮 LLM 仍能看到完整工具清单（tool schema 重建注入），不会忘记可用工具

### Requirement: 机制与内容分离

压缩系统 SHALL 遵循"机制归 Engine、内容归 pack"的分离原则：
- **机制归 Engine**：阈值判断、keep-recent、熔断器、PTL、调 LLM 摘要、动态上下文注入——这些逻辑 MUST 在 `engine/` 内，不依赖任何领域
- **内容归 pack**：`summarize_artifact` 返回的状态补偿文本、压缩 prompt 模板 MUST 由 pack 提供

Engine 的压缩机制代码 MUST 不引用任何领域字段名（如 formCode/formFieldConfigVos）。

#### Scenario: 换领域压缩可复用
- **WHEN** 新增 `domains/bi_report/` pack
- **THEN** Engine 的压缩机制代码无需改动，pack 提供 `summarize_artifact` 和 compact.j2 即可
