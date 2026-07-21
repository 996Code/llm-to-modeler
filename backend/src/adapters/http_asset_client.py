"""HttpAssetClient — AssetClient 的通用 HTTP 实现。

不绑 njmind。委托现有 UpstreamClient 发请求,返回前调 sanitize_obj。
njmind 的具体路径表由 UpstreamClient 内部管理(阶段 1 暂保留),
后续 UpstreamClient 也会从 config.yaml 读路径(本阶段不强改)。

归一化:
- validate_form 返回 {pass, errors:[str], warnings} → {valid, errors:[{message}], warnings}
- create/update 返回 {success, ...} 原样

扩展(插件化阶段):
- submit_data / query_data: 通用数据提交/查询,供非配置类插件使用。
  通过 httpx 直接请求,base_url 从环境变量 ASSET_BASE_URL 读取,
  默认 http://localhost:19999(mock API)。返回前统一 sanitize_obj。
"""
import logging
import os
from typing import Any, Optional

import httpx

from sdk.asset_client import AssetClient
from sdk.sanitize import sanitize_obj

logger = logging.getLogger(__name__)

# 通用数据操作的 base URL,从环境变量读取,默认 mock API
_DEFAULT_BASE_URL = "http://localhost:19999"


class HttpAssetClient(AssetClient):
    """通用 HTTP 资产客户端。

    本阶段(阶段 1):委托 UpstreamClient 发请求,加 sanitize 层。
    通用数据操作(submit_data/query_data)通过 httpx 直接请求。
    """

    def __init__(self, upstream):
        """upstream: 现有 UpstreamClient 实例。"""
        self._upstream = upstream
        # 通用数据操作的 base URL,优先读环境变量
        self._data_base_url = os.environ.get("ASSET_BASE_URL", _DEFAULT_BASE_URL)

    def _clean(self, data):
        """返回前清洗。"""
        return sanitize_obj(data)

    # ── 表单配置类操作(原有) ──

    def list_templates(self) -> list[str]:
        return self._clean(self._upstream.list_templates())

    def get_template(self, name: str) -> dict:
        data = self._upstream.get_template(name)
        return self._clean(data) if data else {}

    def get_schema(self, name: str) -> dict:
        data = self._upstream.get_schema(name)
        return self._clean(data) if data else {}

    def get_guide(self) -> dict:
        data = self._upstream.get_guide()
        return self._clean(data) if data else {}

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """归一化上游 {pass, errors:[str]} → {valid, errors:[{message}]}。"""
        raw = self._upstream.validate_form(artifact, mode=mode.upper())
        raw = self._clean(raw) or {}
        return {
            "valid": raw.get("pass", False),
            "errors": [{"message": e} if isinstance(e, str) else e
                       for e in (raw.get("errors") or [])],
            "warnings": raw.get("warnings") or [],
        }

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        if mode == "create":
            result = self._upstream.create_form(artifact)
        elif mode == "update":
            # 现有 UpstreamClient 无 update_form,阶段 3 完善;先用 create 兜底
            result = self._upstream.create_form(artifact)
        else:
            raise ValueError(f"unknown mode: {mode}")
        return self._clean(result) or {}

    def get_form(self, form_code: str) -> Optional[dict]:
        """根据 formCode 查询已有表单配置(委托 UpstreamClient)。"""
        result = self._upstream.get_form(form_code)
        return self._clean(result) if result else None

    # ── 通用数据操作(插件化扩展) ──

    def submit_data(self, path: str, data: dict, headers: dict = None) -> dict:
        """提交数据到上游指定路径(POST)。

        Args:
            path: API 路径,如 "/api/leave/submit"
            data: 提交的数据体
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON,统一归一化:
            - success: bool (从 "pass" 或 "success" 字段推断)
            - errors: list[str] (原始错误列表)
            - 其余字段原样透传
        """
        url = f"{self._data_base_url}{path}"
        req_headers = {"Content-Type": "application/json"}
        if headers:
            req_headers.update(headers)
        try:
            resp = httpx.post(url, json=data, headers=req_headers, timeout=10)
            result = resp.json()
        except Exception as e:
            logger.warning(f"submit_data POST {url} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        result = self._clean(result) or {}
        # 归一化:上游可能返回 "pass" 或 "success"
        if "success" not in result and "pass" in result:
            result["success"] = result["pass"]
        return result

    def query_data(self, path: str, params: dict = None, headers: dict = None) -> dict:
        """查询上游数据(GET)。

        Args:
            path: API 路径,如 "/api/leave/status"
            params: 查询参数
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON(dict)
        """
        url = f"{self._data_base_url}{path}"
        req_headers = {}
        if headers:
            req_headers.update(headers)
        try:
            resp = httpx.get(url, params=params or {}, headers=req_headers, timeout=10)
            result = resp.json()
        except Exception as e:
            logger.warning(f"query_data GET {url} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        return self._clean(result) or {}
