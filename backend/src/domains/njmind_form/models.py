"""njmind_form 领域模型。

把 njmind 表单 schema 收口到 pack 内,Engine 从不访问这些字段名。
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field


class ParsedField(BaseModel):
    """从用户自然语言解析出的字段。"""
    fieldTitleText: str = ""        # 中文名, e.g. "姓名"
    fieldTitleKey: str = ""         # 拼音蛇形, e.g. "xingming"
    formFieldType: int = 0          # type code, e.g. 0=TEXT
    fieldTypeName: str = "TEXT"     # type name, e.g. "TEXT"
    description: str = ""           # extra description from user
    options: Optional[List[str]] = None  # for SELECT types


class FormConfig(BaseModel):
    """njmind 表单配置。

    这是上游 njmind-modeler 的 schema,字段名不能改。
    Engine 从不访问这些字段,只在 pack 内部使用。
    """
    model_config = ConfigDict(extra="allow")  # 允许其他字段(模板可能带更多)

    formCode: str = ""
    formName: str = ""
    formTitle: str = ""
    titleFieldKey: str = ""
    formFieldConfigVos: List[Dict[str, Any]] = Field(default_factory=list)

    # 系统字段(保留模板中的默认值)
    topButtons: List[Dict[str, Any]] = Field(default_factory=list)
    bottomButtons: List[Dict[str, Any]] = Field(default_factory=list)
    isShowFieldAdd: bool = True
    isShowFieldDetail: bool = True
    isEditField: bool = True
