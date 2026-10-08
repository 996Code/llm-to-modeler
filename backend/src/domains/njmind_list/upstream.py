"""njmind_list 领域上游客户端 —— 列表配置端点表/服务名/响应归一化。

端点全部是 Java 侧纯计算端点（guide/templates/validate），零持久化，
保存只走 designer 原保存流程。凭证策略沿用 njmind_form/bpm 的匿名语义
（/api/mcp/* 端点族匿名白名单放行）。
"""
import logging
from typing import Any, Dict, List, Optional

from domains.njmind_list._config_loader import load_paths, load_service_name

logger = logging.getLogger(__name__)

SERVICE_NAME = load_service_name()


class ListModelerAPI:
    """njmind-modeler 列表配置领域 API（构造注入通用传输 transport）。"""

    def __init__(self, transport):
        """transport: services.upstream_client.UpstreamClient"""
        self._t = transport
        self._paths = load_paths()

    def _path(self, key: str, **kw) -> str:
        tpl = self._paths.get(key)
        if tpl is None:
            raise KeyError(f"config.yaml paths 缺少 {key} 声明")
        return tpl.format(**kw) if kw else tpl

    # ── 静态资产（匿名读取，带缓存）──────────────────────────

    def list_tables(self) -> List[Dict[str, Any]]:
        """GET tables → [{code, name}]（数据源确认门候选清单）。

        上游把 ListTableRespVO 收敛为 {code=tableCode, name=tableName}；
        上游失败返回 None（fail-closed 由调用方维持原追问文案）。
        """
        return self._t.get(SERVICE_NAME, self._path("tables"),
                           auth=False) or []

    def get_guide(self, table_code: str, part_table_code: str = "",
                  main_table_field=()) -> Dict[str, Any]:
        """GET guide?tableCode=..&partTableCode=..&mainTableField=a,b。

        tableCode 必填；partTableCode/main_table_field（逗号分隔）可选，
        空值不进 query（上游按缺省主表语义处理）。字段目录是后续列/筛选
        白名单的唯一事实源，缺失由调用方 fail-closed 追问。
        """
        params = {"tableCode": table_code}
        if part_table_code:
            params["partTableCode"] = part_table_code
        if main_table_field:
            fields = [f for f in main_table_field if f]
            if fields:
                params["mainTableField"] = ",".join(fields)
        return self._t.get(SERVICE_NAME, self._path("guide"),
                           auth=False, params=params) or {}

    def list_templates(self) -> list:
        return self._t.get(SERVICE_NAME, self._path("templates_list"),
                           auth=False) or []

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        # name 不带 .json 时补后缀（与 form/bpm 同款约定）
        filename = name if name.endswith(".json") else f"{name}.json"
        return self._t.get(SERVICE_NAME, self._path("template", name=filename),
                           auth=False, cache=True)

    # ── 纯计算端点（零持久化）────────────────────────────────

    def validate(self, config: Dict[str, Any], field_catalog: list) -> Dict[str, Any]:
        """POST validate；body {config, fieldCatalog}（catalog 请求携带，
        服务端纯计算）。上游失败 fail-closed：
        {"pass": False, "errors": [{"message": f"上游校验失败: {err}"}]}
        """
        raw, err = self._t.post(SERVICE_NAME, self._path("validate"),
                                json_body={"config": config,
                                           "fieldCatalog": field_catalog or []},
                                auth=False)
        if raw is None:
            return {"pass": False,
                    "errors": [{"message": f"上游校验失败: {err}"}],
                    "warnings": []}
        return {"pass": raw.get("pass", False),
                "errors": (raw.get("errors") or []),
                "warnings": raw.get("warnings") or []}
