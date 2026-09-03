"""HttpAssetClient — AssetClient 的通用 HTTP 适配器（零领域知识）。

职责边界：
  - 配置类操作（模板/Schema/guide/校验/表单 CRUD）：委托 pack 注入的
    领域客户端（njmind_form 的 ModelerAPI，经 pack.py 的
    enhance_asset_client 钩子在装配时注入）——端点表/凭证策略/响应
    归一化都是领域知识，归 pack；
  - 通用数据操作（submit_data/query_data）：面向非配置类 pack，
    按 service_name 经通用传输收发，返回前 sanitize_obj；
  - has_service：透传通用传输的地址可用性判定（preflight 用）。
"""
import logging
from typing import Optional

from sdk.asset_client import AssetClient
from sdk.sanitize import sanitize_obj

logger = logging.getLogger(__name__)


class HttpAssetClient(AssetClient):
    """通用 HTTP 资产适配器：领域实现注入 + 通用数据收发。"""

    def __init__(self, upstream):
        """upstream: 通用传输（services.upstream_client.UpstreamClient）。

        配置类领域实现（modeler）由 pack 装配时经 set_modeler_api 注入；
        未注入前配置类方法不可用（测试桩可不注入）。
        """
        self._upstream = upstream
        self._modeler = None

    def set_modeler_api(self, modeler):
        """注入配置类领域客户端（pack 装配钩子调用）。"""
        self._modeler = modeler

    def _m(self):
        if self._modeler is None:
            raise RuntimeError(
                "配置类操作不可用：pack 未注入领域客户端（set_modeler_api）")
        return self._modeler

    def _clean(self, data):
        """返回前清洗（去除 null/归一化字段名/清理隐写字符）。"""
        return sanitize_obj(data)

    def has_service(self, service_name: str) -> bool:
        """该上游服务当前是否可解析地址（透传通用传输判定，preflight 用）。"""
        return self._upstream.has_service(service_name)

    # ── 表单配置类操作（领域知识归 pack，此处仅 sanitize 透传） ──

    def list_templates(self) -> list:
        return self._clean(self._m().list_templates())

    def get_template(self, name: str) -> dict:
        data = self._m().get_template(name)
        return self._clean(data) if data else {}

    def get_schema(self, name: str) -> dict:
        data = self._m().get_schema(name)
        return self._clean(data) if data else {}

    def get_guide(self) -> dict:
        data = self._m().get_guide()
        # None 透传语义：上游失败/假200信封——工具层据此追问用户刷新重开,
        # 不再静默降级成空 guide 盲跑(真实事故:整轮无类型表烧了3分半重试)
        return self._clean(data) if data else None

    def get_guide_for(self, service_name: str) -> dict:
        data = self._m().get_guide_for(service_name)
        return self._clean(data) if data else None

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验（mode 统一大写：上游接口要求 CREATE/UPDATE 枚举）。"""
        raw = self._m().validate_form(artifact, mode=mode.upper())
        return self._clean(raw) or {"valid": False, "errors": [], "warnings": []}

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """落库（预留接口，「AI 永不落库」不变量；update 按 formCode 定位）。"""
        if mode == "create":
            result = self._m().create_form(artifact)
        elif mode == "update":
            form_code = artifact.get("formCode")
            if not form_code:
                raise ValueError("update 模式缺少 formCode，无法定位要更新的表单")
            result = self._m().update_form(form_code, artifact)
        else:
            raise ValueError(f"unknown mode: {mode}")
        return self._clean(result) or {}

    def get_form(self, form_code: str) -> Optional[dict]:
        result = self._m().get_form(form_code)
        return self._clean(result) if result else None

    # ── 通用数据操作（非配置类 pack，按 service_name 经通用传输） ──

    def submit_data(self, path: str, data: dict, service_name: str,
                    headers: dict = None) -> dict:
        """提交数据到指定上游服务的相对路径(POST)。

        Args:
            path: 相对该服务 base 的 API 路径,如 "/api/leave/submit"
            data: 提交的数据体
            service_name: pack manifest 声明的上游服务名(决定 base)
            headers: 额外请求头(如 forward_headers;默认走透传凭证)
        """
        try:
            if headers:
                return self._submit_with_extra(service_name, path, data, headers)
            result, err = self._upstream.post(
                service_name, path, json_body=data, auth=True)
            if result is None:
                # Fail-Closed：地址不可解析/网络失败/假200信封统一降级，
                # 调用方拿 success=False 处理，无需 try-catch
                return {"success": False, "errors": [err or "unknown"]}
        except Exception as e:
            # 地址不可解析等传输层抛错：同样 Fail-Closed 降级
            return {"success": False, "errors": [str(e)]}
        result = self._clean(result) or {}
        # 归一化:上游可能返回 "pass" 或 "success",统一成 success
        if "success" not in result and "pass" in result:
            result["success"] = result["pass"]
        return result

    def _submit_with_extra(self, service_name, path, data, extra):
        """带自定义头的提交（透传头 + 额外头合并；仍走传输的地址解析）。"""
        import httpx
        try:
            merged = {**(self._upstream._headers(True) or {}), **extra}
            url = f"{self._upstream.resolve_base(service_name)}{path}"
            resp = httpx.post(url, json=data, headers=merged, timeout=10)
            result = self._clean(resp.json()) or {}
            if "success" not in result and "pass" in result:
                result["success"] = result["pass"]
            return result
        except Exception as e:
            logger.warning(f"submit_data POST {service_name}{path} failed: {e}")
            return {"success": False, "errors": [str(e)]}

    def query_data(self, path: str, service_name: str, params: dict = None,
                   headers: dict = None) -> dict:
        """查询上游数据(GET,按 service_name 经通用传输)。"""
        try:
            if headers:
                return self._query_with_extra(service_name, path, params, headers)
            data = self._upstream.get(service_name, path, auth=True)
            return self._clean(data) or {}
        except Exception as e:
            return {"success": False, "errors": [str(e)]}

    def _query_with_extra(self, service_name, path, params, extra):
        import httpx
        try:
            merged = {**(self._upstream._headers(True) or {}), **extra}
            url = f"{self._upstream.resolve_base(service_name)}{path}"
            resp = httpx.get(url, params=params or {}, headers=merged, timeout=10)
            return self._clean(resp.json()) or {}
        except Exception as e:
            logger.warning(f"query_data GET {service_name}{path} failed: {e}")
            return {"success": False, "errors": [str(e)]}
