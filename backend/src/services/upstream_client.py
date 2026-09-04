"""
上游通用传输层 —— 面向插件领域客户端的 HTTP API（零领域知识）。

【模块定位】
本模块只做「把 HTTP 请求送对地方」这一件事：
  - 地址解析：宿主 services 表按请求下发（唯一来源，见 resolve_base）；
  - 凭证策略：调用方（插件代码）每次调用自己决定带不带透传凭证（auth=）；
  - 通用治理：TTL 缓存（GET 可选）、调用日志入链、假 200 业务信封识别、
    Fail-Closed（失败返回 None/error，不抛异常）。

【分层铁律】
本文件不得出现任何领域知识：不写死服务名、不写死端点路径、不认识任何
响应格式。njmind 的端点表/服务名/响应归一化在 domains/njmind_form/upstream.py
（领域客户端），经 pack 装配钩子注入 adapter。端点路径声明在 pack 的
config.yaml paths 表（单一事实源）。

【给插件的调用 API】
    transport.get(SERVICE, path, auth=True, cache=False)  -> data | None
    transport.post(SERVICE, path, json_body=..., auth=True) -> (data, error)
  - auth=True：附带本请求的透传头（鉴权/租户等）；auth=False 匿名——
    静态公共资产应匿名（上游网关对公共资产匿名放行、带登录凭证
    反而触发端点功能权限校验，真实事故：有效 token 拉取静态资产收到
    {code:403, 没有该操作权限}，而同请求的其他资产接口全 200）。
  - 假 200 信封（HTTP 200 + {code≠成功, msg}）：按失败处理（GET 返回
    None、不进缓存；POST 返回 (None, msg)），由调用方决定业务语义。

【线程模型】
透传头/服务地址表用 threading.local 做请求级隔离——stream.py 在跑图的
工作线程内绑定（_run_graph），同线程内的节点/工具调用可读；
绑在事件循环线程读不到（早期 bug：token/地址"随机丢失"）。
注意以 main:app 启动（PYTHONPATH 含 src/）：src.X 与 X 双模块加载会让
thread-local 在两个副本间互不可见（真实事故：services 下发了但
resolve_base 读不到）。
"""
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple

# 会话上下文(同包,无循环依赖):日志层读线程绑定的 conv_id 兜底
from services.call_context import current_conversation_id

import httpx

logger = logging.getLogger(__name__)

# 线程本地存储：保存"每个请求"需要透传给上游的鉴权/上下文头 + 宿主服务
# 地址表。LangGraph 在工作线程跑节点，必须在请求开始时绑定到当前线程。
_forward_headers = threading.local()
_request_services = threading.local()


def set_forward_headers(headers: Optional[Dict[str, str]]):
    """为本线程设置要透传给上游的请求头（请求级作用域，None 清空）。"""
    if headers:
        _forward_headers.value = dict(headers)
    else:
        _forward_headers.value = None


def _get_forward_headers() -> Dict[str, str]:
    """读取本线程当前要透传的请求头；未设置返回空 dict。"""
    return getattr(_forward_headers, 'value', None) or {}


def set_request_services(services: Optional[Dict[str, str]]):
    """为本线程设置宿主服务地址表（{服务名: base_url}；None 清空）。

    嵌入模式下宿主在 chat 请求 services 字段里提供，key 是 pack manifest
    声明的服务名。
    """
    if services:
        _request_services.value = dict(services)
    else:
        _request_services.value = None


def _get_request_services() -> Dict[str, str]:
    """读取本线程的服务地址表；未设置返回空字典。"""
    return getattr(_request_services, 'value', None) or {}


class ServiceUnresolvableError(RuntimeError):
    """请求的上游服务不在本请求的宿主 services 表中（fail-closed 抛出）。

    无地址时宁可明确报错也不猜测目标——把请求打到错误地址比失败更糟。
    工具层捕获后转成用户可读的错误信息。
    """


# ── 服务标识头（固定，与领域无关）─────────────────────────────
# 所有上游请求统一携带：上游侧可据此识别/过滤/监控 AI 服务流量，
# 调用日志(call_logs)同步可见。key 固定，value 按规则生成（服务名/版本）。
# 版本单一源在此；main.py 的 FastAPI(version=) 同源引用。
CLIENT_VERSION = "0.4.0"
CLIENT_ID = f"llm-modeler/{CLIENT_VERSION}"
CLIENT_HEADER_KEY = "X-AI-Client"


class UpstreamConfig:
    """传输层行为参数（普通类，非 Pydantic）。

    上游地址不在此配置——按请求从宿主 services 表解析（见 resolve_base），
    未下发的服务 fail-closed。此处仅保留 HTTP 行为参数：
        timeout: HTTP 超时秒数（默认 30）
        cache_ttl: GET 缓存有效期秒数（默认 300=5 分钟）
    """

    def __init__(self, timeout: int = 30, cache_ttl: int = 300):
        self.timeout = timeout
        self.cache_ttl = cache_ttl


class UpstreamClient:
    """通用上游传输客户端（面向插件的领域客户端，不直接给工具用）。

    职责边界：地址解析 + 凭证策略 + 缓存/日志/信封的通用治理。
    领域知识（服务名/路径/响应语义）归 pack 的领域客户端（如
    domains/njmind_form/upstream.py 的 ModelerAPI）。
    """

    # 上游网关的「假 200」：HTTP 200 但 body 是业务错误信封 {code≠成功, msg}。
    # 只看状态码会把信封当正常内容——真实事故：有效 token 拉到
    # {code:403, msg:'没有该操作权限'} 被当静态资产缓存 300s 毒化后续请求。
    _ENVELOPE_OK_CODES = (0, 200, "0", "200")

    def __init__(self, config: Optional[UpstreamConfig] = None, conversation_store=None):
        """初始化传输客户端。

        Args:
            config: 行为参数；None 时从环境变量读（超时/缓存 TTL）。
            conversation_store: 会话存储，用于持久化上游调用日志。None 不记。
        """
        import os

        if config is None:
            config = UpstreamConfig(
                timeout=int(os.getenv("UPSTREAM_TIMEOUT", "30")),
                cache_ttl=int(os.getenv("UPSTREAM_CACHE_TTL", "300")),
            )

        self.config = config
        # 无 client 级 base_url：地址按请求解析（resolve_base 返回绝对 URL），
        # httpx 在此仅作为连接池/超时载体
        self._client = httpx.Client(timeout=config.timeout)
        # 内存缓存：key → (data, timestamp)。读多写少、偶发重复回源可接受。
        self._cache: Dict[str, tuple] = {}
        self._conversation_store = conversation_store

        logger.info("UpstreamClient initialized (通用传输;地址按请求由宿主 services 表解析)")

    # ── 地址解析 ────────────────────────────────────────────────

    def resolve_base(self, service_name: str) -> str:
        """按服务名解析上游 base URL（请求级，唯一来源是宿主 services 表）。

        未下发 → 抛 ServiceUnresolvableError——宁可明确报错也不猜测目标。

        Raises:
            ServiceUnresolvableError: 本请求的宿主 services 表未下发该服务。
        """
        host_bases = _get_request_services()
        provided = host_bases.get(service_name)
        if provided:
            return provided.rstrip("/")
        raise ServiceUnresolvableError(
            f"上游服务 '{service_name}' 无可用地址：本请求的宿主 services 表"
            f"未下发该服务。请检查宿主 INIT 的 services 字段是否包含该服务名。"
        )

    def has_service(self, service_name: str) -> bool:
        """该服务当前是否可解析出地址（宿主 services 表有该服务）。

        供工具的 preflight 钩子做执行前提校验（fail-fast）。
        """
        return service_name in _get_request_services()

    # ── 通用 HTTP API（给插件的领域客户端调用）────────────────────

    def get(self, service_name: str, path: str, *,
            auth: bool = True, cache: bool = False,
            params: Optional[dict] = None,
            extra_headers: Optional[Dict[str, str]] = None) -> Optional[Any]:
        """GET 并解析 JSON。成功返回 data；失败（网络/非2xx/假200信封）返回 None。

        Args:
            service_name: pack manifest 声明的服务名（决定 base）。
            path: 相对该服务 base 的路径（领域客户端从 manifest paths 表取）。
            auth: True 附带本请求透传头（业务端点）；False 匿名（静态公共资产）。
            cache: True 时按 (base, path) 做 TTL 缓存；params 非空时禁用
                （缓存键不含查询串，带参请求命中会串数据）。
            params: 查询参数（拼 query string）。
            extra_headers: 额外头（如 Content-Type），与透传头合并、同名覆盖。
        """
        url = f"{self.resolve_base(service_name)}{path}"
        if cache and not params:
            cached = self._get_cached(url)
            if cached is not None:
                return cached
        start = time.time()
        sent_headers = self._headers(auth, extra_headers)
        try:
            resp = self._client.get(url, params=params, headers=sent_headers)
            resp.raise_for_status()
            data = resp.json()
            env_msg = self._envelope_error_msg(data)
            if env_msg:
                logger.warning(f"GET {path} 假200业务信封: {env_msg}")
                self._log(url, error_message=f"业务信封: {env_msg}",
                          duration=self._ms(start), headers=sent_headers,
                          method="GET", params=params, response=data)
                return None
            if cache and not params:
                self._set_cached(url, data)
            self._log(url, status_code=resp.status_code,
                      duration=self._ms(start), headers=sent_headers,
                      method="GET", params=params, response=data)
            return data
        except Exception as e:
            logger.warning(f"GET {path} failed: {e}")
            self._log(url, error_message=str(e),
                      duration=self._ms(start), headers=sent_headers,
                      method="GET", params=params)
            return None

    def post(self, service_name: str, path: str, *,
             json_body: Any = None, params: Optional[dict] = None,
             auth: bool = True,
             extra_headers: Optional[Dict[str, str]] = None) -> Tuple[Optional[Any], Optional[str]]:
        """POST 并解析 JSON。成功返回 (data, None)；失败返回 (None, 原因)。

        失败原因给调用方（领域客户端）决定业务语义。extra_headers 与
        透传头合并（同名覆盖）——数据类操作需要 Content-Type 等额外头。
        """
        url = f"{self.resolve_base(service_name)}{path}"
        start = time.time()
        sent_headers = self._headers(auth, extra_headers)
        try:
            resp = self._client.post(url, json=json_body, params=params,
                                     headers=sent_headers)
            resp.raise_for_status()
            data = resp.json()
            env_msg = self._envelope_error_msg(data)
            if env_msg:
                logger.warning(f"POST {path} 假200业务信封: {env_msg}")
                self._log(url, error_message=f"业务信封: {env_msg}",
                          duration=self._ms(start), headers=sent_headers,
                          method="POST", params=params, body=json_body,
                          response=data)
                return None, env_msg
            self._log(url, status_code=resp.status_code,
                      duration=self._ms(start), headers=sent_headers,
                      method="POST", params=params, body=json_body,
                      response=data)
            return data, None
        except Exception as e:
            logger.warning(f"POST {path} failed: {e}")
            self._log(url, error_message=str(e),
                      duration=self._ms(start), headers=sent_headers,
                      method="POST", params=params, body=json_body)
            return None, str(e)

    # ── 内部：凭证/缓存/信封/日志 ─────────────────────────────────

    def _headers(self, auth: bool,
                 extra: Optional[Dict[str, str]] = None) -> Optional[Dict[str, str]]:
        """组装请求头：固定标识头 + auth 决定的透传头 + extra 叠加（后者同名覆盖）。"""
        base = (_get_forward_headers() or {}) if auth else {}
        if extra:
            base = {**base, **extra}
        base[CLIENT_HEADER_KEY] = CLIENT_ID  # 固定标识头最后写（不被覆盖）
        return base

    @classmethod
    def _envelope_error_msg(cls, data: Any) -> Optional[str]:
        """识别业务错误信封：是错误信封返回 msg，否则 None（含成功信封）。"""
        if isinstance(data, dict) and "code" in data and "msg" in data:
            if data.get("code") not in cls._ENVELOPE_OK_CODES:
                return str(data.get("msg") or f"code={data.get('code')}")
        return None

    def _get_cached(self, key: str) -> Optional[Any]:
        if key in self._cache:
            data, ts = self._cache[key]
            if time.time() - ts < self.config.cache_ttl:
                return data
        return None

    def _set_cached(self, key: str, data: Any):
        self._cache[key] = (data, time.time())


    @staticmethod
    def _ms(start: float) -> int:
        return int((time.time() - start) * 1000)

    def _log(self, endpoint: str, *, status_code: Optional[int] = None,
             error_message: Optional[str] = None, duration: int = 0,
             headers: Optional[Dict[str, str]] = None,
             method: str = "GET", params: Optional[dict] = None,
             body: Any = None, response: Any = None):
        """上游调用日志入链（call_type='upstream'）。conv_id 从线程上下文兜底，
        插件经领域客户端的调用无需透传 conv_id 即自动关联会话。

        request_data: {method, params, headers, body}——存结构化对象，
        前端 JsonViewer 自动格式化渲染（此前存截断字符串导致前端显示
        一坨未格式化文本）。response_data 同理存原始响应对象，不截断
        （产品决策：完整观测优先于 DB 体积）。
        """
        if not self._conversation_store:
            return
        conv_id = current_conversation_id()
        request_data = {
            "method": method,
            "params": params,
            "headers": dict(headers or {}),
        }
        if body is not None:
            request_data["body"] = body
        response_data = response
        try:
            self._conversation_store.save_call_log(
                call_type="upstream",
                endpoint=endpoint,
                request_data=request_data,
                response_data=response_data,
                status_code=status_code or (500 if error_message else 200),
                duration_ms=duration,
                error_message=error_message,
                conv_id=conv_id,
            )
        except Exception as e:
            logger.warning(f"Failed to save upstream call log: {e}")

    def close(self):
        """关闭 HTTP 客户端，释放连接池（失败不阻断清理链的后续组件）。"""
        try:
            self._client.close()
        except Exception as e:
            logger.warning(f"upstream close failed: {e}")
