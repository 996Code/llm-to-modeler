"""HttpAssetClient — AssetClient 的通用 HTTP 适配器（零领域知识）。

职责边界：
  - 配置类操作（模板/Schema/guide/校验/制品 CRUD）：委托 pack 注入的
    领域客户端（经 set_config_api 注入；端点表/凭证策略/响应归一化/
    制品主键语义都是领域知识，归 pack）；
  - 通用数据操作（submit_data/query_data）：面向非配置类 pack，
    按 service_name 经通用传输收发（extra_headers 公开参数，不摸
    传输层私有成员），返回前 sanitize_obj；
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

        配置类领域实现由 pack 装配时经 set_config_api 注入；
        未注入前配置类方法不可用（测试桩可不注入）。
        """
        self._upstream = upstream
        self._config_api = None

    def set_config_api(self, config_api):
        """注入配置类领域客户端（pack 装配钩子调用，中立命名）。"""
        self._config_api = config_api

    def _c(self):
        if self._config_api is None:
            raise RuntimeError(
                "配置类操作不可用：pack 未注入领域客户端（set_config_api）")
        return self._config_api

    def _clean(self, data):
        """返回前清洗（去除 null/归一化字段名/清理隐写字符）。"""
        return sanitize_obj(data)

    def has_service(self, service_name: str) -> bool:
        """该上游服务当前是否可解析地址（透传通用传输判定，preflight 用）。"""
        return self._upstream.has_service(service_name)

    # ── 读资产（领域知识归 pack，此处仅 sanitize 透传） ──

    def list_templates(self) -> list:
        return self._clean(self._c().list_templates())

    def get_template(self, name: str) -> dict:
        data = self._c().get_template(name)
        return self._clean(data) if data else {}

    def get_schema(self, name: str) -> dict:
        data = self._c().get_schema(name)
        return self._clean(data) if data else {}

    def get_guide(self) -> Optional[dict]:
        data = self._c().get_guide()
        # None 透传语义：上游失败/假200信封——工具层据此追问用户刷新重开
        return self._clean(data) if data else None

    def get_guide_for(self, service_name: str) -> Optional[dict]:
        data = self._c().get_guide_for(service_name)
        return self._clean(data) if data else None

    # ── 制品操作（领域客户端持有主键/枚举/归一化语义） ──

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验（mode 语义由领域客户端解释，adapter 只透传）。"""
        raw = self._c().validate_artifact(artifact, mode=mode)
        return self._clean(raw) or {"valid": False, "errors": [], "warnings": []}

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """落库（预留接口，「AI 永不落库」不变量；主键提取归领域客户端）。"""
        result = self._c().persist_artifact(artifact, mode=mode)
        return self._clean(result) or {}

    def get_artifact(self, entry_id: str) -> Optional[dict]:
        result = self._c().get_artifact(entry_id)
        return self._clean(result) if result else None

    # ── 通用数据操作（非配置类 pack，按 service_name 经通用传输） ──

    def submit_data(self, path: str, data: dict, service_name: str,
                    headers: dict = None) -> dict:
        """提交数据到指定上游服务的相对路径(POST)。

        Args:
            path: 相对该服务 base 的 API 路径
            data: 提交的数据体
            service_name: pack manifest 声明的上游服务名(决定 base)
            headers: 额外请求头(如 forward_headers;默认走透传凭证)
        """
        extra = {"Content-Type": "application/json"}
        if headers:
            extra.update(headers)
        try:
            result, err = self._upstream.post(
                service_name, path, json_body=data, auth=True,
                extra_headers=extra)
            if result is None:
                return {"success": False, "errors": [err or "unknown"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)]}
        result = self._clean(result) or {}
        # 归一化:上游可能返回 "pass" 或 "success",统一成 success
        if "success" not in result and "pass" in result:
            result["success"] = result["pass"]
        return result

    def query_data(self, path: str, service_name: str, params: dict = None,
                   headers: dict = None) -> dict:
        """查询上游数据(GET,按 service_name 经通用传输)。"""
        try:
            data = self._upstream.get(service_name, path, auth=True,
                                      extra_headers=headers or None)
            if data is None:
                return {"success": False, "errors": ["查询失败"]}
        except Exception as e:
            return {"success": False, "errors": [str(e)]}
        return self._clean(data) or {}
