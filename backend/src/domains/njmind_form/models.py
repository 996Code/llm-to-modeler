"""njmind_form 领域模型(数据传输对象 / DTO)。

【模块定位】
定义本 pack 内部使用的结构化数据模型。把 njmind 表单的 schema 收口在 pack 内,
Engine 层从不直接接触这些字段名 —— Engine 只通过 ToolResult 的抽象接口
(artifact / summary 等)与 pack 交互。这是"分层隔离"的架构试金石。

【为什么用 Pydantic 而不是普通 dataclass】
Pydantic 在赋值时自动做类型校验和 JSON 序列化/反序列化,且能与 JSON Schema
互转 —— 对接 LLM(输入输出都是 JSON)非常方便。

【Java 类比】
对标 Java 的 POJO / DTO + Jackson + Bean Validation:
- pydantic.BaseModel  ≈  带 @JsonProperty 注解的 POJO + 校验注解。
- Field(default_factory=list)  ≈  Jackson 的默认值 + Lombok @Builder 默认。
- ConfigDict(extra="allow")  ≈  Jackson 的 @JsonInclude / FAIL_ON_UNKNOWN_PROPERTIES
  反向开关:允许 JSON 里多出未定义字段(透传保留)。
- 字段名用 camelCase 而非 Python 惯例的 snake_case,是因为这些字段名要
  原样对齐上游 njmind-modeler 的 JSON schema(序列化时不能改名)。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ParsedField(BaseModel):
    """从用户自然语言解析出的单个字段(表单的一列)。

    LLM 把用户的口语描述(如"加个姓名输入框")结构化成 ParsedField,
    再由后续步骤映射成上游 schema 里的字段配置。

    【Java 类比】
    一个标准的 DTO 类,带默认值(类似 Lombok @Builder.Default)。
    """
    fieldTitleText: str = ""        # 中文名, e.g. "姓名" —— 前端展示用
    fieldTitleKey: str = ""         # 拼音蛇形 key, e.g. "xingming" —— 数据库/接口字段名
    formFieldType: int = 0          # type code(整数), e.g. 0 表示 TEXT 文本框
    fieldTypeName: str = "TEXT"     # type 名称(字符串), e.g. "TEXT" —— 便于日志和调试
    description: str = ""           # 用户补充的字段描述,供 LLM/前端理解字段用途
    # Optional + None 默认值:仅 SELECT(下拉)等枚举类型才填选项。
    # 等价 Java 的 List<String> options = null; —— 显式 Optional 表达"可能没有"。
    options: Optional[List[str]] = None  # for SELECT types


class FormConfig(BaseModel):
    """njmind 表单配置 —— 对应上游 njmind-modeler 的核心 schema。

    【重要约束】
    这是上游系统的 schema,字段名(formCode / formName / formFieldConfigVos 等)
    是与上游接口契约绑定的,不能随意改名(改名会导致序列化出的 JSON 不被上游接受)。
    Engine 层从不访问这些字段,只在 pack 内部使用。

    【extra="allow" 的意义】
    上游模板可能携带本模型未列出的额外字段(如不同表单类型的特有配置)。
    ConfigDict(extra="allow") 让 Pydantic 接受并保留这些未知字段,
    序列化时原样回传 —— 等价 Jackson 的反序列化时不丢未知字段。

    【Java 类比】
    一个用 @JsonAlias / @JsonIgnoreProperties(ignoreUnknown=false) 配置的 POJO,
    允许 JSON 携带比类定义更多的字段并保留透传。
    """
    model_config = ConfigDict(extra="allow")  # 允许其他字段(模板可能带更多)

    # ── 业务标识与标题字段 ──
    formCode: str = ""        # 表单唯一编码(上游主键)
    formName: str = ""        # 表单内部名(英文标识)
    formTitle: str = ""       # 表单展示标题(给用户看的)
    titleFieldKey: str = ""   # 用作列表"标题列"的字段 key(如 "xingming")
    # 表单字段列表:每个元素是一个字段配置 dict。用 Field(default_factory=list)
    # 而不是直接 = [] —— Python 中可变默认值若用字面量会被所有实例共享(经典坑),
    # default_factory 保证每个实例拿到独立的新 list。等价 Java 的 new ArrayList<>()。
    formFieldConfigVos: List[Dict[str, Any]] = Field(default_factory=list)

    # ── 系统字段(保留模板中的默认值) ──
    # 这些是上游表单渲染相关的开关,通常从模板继承默认值,pack 不主动改写。
    topButtons: List[Dict[str, Any]] = Field(default_factory=list)     # 顶部按钮配置
    bottomButtons: List[Dict[str, Any]] = Field(default_factory=list)  # 底部按钮配置
    isShowFieldAdd: bool = True     # 是否显示"新增字段"按钮
    isShowFieldDetail: bool = True  # 是否显示"字段详情"
    isEditField: bool = True        # 字段是否可编辑
