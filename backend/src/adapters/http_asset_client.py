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
        # 通用数据操作的 base URL,优先读环境变量(嵌入模式可指向宿主 mock API)
        self._data_base_url = os.environ.get("ASSET_BASE_URL", _DEFAULT_BASE_URL)

    def _clean(self, data):
        """返回前清洗。"""
        # sanitize_obj:统一清理返回数据(如去除 null/归一化字段名),类比 Java 的 DTO 转换
        return sanitize_obj(data)

    # ── 表单配置类操作(原有) ──

    def list_templates(self) -> list[str]:
        # 委托上游 + 清洗返回(本类只加 sanitize 层,不改业务逻辑)
        return self._clean(self._upstream.list_templates())

    def get_template(self, name: str) -> dict:
        data = self._upstream.get_template(name)
        # 上游可能返回 None(模板不存在),兜底成空 dict 避免下游 KeyError
        return self._clean(data) if data else {}

    def get_schema(self, name: str) -> dict:
        data = self._upstream.get_schema(name)
        return self._clean(data) if data else {}

    def get_guide(self) -> dict:
        data = self._upstream.get_guide()
        return self._clean(data) if data else {}

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """归一化上游 {pass, errors:[str]} → {valid, errors:[{message}]}。"""
        # mode 统一大写:上游接口要求大写枚举(CREATE/UPDATE)
        raw = self._upstream.validate_form(artifact, mode=mode.upper())
        raw = self._clean(raw) or {}  # 清洗 + 防 None
        return {
            "valid": raw.get("pass", False),  # 上游用 pass,统一成 valid
            # errors 统一成 [{message}] 结构:字符串包成对象,对象原样保留
            "errors": [{"message": e} if isinstance(e, str) else e
                       for e in (raw.get("errors") or [])],
            "warnings": raw.get("warnings") or [],
        }

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        # 按 mode 分流到上游不同接口
        # 类比 Java 的策略模式 if-else 分发
        if mode == "create":
            result = self._upstream.create_form(artifact)  # 新建走 create 接口
        elif mode == "update":
            # 现有 UpstreamClient 无 update_form,阶段 3 完善;先用 create 兜底
            # 注意：临时用 create 兜底，后续接入真正的 update 接口
            result = self._upstream.create_form(artifact)
        else:
            # 未知 mode:直接抛(防御性编程)
            # 抛异常而非静默处理：mode 错误是编程 bug，应尽早暴露
            raise ValueError(f"unknown mode: {mode}")
        return self._clean(result) or {}  # 清洗 + 防 None

    def get_form(self, form_code: str) -> Optional[dict]:
        """根据 formCode 查询已有表单配置(委托 UpstreamClient)。"""
        result = self._upstream.get_form(form_code)
        # 找不到返回 None(区别于空 dict:None 表示不存在,空 dict 表示存在但无数据)
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
        url = f"{self._data_base_url}{path}"  # 拼完整 URL（base + 路径）
        req_headers = {"Content-Type": "application/json"}  # 默认 JSON 头（POST 带体）
        if headers:
            # 合并透传头（如 forward_headers 里的鉴权 token），覆盖默认
            # 类比 Java 的 HttpHeaders 多源合并
            req_headers.update(headers)
        try:
            # httpx POST:超时 10 秒(防止上游卡死拖垮整个请求)
            # timeout=10 是硬限制，超时后抛 ReadTimeout
            resp = httpx.post(url, json=data, headers=req_headers, timeout=10)
            result = resp.json()  # 解析响应 JSON
        except Exception as e:
            # 网络异常/超时/解析失败:降级返回失败结构,不向上抛
            # Fail-Closed：返回 success=False，调用方能正常处理失败，无需 try-catch
            logger.warning(f"submit_data POST {url} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        result = self._clean(result) or {}  # 清洗 + 防 None
        # 归一化:上游可能返回 "pass" 或 "success",统一成 success
        # 有些上游用 pass（兼容校验接口风格），有些用 success，这里做字段名统一
        if "success" not in result and "pass" in result:
            result["success"] = result["pass"]  # pass 值复制到 success
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
        url = f"{self._data_base_url}{path}"  # 拼完整 URL
        req_headers = {}
        if headers:
            # 合并透传头(GET 一般不需 Content-Type,因为没有 body)
            req_headers.update(headers)
        try:
            # httpx GET:参数通过 params 传递(会拼到 query string)
            # params={} 或 None 都行：params or {} 防止传 None 报错
            resp = httpx.get(url, params=params or {}, headers=req_headers, timeout=10)
            result = resp.json()  # 解析响应 JSON
        except Exception as e:
            # 异常降级:返回失败结构,不向上抛(保证调用方稳定)
            # Fail-Closed：调用方拿到 success=False 就知道查询失败
            logger.warning(f"query_data GET {url} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        return self._clean(result) or {}  # 清洗 + 防 None
