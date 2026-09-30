"""njmind_list pack 内部高频 JSON key 常量 —— 插件私有契约唯一定义处。

ListConfigVo 保存形态 JSON 的结构约定（pack 私有，engine 不感知）。
集中定义后拼错在 import 处报错，而不是运行时静默 None。
"""

# 宿主下发的会话键（tool_state 顶层）
CONFIG = "config"        # 宿主下发的 ListConfigVo 保存形态
FIELDS = "fields"        # 宿主下发的数据源字段目录（SQL 白名单）

# 列表配置顶层（config 内）
LIST_CODE = "listCode"
LIST_NAME = "listName"
SERVER_KEY = "serverKey"                # 服务源（数据源二元组之一，确认门点选回填）
TABLE_CONFIG = "tableConfig"
TABLE_SHOW_FIELDS = "tableShowFields"    # 列定义数组
ROW_BUTTONS = "rowButtons"              # 行按钮数组
BUTTON_GROUP_CONFIG = "buttonGroupConfig"  # 顶部按钮组
QUERY_PAGE = "queryPage"                # 分页 {defPageSize, pageSizes}
VALIDATION = "validation"               # 校验结果 {pass, errors, warnings}

# 身份域 13 字段（宿主注入，LLM 产物一律剥离——F1）
IDENTITY_FIELDS = ("listConfigId", "listCode", "listName", "listType",
                   "serverKey", "tableCode", "partTableCode", "mainTableField",
                   "mobileListCode", "dataVersion", "listState",
                   "processDefinitionKey", "listDirConfig")
