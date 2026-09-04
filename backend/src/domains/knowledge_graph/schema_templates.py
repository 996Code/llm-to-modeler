"""知识库本体(类型体系)模板 —— 建库时选一个起步,之后可完全自定义。

【结构约定】(与 kg_knowledge_bases.schema_json 同构)
  schema_mode: strict(白名单外的类型丢弃并计数) / semi_open(进待审提案)
  entity_types:  [{key, label, description, examples, color}]
  relation_types: [{key, label, description, domain, range, color?}]
    - domain/range = 允许的源/目标实体类型 key 列表(空列表 = 不限制),
      抽取 prompt 注入该约束提升质量
  pending_types:  待审核提案(semi_open 模式 LLM 提的新类型,审批后并入)

【color】给前端图谱可视化用;未提供时前端按类型序取默认调色板。
"""

# 调色板(与前端 ECharts 默认分类色一致,模板里按序取用)
_PALETTE = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
    "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc",
]


def _entity(key, label, description, examples=None):
    return {
        "key": key, "label": label, "description": description,
        "examples": examples or [],
    }


def _relation(key, label, description, domain=None, range_=None):
    return {
        "key": key, "label": label, "description": description,
        "domain": domain or [], "range": range_ or [],
    }


def _build(entity_types, relation_types, schema_mode="semi_open"):
    for i, et in enumerate(entity_types):
        et.setdefault("color", _PALETTE[i % len(_PALETTE)])
    return {
        "schema_mode": schema_mode,
        "entity_types": entity_types,
        "relation_types": relation_types,
        "pending_types": [],
    }


# ── 内置模板(建库下拉可选) ─────────────────────────────────

GENERAL = _build(
    entity_types=[
        _entity("person", "人物", "真实或虚构的人物、角色",
                ["张三", "项目经理", "CEO"]),
        _entity("organization", "组织", "公司、部门、团队、机构",
                ["研发部", "某某科技有限公司"]),
        _entity("location", "地点", "地理位置、场所",
                ["北京", "总部大楼"]),
        _entity("product", "产品", "产品、系统、工具、服务",
                ["知识图谱插件", "低代码平台"]),
        _entity("event", "事件", "发生的事情、活动、变更",
                ["上线发布", "季度评审"]),
        _entity("concept", "概念", "技术概念、术语、规则、指标",
                ["实体消歧", "上下文窗口"]),
        _entity("document", "文档", "制度、手册、规范类文档本身",
                ["员工手册", "接口规范"]),
    ],
    relation_types=[
        _relation("任职于", "任职于", "在某组织担任职务", ["person"], ["organization"]),
        _relation("属于", "属于", "归属/从属关系", [], []),
        _relation("位于", "位于", "地理位置关系", [], ["location"]),
        _relation("包含", "包含", "整体-部分/组成关系", [], []),
        _relation("参与", "参与", "参与某事件/活动", ["person", "organization"], ["event"]),
        _relation("使用", "使用", "使用某产品/工具", [], ["product"]),
        _relation("相关", "相关", "其他语义关联(兜底)", [], []),
    ],
)

ORG_PEOPLE = _build(
    entity_types=[
        _entity("person", "人物", "人员、联系人", ["张三"]),
        _entity("department", "部门", "内部组织单元", ["研发部"]),
        _entity("company", "公司", "法人主体", ["某某集团"]),
        _entity("role", "职务", "岗位、头衔", ["架构师"]),
        _entity("project", "项目", "在研项目/项目群", ["中台建设"]),
    ],
    relation_types=[
        _relation("任职于", "任职于", "在部门/公司担任职务", ["person"], ["department", "company"]),
        _relation("汇报给", "汇报给", "汇报线关系", ["person"], ["person"]),
        _relation("隶属于", "隶属于", "下级组织归属上级", ["department"], ["department", "company"]),
        _relation("负责", "负责", "负责某项目", ["person", "role"], ["project"]),
        _relation("参与", "参与", "参与某项目", ["person"], ["project"]),
    ],
)

PRODUCT_DOC = _build(
    entity_types=[
        _entity("module", "模块", "产品功能模块/子系统", ["表单引擎"]),
        _entity("api", "接口", "API 端点/服务", ["/api/mcp/forms/create"]),
        _entity("config", "配置项", "参数、开关、环境变量", ["LLM_TIMEOUT"]),
        _entity("component", "组件", "技术组件、依赖库", ["LangGraph"]),
        _entity("flow", "流程", "业务流程/数据流", ["导入流水线"]),
        _entity("constraint", "约束", "限制、注意事项、前置条件", ["必填项校验"]),
    ],
    relation_types=[
        _relation("依赖", "依赖", "实现/运行依赖关系", [], []),
        _relation("属于", "属于", "归属某模块", [], ["module"]),
        _relation("调用", "调用", "调用某接口", [], ["api"]),
        _relation("配置于", "配置于", "配置作用目标", ["config"], []),
        _relation("作用于", "作用于", "作用于某流程/组件", ["constraint"], []),
    ],
)

REGULATION = _build(
    entity_types=[
        _entity("clause", "条款", "制度条款/规则条目", ["第 3.2 条"]),
        _entity("role", "角色", "适用对象、责任方", ["直属上级", "HR"]),
        _entity("behavior", "行为", "要求/禁止的行为", ["请假需提前申请"]),
        _entity("condition", "条件", "触发条件、时限、额度", ["3 天以上需审批"]),
        _entity("consequence", "后果", "违规后果、处理措施", ["记入绩效"]),
    ],
    relation_types=[
        _relation("适用对象", "适用对象", "条款适用的角色", ["clause"], ["role"]),
        _relation("要求", "要求", "条款要求的行为", ["clause"], ["behavior"]),
        _relation("触发条件", "触发条件", "行为生效条件", ["behavior"], ["condition"]),
        _relation("导致", "导致", "违规的后果", ["behavior"], ["consequence"]),
    ],
)

TEMPLATES = {
    "general": {"title": "通用", "description": "人物/组织/地点/产品/事件/概念", "schema": GENERAL},
    "org_people": {"title": "组织人事", "description": "人员/部门/汇报线/项目", "schema": ORG_PEOPLE},
    "product_doc": {"title": "产品文档", "description": "模块/接口/配置/流程/约束", "schema": PRODUCT_DOC},
    "regulation": {"title": "规章制度", "description": "条款/角色/行为/条件/后果", "schema": REGULATION},
}


def list_templates() -> list:
    """模板清单(建库下拉用):[{key, title, description, entityCount, relationCount}]。"""
    return [
        {
            "key": key,
            "title": t["title"],
            "description": t["description"],
            "entityCount": len(t["schema"]["entity_types"]),
            "relationCount": len(t["schema"]["relation_types"]),
        }
        for key, t in TEMPLATES.items()
    ]


def get_template_schema(key: str):
    """取模板的本体结构(深拷贝,建库后独立演化互不影响);未知 key 返回通用。"""
    import copy
    t = TEMPLATES.get(key) or TEMPLATES["general"]
    return copy.deepcopy(t["schema"])
