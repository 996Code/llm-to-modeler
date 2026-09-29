"""njmind_bpm 领域上游客户端 —— BPM 端点表/服务名/响应归一化。

端点全部是 Java 侧纯计算/只读资产（parse/render/guide/templates/schemas/
get_form），零持久化。凭证策略沿用 njmind_form 的匿名语义（/api/mcp/* 端点族
匿名白名单放行）。
"""
import logging
from typing import Any, Dict, List, Optional

from domains.njmind_bpm._config_loader import load_paths, load_service_name

logger = logging.getLogger(__name__)

SERVICE_NAME = load_service_name()


class BpmModelerAPI:
    """njmind-modeler BPM 领域 API（构造注入通用传输 transport）。"""

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

    def get_guide(self) -> Optional[Dict[str, Any]]:
        return self._t.get(SERVICE_NAME, self._path("guide"),
                           auth=False, cache=True) or {}

    def list_templates(self) -> List[str]:
        return self._t.get(SERVICE_NAME, self._path("templates_list"),
                           auth=False) or []

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        filename = name if name.endswith(".json") else f"{name}.json"
        return self._t.get(SERVICE_NAME, self._path("template", name=filename),
                           auth=False, cache=True)

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        filename = name if name.endswith(".json") else f"{name}.schema.json"
        return self._t.get(SERVICE_NAME, self._path("schema", name=filename),
                           auth=False, cache=True)

    # ── 纯计算端点（零持久化）────────────────────────────────

    def parse(self, bpmn_xml: str) -> Dict[str, Any]:
        """XML → {draft, warnings[]}。失败 fail-closed 返回空 draft + 原因。"""
        raw, err = self._t.post(SERVICE_NAME, self._path("parse"),
                                json_body={"bpmnXml": bpmn_xml}, auth=False)
        if raw is None:
            return {"draft": None, "warnings": [
                {"code": "PARSE_FAILED", "message": f"上游解析失败: {err}"}]}
        return {"draft": raw.get("draft"), "warnings": raw.get("warnings") or []}

    def render(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        """draft → {bpmnXml, rules, validation}。REST 入参是裸 draft。
        失败 fail-closed：pass=False + 原因，不返回产物。"""
        raw, err = self._t.post(SERVICE_NAME, self._path("render"),
                                json_body=draft, auth=False)
        if raw is None:
            return {"bpmnXml": None, "rules": None,
                    "validation": {"pass": False,
                                   "errors": [{"message": f"上游渲染失败: {err}"}],
                                   "warnings": []}}
        return raw

    # ── 只读表单目录（V5 分支条件引用）──────────────────────

    def get_form(self, form_code: str) -> Optional[Dict[str, Any]]:
        return self._t.get(SERVICE_NAME, self._path("get_form", code=form_code),
                           auth=False)
