"""njmind_form pack 内部的高频 JSON key 常量 —— 插件私有契约的唯一定义处。

这些 key 是 njmind 表单配置的结构约定（pack 私有，engine 不感知）。
集中定义后拼错在 import 处报错，而不是运行时静默 None / 错改字段。
"""

# 表单配置顶层
FIELDS = "formFieldConfigVos"             # 字段列表
FORM_CODE = "formCode"                    # 表单编码（数据库标识，禁改）
FORM_CONFIG_ID = "formConfigId"           # 表单数据 ID（已保存表单的标识，禁改）
FORM_NAME = "formName"                    # 表单名

# 字段对象
FIELD_KEY = "fieldTitleKey"               # 字段标识（数据库标识，禁改）
FIELD_TITLE = "fieldTitleText"            # 字段中文名
FIELD_TYPE = "formFieldType"              # 字段类型码
FIELD_REQUIRED = "isRequiredField"        # 必填
OPTION_SETTINGS = "optionSettings"        # 选项配置（SELECT/MULTI）
