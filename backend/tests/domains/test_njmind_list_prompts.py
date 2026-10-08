"""njmind_list prompts 模板渲染测试（parse/generate/_sections/field_catalog）。

契约对应 task-B4：parse 输出意图 JSON 契约、generate 输出 ListConfigVo 保存形态，
硬约束（身份域禁写/0|1 整数/sortType 数字域/脚本禁写/text 兜底）必须落进提示词。
guide/模板变量全部来自 A3 端点真实响应形态。
"""
import pytest
from pathlib import Path

from sdk.prompt_loader import PromptLoader


@pytest.fixture
def loader():
    """PromptLoader 指向 domains 根（pack.py 同款装配）。"""
    return PromptLoader(packs_root=Path(__file__).resolve().parents[2] / "src" / "domains")


MINIMAL_GUIDE = {
    "fieldCatalog": [
        {"fieldKey": "device_name", "fieldTitle": "设备名称", "fieldType": 0, "source": "base"},
        {"fieldKey": "device_model", "fieldTitle": "设备型号", "fieldType": 0, "source": "base"},
        {"fieldKey": "device_status", "fieldTitle": "设备状态", "fieldType": 4, "source": "part"},
    ],
    "enums": {
        "align": ["left", "center", "right"],
        "listType": ["list", "dialogList", "form", "cardList",
                     "mobileCardList", "mobileCardDialogList"],
        "buttonType": ["primary", "default", "transparent", "text", "icon"],
        "triggerType": ["click", "hover"],
        "pageType": [0, 1],
        "buttonActionType": ["dropdown", "jump", "commonAction", "custom"],
        "commonAction": ["add", "edit", "delete", "import", "export"],
        "conditionMode": [10, 20],
        "booleanFlag": [0, 1],
        # A1 既有形态：guide 输出 asc/desc 展示值，落库是数字域 10/20
        "sortType": ["asc", "desc"],
    },
    "buttonCatalog": [
        {"buttonName": "新增", "actionType": "commonAction", "commonAction": "add",
         "defaultButtonEventConfig": {"triggerType": "click", "actionType": "commonAction",
                                      "commonAction": "add", "eventScript": ""}},
        {"buttonName": "导出", "actionType": "commonAction", "commonAction": "export",
         "defaultButtonEventConfig": {"triggerType": "click", "actionType": "commonAction",
                                      "commonAction": "export", "eventScript": ""}},
    ],
}

COLUMN_TEMPLATES = [
    {"fieldType": 0, "fieldTypeName": "文本", "renderType": "text", "width": 120, "align": "left",
     "defaults": {"hideColumn": 0, "fieldShow": 1, "defaultShow": 1, "defaultFilter": 0,
                  "defaultSort": 0, "defaultFixed": 0, "defaultCalculate": 0}},
    {"fieldType": 4, "fieldTypeName": "单选", "renderType": "tag", "width": 100, "align": "center",
     "defaults": {"hideColumn": 0, "fieldShow": 1, "defaultShow": 1, "defaultFilter": 0,
                  "defaultSort": 0, "defaultFixed": 0, "defaultCalculate": 0}},
]

BUTTON_TEMPLATES = [
    {"buttonName": "新增", "commonAction": "add", "buttonType": "primary", "buttonConfirm": 0,
     "defaultButtonEventConfig": {"triggerType": "click", "actionType": "commonAction",
                                  "commonAction": "add", "eventScript": ""}},
]


class TestParsePrompt:
    """意图解析 prompt：输出意图 JSON 契约。"""

    def test_冒烟_意图契约键齐全且含fieldKey引用说明(self, loader):
        out = loader.render("njmind_list", "parse", guide=MINIMAL_GUIDE)
        for key in ("needsClarification", "clarificationQuestions", "listSummary",
                    "columnIntents", "queryIntents", "buttonIntents", "paginationIntent",
                    "filterIntents", "permissionSqlIntent"):
            assert key in out, f"parse.j2 缺意图契约键 {key}"
        assert "fieldKey" in out  # 字段引用说明
        assert "clarificationQuestions" in out

    def test_按钮动作目录来自guide非静态(self, loader):
        """动作值域渲染自 guide.buttonCatalog（SSoT），不静态复制码表。"""
        out = loader.render("njmind_list", "parse", guide=MINIMAL_GUIDE)
        assert "add" in out and "新增" in out
        assert "export" in out and "导出" in out

    def test_字段目录段融入_parse(self, loader):
        out = loader.render("njmind_list", "parse", guide=MINIMAL_GUIDE)
        assert "device_name" in out and "设备名称" in out

    def test_空guide渲染不抛(self, loader):
        out = loader.render("njmind_list", "parse", guide={})
        assert "columnIntents" in out  # 契约段仍在


class TestGeneratePrompt:
    """意图→保存形态组装 prompt：硬约束段落齐全。"""

    def test_冒烟_身份域13字段禁写(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        for f in ("listConfigId", "listCode", "listName", "listType", "serverKey",
                  "tableCode", "partTableCode", "mainTableField", "mobileListCode",
                  "dataVersion", "listState", "processDefinitionKey", "listDirConfig"):
            assert f in out, f"generate.j2 缺身份域字段名 {f}"

    def test_冒烟_0_1整数硬约束含11开关字段(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        assert "0|1" in out
        for f in ("hideColumn", "fieldShow", "defaultShow", "defaultFilter", "defaultSort",
                  "defaultFixed", "defaultCalculate", "buttonConfirm", "isTextButton",
                  "resetFilter", "showNum"):
            assert f in out, f"generate.j2 缺开关字段名 {f}"

    def test_冒烟_脚本位禁写6字段(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        for f in ("formatterScript", "hiddenScript", "beforeNotifyScript", "notifyScript",
                  "disableScript", "eventScript"):
            assert f in out, f"generate.j2 缺脚本位字段名 {f}"

    def test_冒烟_sortType数字域且guide展示值不进枚举段(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        assert "sortType" in out and "10" in out and "20" in out
        # guide enums.sortType 的 asc/desc 展示值被剔除，防 LLM 照抄
        assert "asc" not in out

    def test_冒烟_text兜底与列模板注入(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        assert "兜底" in out and "text" in out          # 无模板 fieldType 用 text 兜底
        assert '"renderType": "text"' in out            # 列模板 JSON 注入
        assert '"renderType": "tag"' in out
        # I-1（终审轮2）：11(条码)在判定域且仅 attachment 家族合法——prompt 兜底必须
        # 与校验器一致，否则含条码字段的"全部列"需求必然 TEMPLATE_MISMATCH
        assert "11(条码)" in out and '"attachment"' in out
        assert "8/14" in out

    def test_冒烟_过滤意图占位说明(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        assert "占位" in out and "conditionWhereSqlText" in out
        assert "@sql" in out  # 占位格式

    def test_冒烟_生成域白名单与枚举注入(self, loader):
        out = loader.render("njmind_list", "generate", guide=MINIMAL_GUIDE,
                            column_templates=COLUMN_TEMPLATES,
                            button_templates=BUTTON_TEMPLATES)
        for block in ("tableShowFields", "rowButtons", "buttonGroupConfig",
                      "conditionConfig", "advanceConfig", "combineConfig",
                      "queryPage", "listTableQueryCondition", "dataPermissionSqlText"):
            assert block in out, f"generate.j2 缺生成域块 {block}"
        assert "dialogList" in out          # 枚举值域注入
        assert '"add"' in out               # commonAction 枚举注入

    def test_空guide且缺模板变量渲染不抛(self, loader):
        """缺省变量安全：guide 空 dict、模板变量未传（default 过滤器兜底）。"""
        out = loader.render("njmind_list", "generate", guide={})
        assert "tableShowFields" in out  # 规则段仍在


class TestFieldCatalogSection:
    """_sections/field_catalog.j2 段渲染。"""

    def test_段落渲染_目录条目逐行出现(self, loader):
        out = loader.render("njmind_list", "_sections/field_catalog", guide=MINIMAL_GUIDE)
        for entry in MINIMAL_GUIDE["fieldCatalog"]:
            assert entry["fieldKey"] in out, f"目录条目 {entry['fieldKey']} 未渲染"
            assert entry["fieldTitle"] in out, f"目录条目 {entry['fieldTitle']} 未渲染"
        assert "part" in out  # source 来源标注

    def test_段落渲染_空guide给缺省提示不抛(self, loader):
        out = loader.render("njmind_list", "_sections/field_catalog", guide={})
        assert "fieldKey" in out  # 缺省说明仍引导用 fieldKey/追问


class TestChatPrompt:
    """chat.j2 存在性（chat.py 有 try/except 内置兜底，模板补齐后走渲染路径）。"""

    def test_chat_prompt_渲染含列表角色定位(self, loader):
        out = loader.render("njmind_list", "chat")
        assert "列表" in out
