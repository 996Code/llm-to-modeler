"""njmind-modeler 领域客户端 —— 端点表/服务名/响应归一化（领域知识归 pack）。

【模块定位】
通用传输（services/upstream_client.py）只负责"把请求送对地方"；
本模块持有 njmind 的全部领域知识：
  - 服务名：manifest config.yaml services 段声明（njmind-modeler）；
  - 端点路径：manifest config.yaml paths 段（单一事实源，本模块只引用）；
  - 凭证策略（代码决定，不走配置）：
      静态资产（guide/模板/schema）匿名——网关对 MCP 资产匿名放行，
      带登录凭证反而触发端点功能权限校验（真实事故：有效 token 拉
      guide 收 {code:403}，同请求的 schemas/validate 全 200）；
      业务端点（校验/表单 CRUD）透传凭证；
  - 响应归一化：validate 的 {pass,errors:[str]} → {valid,errors:[{message}]}；
    文件名后缀补全等 njmind 约定。

HttpAssetClient（adapter）的配置类方法委托本模块实现，经 pack 装配钩子
注入（pack.py 的 enhance_asset_client），adapter 保持零领域知识。
"""
import logging
from typing import Any, Dict, List, Optional

from domains.njmind_form.tools._config_loader import (
    load_paths, load_service_name,
)

logger = logging.getLogger(__name__)

# 与 config.yaml services 声明同源（load_service_name 读 manifest 首个 key；
# _preflight 的 MODELER_SERVICE 也引用此处——改 manifest 即生效，无平行源）
SERVICE_NAME = load_service_name() or "njmind-modeler"


class ModelerAPI:
    """njmind-modeler 的领域 API（构造注入通用传输 transport）。"""

    def __init__(self, transport):
        """transport: 通用传输（services.upstream_client.UpstreamClient）。"""
        self._t = transport
        self._paths = load_paths()

    def _path(self, key: str, **kw) -> str:
        """从 manifest paths 表取路径；{placeholder} 用 kw 填充。"""
        tpl = self._paths.get(key)
        if tpl is None:
            raise KeyError(f"config.yaml paths 缺少 {key} 声明")
        return tpl.format(**kw) if kw else tpl

    # ── 静态资产（匿名读取，带缓存）──────────────────────────────

    def list_templates(self) -> List[str]:
        return self._t.get(SERVICE_NAME, self._path("templates_list"),
                           auth=False) or []

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        filename = name if name.endswith(".json") else f"{name}.json"
        return self._t.get(SERVICE_NAME, self._path("template", name=filename),
                           auth=False, cache=True)

    def list_schemas(self) -> List[str]:
        return self._t.get(SERVICE_NAME, self._path("schemas_list"),
                           auth=False) or []

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        # njmind Schema 命名规范 xxx.schema.json；已 .json 结尾视为完整名
        filename = name if name.endswith(".json") else f"{name}.schema.json"
        return self._t.get(SERVICE_NAME, self._path("schema", name=filename),
                           auth=False, cache=True)

    def get_guide(self) -> Optional[Dict[str, Any]]:
        return self._t.get(SERVICE_NAME, self._path("guide"),
                           auth=False, cache=True)

    def get_guide_for(self, service_name: str = SERVICE_NAME) -> Optional[Dict[str, Any]]:
        """按服务名取 guide（多服务地址场景；无多服务时与 get_guide 等价）。"""
        return self._t.get(service_name, self._path("guide"),
                           auth=False, cache=True)

    # ── 业务端点（透传凭证，不缓存；主键/枚举/归一化语义归 pack） ──

    def validate_artifact(self, artifact: Dict[str, Any], mode: str) -> Dict[str, Any]:
        """校验制品。mode 由 adapter 原样透传（领域客户端负责枚举归一化）。

        归一化 {pass,errors:[str]} → {valid,errors:[{message}]}。
        失败 Fail-Closed 返回 valid=False。
        """
        raw, err = self._t.post(
            SERVICE_NAME, self._path("validate"),
            json_body=artifact, params={"mode": mode.upper()}, auth=True,
        )
        if raw is None:
            reason = f"Upstream validation request failed: {err}"
            # 网关假200信封的两种典型文案：403 无权限(端点权限/登录态问题)、
            # 500 功能异常(无效 token 触发)——都指引用户刷新重开
            if err and ("权限" in err or "功能异常" in err):
                reason = (f"上游校验未执行：{err}（多为登录态过期，"
                          f"请刷新设计器页面后重开 AI 助手）")
            return {"valid": False,
                    "errors": [{"message": reason}], "warnings": []}
        return {
            "valid": raw.get("pass", False),
            "errors": [{"message": e} if isinstance(e, str) else e
                       for e in (raw.get("errors") or [])],
            "warnings": raw.get("warnings") or [],
        }

    def persist_artifact(self, artifact: Dict[str, Any], mode: str) -> Optional[Dict[str, Any]]:
        """落库（预留接口；主键提取与 create/update 分路归 pack）。"""
        if mode == "create":
            data, _err = self._t.post(SERVICE_NAME, self._path("create"),
                                      json_body=artifact, auth=True)
            return data
        form_code = artifact.get("formCode")
        if not form_code:
            raise ValueError("update 模式缺少 formCode，无法定位要更新的表单")
        data, _err = self._t.post(SERVICE_NAME, self._path("update", code=form_code),
                                  json_body=artifact, auth=True)
        return data

    def get_artifact(self, entry_id: str) -> Optional[Dict[str, Any]]:
        """按制品标识（formCode）查询已有配置。"""
        return self._t.get(SERVICE_NAME, self._path("get_form", code=entry_id),
                           auth=True)
