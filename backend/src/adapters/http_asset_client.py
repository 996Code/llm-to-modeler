"""HttpAssetClient — AssetClient 的通用 HTTP 实现。

不绑 njmind。委托现有 UpstreamClient 发请求,返回前调 sanitize_obj。
njmind 的具体路径表由 UpstreamClient 内部管理(阶段 1 暂保留),
后续 UpstreamClient 也会从 config.yaml 读路径(本阶段不强改)。

归一化:
- validate_form 返回 {pass, errors:[str], warnings} → {valid, errors:[{message}], warnings}
- create/update 返回 {success, ...} 原样

扩展(插件化阶段):
- submit_data / query_data: 通用数据提交/查询,供非配置类插件使用。
  地址与配置类操作同一套解析(upstream.resolve_base:宿主 services 表
  按请求下发,未下发 fail-closed),服务名由 pack manifest 声明、
  工具调用时传入。返回前统一 sanitize_obj。
"""
import logging
from typing import Any, Optional

import httpx

from sdk.asset_client import AssetClient
from sdk.sanitize import sanitize_obj

logger = logging.getLogger(__name__)


class HttpAssetClient(AssetClient):
    """通用 HTTP 资产客户端。

    本阶段(阶段 1):委托 UpstreamClient 发请求,加 sanitize 层。
    通用数据操作(submit_data/query_data)通过 httpx 直接请求,
    base 由 resolve_base(service_name) 按请求解析。
    """

    def __init__(self, upstream):
        """upstream: 现有 UpstreamClient 实例(数据操作经它解析服务地址)。"""
        self._upstream = upstream

    def _clean(self, data):
        """返回前清洗。"""
        # sanitize_obj:统一清理返回数据(如去除 null/归一化字段名),类比 Java 的 DTO 转换
        return sanitize_obj(data)

    def has_service(self, service_name: str) -> bool:
        """该上游服务当前是否可解析地址（委托 UpstreamClient.has_service）。

        供工具 preflight 钩子做执行前提校验：进管线前确认地址可用，
        缺失时 fail-fast，而不是跑到第一次元数据调用才抛错。
        """
        return self._upstream.has_service(service_name)

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
        # None 透传语义：上游失败/假200信封——工具层据此追问用户刷新重开,
        # 不再静默降级成空 guide 盲跑(真实事故:整轮无类型表烧了3分半重试)
        return self._clean(data) if data else None

    def get_guide_for(self, service_name: str) -> dict:
        """按服务名取上游 guide（嵌入模式多服务地址）。

        委托 UpstreamClient.get_guide_for（resolve_base + 带 base 缓存键）。
        """
        data = self._upstream.get_guide_for(service_name)
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
        """落库（预留接口，当前无调用方——「AI 永不落库」不变量，见设计文档 §7.5）。

        update 模式走 UpstreamClient.update_form（按 formCode 更新），
        不再用 create 兜底——create 兜底会把已有表单另建一份新记录。
        """
        if mode == "create":
            result = self._upstream.create_form(artifact)
        elif mode == "update":
            form_code = artifact.get("formCode")
            if not form_code:
                raise ValueError("update 模式缺少 formCode，无法定位要更新的表单")
            result = self._upstream.update_form(form_code, artifact)
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

    def submit_data(self, path: str, data: dict, service_name: str,
                    headers: dict = None) -> dict:
        """提交数据到指定上游服务的相对路径(POST)。

        Args:
            path: 相对该服务 base 的 API 路径,如 "/api/leave/submit"
            data: 提交的数据体
            service_name: pack manifest 声明的上游服务名(决定 base,见 resolve_base)
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON,统一归一化:
            - success: bool (从 "pass" 或 "success" 字段推断)
            - errors: list[str] (原始错误列表)
            - 其余字段原样透传
        """
        req_headers = {"Content-Type": "application/json"}  # 默认 JSON 头（POST 带体）
        if headers:
            # 合并透传头（如 forward_headers 里的鉴权 token），覆盖默认
            # 类比 Java 的 HttpHeaders 多源合并
            req_headers.update(headers)
        try:
            # 地址与配置类操作同一套解析；解析失败(宿主表未下发该服务)
            # 同样走 Fail-Closed 降级,调用方拿 success=False 统一处理
            base = self._upstream.resolve_base(service_name)
            url = f"{base}{path}"
            # httpx POST:超时 10 秒(防止上游卡死拖垮整个请求)
            # timeout=10 是硬限制，超时后抛 ReadTimeout
            resp = httpx.post(url, json=data, headers=req_headers, timeout=10)
            result = resp.json()  # 解析响应 JSON
        except Exception as e:
            # 网络异常/超时/解析失败/地址不可解析:降级返回失败结构,不向上抛
            # Fail-Closed：返回 success=False，调用方能正常处理失败，无需 try-catch
            logger.warning(f"submit_data POST {service_name}{path} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        result = self._clean(result) or {}  # 清洗 + 防 None
        # 归一化:上游可能返回 "pass" 或 "success",统一成 success
        # 有些上游用 pass（兼容校验接口风格），有些用 success，这里做字段名统一
        if "success" not in result and "pass" in result:
            result["success"] = result["pass"]  # pass 值复制到 success
        return result

    def query_data(self, path: str, service_name: str, params: dict = None,
                   headers: dict = None) -> dict:
        """查询上游数据(GET)。

        Args:
            path: 相对该服务 base 的 API 路径,如 "/api/leave/status"
            service_name: pack manifest 声明的上游服务名(决定 base,见 resolve_base)
            params: 查询参数
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON(dict)
        """
        req_headers = {}
        if headers:
            # 合并透传头(GET 一般不需 Content-Type,因为没有 body)
            req_headers.update(headers)
        try:
            base = self._upstream.resolve_base(service_name)
            url = f"{base}{path}"
            # httpx GET:参数通过 params 传递(会拼到 query string)
            # params={} 或 None 都行：params or {} 防止传 None 报错
            resp = httpx.get(url, params=params or {}, headers=req_headers, timeout=10)
            result = resp.json()  # 解析响应 JSON
        except Exception as e:
            # 异常降级:返回失败结构,不向上抛(保证调用方稳定)
            # Fail-Closed：调用方拿到 success=False 就知道查询失败
            logger.warning(f"query_data GET {service_name}{path} failed: {e}")
            return {"success": False, "errors": [str(e)]}

        return self._clean(result) or {}  # 清洗 + 防 None
