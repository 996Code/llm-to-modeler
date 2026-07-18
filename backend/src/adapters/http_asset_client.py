"""HttpAssetClient — AssetClient 的通用 HTTP 实现。

不绑 njmind。委托现有 UpstreamClient 发请求,返回前调 sanitize_obj。
njmind 的具体路径表由 UpstreamClient 内部管理(阶段 1 暂保留),
后续 UpstreamClient 也会从 config.yaml 读路径(本阶段不强改)。

归一化:
- validate_form 返回 {pass, errors:[str], warnings} → {valid, errors:[{message}], warnings}
- create/update 返回 {success, ...} 原样
"""
from typing import Any, Optional

from sdk.asset_client import AssetClient
from sdk.sanitize import sanitize_obj


class HttpAssetClient(AssetClient):
    """通用 HTTP 资产客户端。

    本阶段(阶段 1):委托 UpstreamClient 发请求,加 sanitize 层。
    """

    def __init__(self, upstream):
        """upstream: 现有 UpstreamClient 实例。"""
        self._upstream = upstream

    def _clean(self, data):
        """返回前清洗。"""
        return sanitize_obj(data)

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
