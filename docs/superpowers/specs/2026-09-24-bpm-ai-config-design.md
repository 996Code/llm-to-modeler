# BPM 流程配置 AI 辅助设计规格

## 一、愿景与核心边界

在流程设计器编辑页中，配置人员可以像使用现有表单 AI 一样，用自然语言生成或修改流程图及流程规则。AI 只生成候选 JSON、渲染到当前画布供人工确认，绝不直接保存、发布或修改后端业务数据。

目标链路：

```text
流程设计器内存态
  → AI 读取当前上下文
  → 读取编译期生成的 BPM Schema/模板/guide
  → LLM 生成拓扑草稿 + 规则草稿
  → Java/Flowable 确定性转换与权威校验
  → artifact 返回设计器
  → 用户 APPLY 到画布
  → 用户确认后点击原有保存按钮
```

明确边界：

- AI 链路不入库、不发布、不调用 `/bpm/model/update`。
- designer 的 APPLY 只更新内存画布状态，`isSaved=false`；落库只走设计器原有保存流程。
- 一期入口只做流程设计器编辑页，参照 `/mind-designer/field-edit` 的悬浮窗契约。
- 编辑页既可以是空白草稿，也可以是已有流程；AI 可在空白草稿中生成完整流程，也可修改已有流程，但不负责创建模型记录。
- LLM 不直接生成 BPMN XML、Flowable resourceId、坐标或自定义 XML 扩展。
- 一期不做独立 MCP 技能文件、流程克隆、流程截图识别、节点级独立弹框 AI。

## 二、调研存档

### 1. 表单 AI 参照链路

`mind-designer/src/views/WidgetConfig/Header/useAIAssistant.ts` 已经实现了目标交互：

- L15-L17：悬浮窗通过 INIT/GET_CONTEXT/APPLY/GET_AUTH 与 AI 子应用通信；APPLY 只渲染画布，落库仍由业务人员点击设计器保存按钮完成。
- L83-L109：`serializeVo()` 将当前画布状态清洗成纯 JSON，剥离运行时组件、前端字段、空值和图标。
- L247-L264：INIT 下发用户身份、鉴权头、服务地址、当前 artifact、revision 和 pack 列表。
- L268-L272：GET_CONTEXT 每次从宿主读取最新画布状态，而不是从后端读取上次保存状态。
- L276-L353：APPLY 做 revision 漂移检查，通过 `buildWidgets()` 回填画布，设置 `isSaved=false`，回传新 artifact 和 revision。

表单 AI 的资产和引擎链路：

```text
Form VO + @FieldTypeMapping
  → CompileTimeGenerator
  → mcp-schemas + mcp-templates + guide.json + SKILL.md
  → njmind-modeler MCP/REST 只读资产和校验端点
  → llm-to-modler domains/njmind_form
  → artifact
  → field-edit 宿主 APPLY
  → 原有 designer 保存流程
```

`llm-to-modler-sqlite/backend/src/domains/njmind_form/config.yaml` 已验证以下模式：

- `paths` 集中声明上游模板、Schema、guide、校验和表单查询端点。
- `artifact` 声明 `view_json/apply/rewind` 等通用制品动作。
- `services.njmind-modeler` 由宿主按请求下发，不在 pack 内硬编码部署地址。

表单侧存在的 MCP create/update REST 能力属于其他消费方，不是本次参照的 field-edit 悬浮窗生成链路。BPM 一期不新增任何 AI 保存工具。

### 2. BPM designer 现状

`mind-designer/src/views/ProcessConfig/`：

- `BpmnView.tsx` 负责读取 XML、读取业务规则和保存流程。
- `modeler/Modeler.tsx` 使用 bpmn-js 8.9.0，并加载 Flowable moddle 扩展。
- `bpmn/store.ts` 管理 Modeler 实例、XML 导出和节点变更。
- `businessStore.ts` 保存审批规则、抄送规则和分支规则。
- `panel/` 和 `panel/components/` 提供审批人、抄送、分支、按钮、回退和加签配置。
- `src/api/model.ts` 调用 `/codeBack/bpm/model/get`、`/codeBack/bpm/model/update` 及规则查询接口。

流程编辑状态由两部分组成：

```text
bpmnXml + bpmModelRule
```

其中规则包含：

- `formKey`
- `approveRules`
- `ccRules`
- `branchRules`

`IApproveRule` 单节点约有 50 个配置项，包含审批人范围、或签/会签、按钮权限、回退、加签、自动审批、互斥节点和电子签名等。

### 3. BPM Java 后端现状

`njmind-modeler-bpm` 已使用 Flowable 6.8.0，并且当前代码已有 `BpmnXMLConverter` 使用：

- `njmind-modeler-bpm-config-biz/.../BpmConfigBpmnXml.java`
- `njmind-modeler-bpm-biz/.../BpmnModelUtils.java`

规则 VO 已存在于 `njmind-modeler-bpm-api`：

- `BpmModelRuleSaveReqVO`
- `BpmModelRuleBaseVO`
- `BpmTaskApproveRuleSaveReqVO`
- `BpmTaskApproveRuleBaseVO`
- `BpmCcNodeRuleVO`
- `BpmModelBranchRuleSaveReqVO`

`BpmConfigModelController` 已有草稿查询、创建、更新、发布、导入导出和任务节点查询接口。`BpmConfigModelRuleController` 提供草稿规则查询。

当前缺少：

- BPM 的 MCP Schema、模板、guide 资产。
- BPM 专用解析、渲染、校验工具。
- 流程编辑页 AI 悬浮窗。

## 三、已确认的设计决策

| 决策项 | 结论 |
|---|---|
| 产物 | 完整流程：拓扑 + 规则，最终由 Flowable 生成 BPMN XML |
| 入口 | 流程设计器编辑页悬浮窗，参照 `/mind-designer/field-edit` |
| 上下文 | INIT/GET_CONTEXT 从宿主读取当前内存态，包含未保存修改 |
| 生成格式 | LLM 生成规范化拓扑 JSON + 规则 JSON，不直接生成 XML |
| XML 内核 | 拓扑 → Flowable `BpmnModel` → 自动布局 → `BpmnXMLConverter` |
| 资产 | Java VO/注解作为 SSoT，maven 编译期生成 Schema、模板和 BPM guide |
| 校验 | Java 侧权威校验，渲染后校验 XML、拓扑、规则和表单引用 |
| 持久化 | AI 和 APPLY 都不入库；人工点击原有保存按钮才入库 |
| 一期范围 | 编辑页内空白草稿生成 + 已有流程修改；不创建模型记录 |
| 实施顺序 | M1 njmind-modeler → M2 llm-to-modler → M3 mind-designer |

## 四、总体架构

### 1. 与 form 的五环节对应

| form | BPM |
|---|---|
| Form VO + `@FieldTypeMapping` | 现有 BPM 规则 VO + 新增拓扑草稿 VO |
| `CompileTimeGenerator` 生成 Schema/模板/guide | 同一生成器新增 BPM Schema/模板/guide 生成器 |
| `McpToolExecutor` 提供资产读取和校验 | 同机制增加 BPM parse/render/资产工具 |
| LLM 输出最终 form JSON | LLM 输出拓扑草稿和 key 版规则草稿 |
| 无中间转换 | 增加确定性的 Flowable `BpmnModel` 渲染阶段 |
| artifact 回填 form 画布 | artifact 回填 XML 和 businessStore |
| designer 原有保存 | designer 原有 `/bpm/model/update` 保存 |

### 2. 两种 JSON 必须分层

不能直接拿后端持久化 VO 的 `taskId` 和 `sequenceFlowId` 存放语义 key。否则字段名会误导调用方，也容易把临时 key 当成真实 BPMN id。

定义两层契约：

```text
BpmProcessDraftVO        # AI 使用的中间 JSON
  topology
  ruleDraft

BpmModelUpdatePayload    # renderer 输出的最终 JSON
  bpmnXml
  approveRules[].taskId
  ccRules[].nodeId
  branchRules[].sequenceFlowId
```

AI 草稿示意：

```json
{
  "topology": {
    "nodes": [
      {"key": "start", "type": "START_EVENT", "name": "开始"},
      {"key": "manager", "type": "USER_TASK", "name": "部门经理审批"},
      {"key": "end", "type": "END_EVENT", "name": "结束"}
    ],
    "edges": [
      {"key": "e_start_manager", "from": "start", "to": "manager"},
      {"key": "e_manager_end", "from": "manager", "to": "end"}
    ]
  },
  "ruleDraft": {
    "formKey": "leave_form",
    "approveRules": [
      {"nodeKey": "manager", "approveOptRange": 20}
    ],
    "ccRules": [],
    "branchRules": [
      {
        "edgeKey": "e_manager_end",
        "logic": "AND",
        "rules": []
      }
    ]
  }
}
```

分支规则独立于 `edges`，通过 `edgeKey` 关联，而不是把业务条件塞进拓扑边对象。这样规则 Schema、规则模板和后端已有规则 VO 仍然是清晰的两个层次。

Renderer 负责：

1. 校验 nodeKey/edgeKey 引用。
2. 为新节点和新边分配 Flowable/BPMN id。
3. 把 `nodeKey` 转成 `taskId`/`nodeId`。
4. 把 `edgeKey` 转成 `sequenceFlowId`。
5. 将最终 id 写入 XML 和持久化规则 JSON。

修改已有流程时，Parser 把原 BPMN id 作为稳定 key 返回。未修改节点和连线保留原 id，新对象才分配新 id。

### 3. 新建草稿和修改草稿

两者都发生在编辑页，不代表 AI 创建数据库模型：

```text
空白/默认模型已由原有流程编辑入口准备好
  → AI 生成完整内容
  → APPLY 到内存画布
  → 人工保存
```

已有流程：

```text
宿主 GET_CONTEXT
  → bpmnXml + rules
  → bpm_parse 得 topology
  → LLM 生成最小变更 draft
  → bpm_render
  → artifact
  → APPLY 到内存画布
  → 人工保存
```

## 五、M1：njmind-modeler Java 侧设计

### 1. bpm-api 新增对象

新增：

- `BpmProcessTopologyVO`
- `BpmTopologyNodeVO`
- `BpmTopologyEdgeVO`
- `BpmProcessDraftVO`
- `BpmApproveRuleDraftVO`
- `BpmCcRuleDraftVO`
- `BpmBranchRuleDraftVO`

新增 `@BpmEnumMapping` 或等价 BPM 专用元数据注解，描述编码、中文含义、关键词、适用场景和依赖关系。Schema 和 guide 都从该元数据生成。

优先覆盖：

- 节点类型：开始、用户任务、抄送、排他网关、结束。
- 审批范围编码。
- 或签/会签方式。
- 分支逻辑和字段操作符。
- 回退、加签和空审批人策略。
- 表单人员字段的引用关系。

### 2. CompileTimeGenerator 扩展

`mvn compile` 的 `process-classes` 生成：

```text
mcp-schemas/
  bpm_process_draft.schema.json
  bpm_topology.schema.json
  bpm_approve_rule.schema.json
  bpm_cc_rule.schema.json
  bpm_branch_rule.schema.json

mcp-templates/
  bpm_process_generic.json
  bpm_process_leave.json
  bpm_process_expense.json
  bpm_topology_linear.json
  bpm_topology_branch.json
  bpm_topology_cc.json
  bpm_approve_rule_default.json
  bpm_approve_rule_multi.json
  bpm_approve_rule_form_field.json
  bpm_cc_rule_default.json
  bpm_branch_rule_field_value.json

mcp-guides/
  bpm_guide.json
```

生成器职责：

- `BpmSchemaGenerator`：从 BPM draft VO 和规则 VO 生成 JSON Schema。
- `BpmTemplateGenerator`：用 Java 对象树生成可复制修改的模板 JSON。
- `BpmGuideGenerator`：生成节点字典、枚举字典、模板索引、字段类型兼容矩阵、反模式。
- 现有 `CompileTimeGenerator`：负责调用上述生成器、写入 `src/main/resources` 和 `target/classes`、同步生成说明文件。

生成依赖必须遵守 Maven 模块构建顺序：`bpm-api` 先于 `mcp-generator`；不得形成 `bpm-api → mcp-generator → bpm-api` 循环依赖。

### 3. Flowable 渲染与解析

新增 `BpmTopologyRenderer`：

```text
BpmProcessDraftVO
  → 创建 Flowable BpmnModel
  → 创建 Process/StartEvent/UserTask/ServiceTask/ExclusiveGateway/EndEvent/SequenceFlow
  → 写入必要 Flowable 扩展属性
  → BpmnAutoLayout 或等价 Flowable 布局能力
  → BpmnXMLConverter
  → bpmnXml + 最终规则 JSON
```

Flowable JSON converter 可以作为调研对象，但不作为 LLM 直接契约。老式 Modeler JSON 包含 bounds、stencil、resourceId、outgoing 等画布实现细节，不符合 LLM 只处理语义 key 的原则。

新增 `BpmTopologyParser`：

```text
bpmnXml
  → BpmnXMLConverter.convertToBpmnModel
  → 遍历 Flowable BpmnModel
  → 识别 designer 支持的节点和连线
  → 生成 BpmProcessDraftVO
```

Parser 必须保留：

- 原 UserTask/抄送节点 id。
- 原 SequenceFlow id。
- 节点与规则之间的引用。
- 会签属性。
- designer 使用的分支表达式对应关系。

### 4. 权威校验

新增 `BpmModelValidator`，至少覆盖：

1. **拓扑结构**：开始/结束节点唯一、节点可达、无悬空节点、网关出口合理。
2. **Flowable XML**：渲染后重新解析，并复用 Flowable 可用的流程校验能力。
3. **引用一致性**：nodeKey/edgeKey 和最终 taskId/nodeId/sequenceFlowId 均存在。
4. **规则合法性**：枚举、必填字段、审批人范围依赖、会签配置、回退/互斥引用。
5. **表单引用**：formKey 存在，分支字段存在，表单人员字段类型正确，操作符与字段类型兼容。
6. **反模式告警**：网关单出口、无审批人策略、无意义分支、节点引用失效等。

错误分为阻断型 `errors` 和不阻断型 `warnings`。校验器不做“猜测修复”，最多返回明确的字段路径和允许值。

### 5. 纯计算工具

一期工具只做资产读取、解析、渲染和校验：

```text
bpm_get_guide
bpm_list_templates
bpm_get_template
bpm_list_schemas
bpm_get_schema
bpm_parse
bpm_render
```

`bpm_parse`：XML → draft topology，零副作用。

`bpm_render`：draft → XML + 最终规则 + validation，零副作用。

一期不提供：

- `bpm_create_model`
- `bpm_update_model`
- `bpm_deploy_model`
- 任何直接写 `bpm_config_model` 或规则表的工具。

REST 与 MCP 入口如需同时暴露，必须调用同一 Java service，不得分别实现两套逻辑。

### 6. renderer 输出协议

```json
{
  "bpmnXml": "<?xml version=...>",
  "rules": {
    "formKey": "leave_form",
    "approveRules": [],
    "ccRules": [],
    "branchRules": []
  },
  "validation": {
    "pass": true,
    "errors": [],
    "warnings": []
  }
}
```

`bpm_render` 不保存该结果，也不修改模型版本。

### 7. 必须用真实 designer XML 对齐的内容

实现前从真实模型导出 XML，逐项确认：

- 抄送节点在 designer 中的实际 BPMN 元素和标记属性。
- `Approve.tsx` 会签配置写入的 `MultiInstanceLoopCharacteristics` 精确属性。
- `Branch.tsx` 字段值分支生成的 `${bpmBranchRuleCalculate.calculate(execution,'<flowId>')}` 格式。
- `flowableDescriptor.json` 中允许的扩展属性和命名空间。
- bpmn-js `importXML` 后节点仍可选中、编辑、保存。

## 六、M2：llm-to-modler 引擎侧设计

新增：

```text
backend/src/domains/njmind_bpm/
  config.yaml
  pack.py
  models.py
  keys.py
  router.py
  upstream.py
  prompts/
    parse.j2
    generate.j2
    update_parse.j2
    chat.j2
    _sections/node_types.j2
    _sections/form_catalog.j2
  tools/
    chat.py
    generate_process.py
    update_process.py
```

### 1. pack 配置

`config.yaml` 声明：

- BPM 领域路由描述，不作为全局兜底域。
- `templates_list/template/schema/guide/parse/render/get_form` 路径。
- `artifact.type: bpm-process`。
- `actions: [view_json, apply, rewind]`。
- `services.njmind-modeler: {}`，地址由宿主下发。

`guide` 使用独立的 `bpm_guide.json`，避免 form pack 每次读取时携带无关 BPM 资产。

### 2. 生成流程

```text
宿主上下文(formKey + 当前 XML + 当前 rules)
  → fetch bpm_guide
  → fetch 绑定表单字段目录(只读)
  → parse 用户意图并形成待确认摘要
  → fetch 对应拓扑/规则模板
  → LLM 生成 BpmProcessDraftVO
  → bpm_render
  → errors 机械修复或带错误重生成
  → artifact
```

规则：

- LLM 只接收压缩后的表单字段目录，不把整份表单 JSON 无限制塞入 prompt。
- LLM 不生成 XML、坐标、resourceId、UEL 字符串。
- 一期分支优先支持字段值模式；表达式模式不由 AI 自动编写。
- 未知节点类型、未知规则属性、未知模板均 fail-closed，转为追问或失败，不回退到猜测值。

### 3. 修改流程

```text
GET_CONTEXT 取得宿主当前内存态
  → bpm_parse(current.bpmnXml)
  → 合并当前 rules
  → update_parse.j2 生成变更意图摘要
  → 用户确认
  → 生成最小变更 draft
  → bpm_render
  → artifact
```

未被变更意图涉及的节点和规则原样保留。新增节点使用新 key；原 key 对应原 BPMN id。

### 4. artifact

artifact 至少包含：

```json
{
  "formKey": "leave_form",
  "bpmnXml": "...",
  "rules": {
    "approveRules": [],
    "ccRules": [],
    "branchRules": []
  },
  "validation": {
    "warnings": []
  }
}
```

artifact 的 APPLY 由宿主执行，artifact 本身不拥有后端保存能力。

## 七、M3：mind-designer 流程编辑页

新增 `src/views/ProcessConfig/useAIAssistant.ts`，实现 field-edit 同款协议：

| field-edit | ProcessConfig |
|---|---|
| `userId: tenant:uid:formCode` | `tenant:uid:modelKey` |
| `packs: ['njmind_form']` | `packs: ['njmind_bpm']` |
| 当前表单纯 JSON | `{formKey, bpmnXml, rules}` |
| `buildWidgets()` | `BpmnStore.importXML()` + businessStore 规则回填 |
| `isSaved=false` | 草稿标记为未保存 |
| form revision | `hash(formKey + bpmnXml + stableRulesJson)` |
| 设计器保存按钮 | 现有流程保存按钮 |

### APPLY 行为

1. 对比 artifact 的 `baseRevision` 和当前画布 revision。
2. 不一致时提示用户确认是否覆盖未保存修改。
3. 先导入 XML，再回填业务规则；任一步失败则不更新成功状态。
4. 回传当前画布重新导出的 XML、规则和新 revision。
5. 提示“已应用到画布，请确认后手动保存”。

不修改：

- `/bpm/model/update` 原有保存接口。
- dataVersion 乐观锁逻辑。
- 流程发布逻辑。
- 现有表单 field-edit AI 链路。

artifact 预览一期沿用通用 `view_json` 和摘要，不新增独立图形预览器；用户 APPLY 后直接在现有 bpmn-js 画布中检查图形，避免重复实现第二个流程渲染器。

## 八、与 form 的真实差异

1. BPM LLM 产物不是最终 XML，必须增加确定性 Flowable 渲染层。
2. BPM 修改需要 XML → topology 的反向 Parser，form 修改不需要反向编译。
3. BPM 有拓扑节点/连线 id 稳定性，form 主要是字段 key 稳定性。
4. BPM 规则引用外部表单字段，需要 formKey、字段目录和跨模块校验。
5. BPM 图形组合数量远大于 form 字段，模板采用整流程模板 + 拓扑片段 + 原子规则模板，不能声称“几个模板覆盖所有流程”。
6. BPM XML 必须同时兼容 Flowable 引擎和 bpmn-js designer；form JSON 没有同等的双端解析约束。
7. BPM 一期只支持编辑页悬浮窗，AI 不创建模型、不更新模型、不发布模型；form 的其他 MCP/REST 消费路径不复制到一期。

## 九、错误处理与安全约束

### 1. 失败处理

- LLM JSON 解析失败：沿用现有 `chat_json` 重试。
- Schema 不合规：先机械剥离未知键并反馈字段路径，不静默丢弃业务字段。
- render 校验失败：最多进行有限轮机械修复；仍失败则返回错误，禁止 APPLY 半成品。
- `importXML` 失败：宿主返回 `RENDER_FAILED`，不改变原画布。
- revision 漂移：用户取消则不应用，确认覆盖也只修改内存。
- 上游不可用：fail-closed，不能用模型猜测规则或模板。

### 2. 安全和资源限制

- XML 解析关闭外部实体、外部 DTD 和外部资源访问，避免 XXE。
- 限制 XML、draft JSON、节点数量、边数量和单次 prompt 大小。
- Renderer 只接受白名单节点类型和白名单扩展属性，不允许 LLM 注入任意 XML。
- 分支条件一期只接受结构化字段操作，不接受任意 UEL/脚本。
- parse/render 端点继续使用现有鉴权、租户和企业上下文；即使不写库，表单字段查询仍必须在正确租户下执行。
- 失败日志记录阶段、输入摘要、错误和耗时；模型原始输入输出继续遵循现有 call_logs 脱敏规则。

## 十、测试策略

### 1. Java

- Schema/模板/guide 生成快照测试。
- `BpmTopologyRenderer` 单测：节点、边、网关、会签、抄送、分支条件和 id 映射。
- `BpmTopologyParser` 单测：真实 XML 解析、原 id 保留、未知节点处理。
- Parser → Renderer golden round-trip：从 `/bpm/model/export` 获取线性、分支、会签、抄送等真实 XML，断言语义等价、原 id 保持、所有 DI 完整。
- Flowable XML 重新解析和流程校验。
- `BpmModelValidator` 六个维度的正反例。
- 自定义扩展 round-trip：确认 bpmn-js importXML 后仍可编辑和保存。

语义等价不做 XML 字节串相等；比较节点、边、类型、名称、扩展属性、规则引用和可用性。

### 2. Python

- upstream 七个端点的 mock 测试。
- `generate_process`/`update_process` 管线测试：mock LLM、mock parse/render，断言顺序、重试和 artifact。
- 表单字段目录蒸馏测试，确认 prompt 不携带无关字段和运行时字段。
- checkpoint 恢复测试，确认 topology draft 可序列化。
- 全量 pytest，必须包含现有 `njmind_form` 回归。

### 3. designer

- `useAIAssistant` 协议测试：INIT、GET_CONTEXT、APPLY、GET_AUTH、RESIZE、CLOSE。
- APPLY 漂移检测、取消覆盖、XML 导入失败测试。
- 浏览器实测：空白草稿生成完整请假流；已有流程修改节点和分支；两条链路都只更新画布不入库。
- form field-edit 悬浮窗回归。

## 十一、阶段与出口标准

### M1：Java 资产和纯计算能力

内容：

- BPM draft VO、Schema、模板、guide。
- Flowable Parser/Renderer/Validator。
- `bpm_parse`、`bpm_render` 及资产读取工具。
- 真实 designer XML golden 集。

出口：

- maven 编译成功并生成完整资源。
- golden round-trip 全绿。
- Flowable 可解析，bpmn-js 可导入的真实样本验证通过。
- 无任何保存、发布或数据库写入调用。

### M2：llm-to-modler BPM pack

内容：

- `domains/njmind_bpm` 配置、pack、prompt、工具和 artifact。
- 空白草稿生成和已有流程修改两条管线。

出口：

- mock 全链路 artifact 生成成功。
- errors/warnings、重试、fail-closed 行为有测试。
- 全量 pytest 通过，form pack 无回归。

### M3：designer 编辑页悬浮窗

内容：

- ProcessConfig Header 入口。
- field-edit 同款宿主通信和 APPLY 回填。

出口：

- 浏览器完成生成、修改、取消覆盖、手动保存验证。
- AI 链路没有数据库写入。
- 原有流程保存、发布和表单 field-edit 不受影响。

## 十二、防跑偏清单

实现和评审时逐项检查：

- [ ] AI 不调用 `/bpm/model/update`、deploy 或任何持久化接口。
- [ ] designer APPLY 只更新内存，不把 `isSaved` 标成已保存。
- [ ] 只有人工点击原有保存按钮才能入库。
- [ ] 当前上下文来自宿主 GET_CONTEXT，不从后端已保存版本覆盖未保存修改。
- [ ] LLM 只生成 key、节点语义和结构化规则，不生成 XML、坐标、Flowable id 或任意 UEL。
- [ ] AI draft 的 `nodeKey/edgeKey` 不冒充最终 `taskId/sequenceFlowId`。
- [ ] Schema、模板、guide 都由 maven 编译期生成，不能在 Python prompt 中复制一套隐式字典。
- [ ] Renderer/Parser 是唯一的 Flowable XML 转换逻辑，前端不再实现第二套 XML 生成器。
- [ ] 真实 designer XML、抄送节点、会签属性、分支 UEL 都有 golden 样本验证。
- [ ] 新增 BPM pack 不改变 njmind_form 的路由、artifact 和悬浮窗行为。
- [ ] 一期不扩展 MCP 技能、clone、image、节点级弹框或自动发布。
- [ ] 所有模型问题优先检查 call_logs 的实际 prompt 和上游响应，不先归因于模型能力。

## 十三、实现期需要先验证的开放项

1. Flowable 6.8.0 当前依赖树中是否已包含可用的自动布局模块；若没有，确认兼容版本后再增加 `flowable-bpmn-layout`，不能凭版本号假定 API 存在。
2. Flowable `BpmnModel` 对 designer 自定义抄送节点和 Flowable 扩展属性的保留能力；必要时用 extension elements/attributes 显式补写。
3. `BpmModelUpdateReqVO` 的最终请求结构与 designer `businessStore` 导出结构是否完全一致，必要时增加单独的映射 DTO，不污染现有保存契约。
4. `BpmnAutoLayout` 产生的 DI 是否满足 bpmn-js 8.9.0 的编辑要求；布局测试只要求稳定可编辑，不要求与人工布局坐标一致。
5. `bpm_parse` 和 `bpm_render` 的 API 前缀、鉴权和租户上下文要与当前 MCP/REST 网关实际路由统一。
6. field-edit 宿主的 artifact 协议是否支持新增 `bpm-process` 的 payload；如通用层只按 `form-config` 写死，需要先做声明驱动扩展，不复制一套前端协议。
7. artifact 的 JSON 预览需要确认是否能安全展示 XML 大字段；一期可只展示规则和拓扑摘要，XML 通过 APPLY 回画布检查。

## 十四、涉及仓库

- `njmind-modeler`：M1，BPM API、编译期资产、Flowable 解析/渲染/校验、纯计算工具。
- `llm-to-modler-sqlite`：M2，`domains/njmind_bpm` 引擎插件、prompt、artifact 和测试。
- `mind-designer`：M3，ProcessConfig 编辑页悬浮窗、宿主上下文和 APPLY 回填。

本设计只描述 AI 生成与编辑草稿链路，不改变任何既有流程保存、发布、运行时审批或表单配置消费逻辑。
