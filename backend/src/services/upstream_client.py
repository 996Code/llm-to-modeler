"""
上游 HTTP 客户端模块 —— 调用 njmind-modeler（:7001）的所有接口。

本项目是"桥接层"（BRIDGE）：所有数据格式（模板、Schema、填写指南、校验规则）
都不在本服务内部存储或生成，全部通过 HTTP 从上游 njmind-modeler 拉取或提交。

核心设计（Java 视角）：
  - 单例客户端：httpx.Client（带连接池），类比 Java 的 OkHttp/Retrofit 客户端，
    在 main.py lifespan 中创建一次，全程复用，关闭时 close()。
  - 线程本地存储：_forward_headers 用 threading.local() 保存"请求级"的透传头，
    类比 Java 的 ThreadLocal<String>，每个工作线程持有自己的 header 副本，
    避免多线程请求之间互相污染（LangGraph 在工作线程跑节点）。
  - 内存缓存：模板/Schema/指南这种"读多写少"的数据做 TTL 缓存（默认 5 分钟），
    类比 Spring Cache，减少上游压力。
  - Fail-Closed：读类接口失败返回空列表/None（不抛异常），写类接口失败返回 None，
    校验类接口失败返回 {valid: False, ...}，保证调用方总能拿到可处理的结果。

上游接口清单：
  GET  /api/mcp/templates/list-templates     → ["simple_form.json", ...]
  GET  /api/mcp/templates/{filename}         → 模板 JSON
  GET  /api/mcp/schemas/list-schemas         → ["form-config.schema.json", ...]
  GET  /api/mcp/schemas/{filename}           → Schema JSON
  GET  /api/mcp/guides/guide.json            → 填写指南 JSON
  POST /api/mcp/forms/validate?mode=CREATE   → {pass: bool, errors: [str], warnings: [str]}
  GET  /api/mcp/forms/{formCode}             → FormConfigVo JSON
  POST /api/mcp/forms/create                 → {success, message}
  POST /api/mcp/forms/{formCode}/update      → {success, formCode, message}

注意：create/update/validate 的请求体都是裸的 FormConfigVo JSON（无外层包装）。
"""

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

# httpx：现代 Python HTTP 客户端，类比 Java 的 OkHttp，支持连接池和同步/异步
import httpx

logger = logging.getLogger(__name__)

# 线程本地存储：保存"每个请求"需要透传给上游的鉴权/上下文头。
# 关键点：LangGraph 在工作线程跑节点，而 web 请求在工作线程间分发，
# 所以必须在请求开始时把 header 绑定到当前线程（set_forward_headers），
# 节点内部调上游时再读出来（_get_forward_headers），实现请求级隔离。
# 类比 Java 的 ThreadLocal<Map<String,String>>。
_forward_headers = threading.local()


def set_forward_headers(headers: Optional[Dict[str, str]]):
    """为本线程设置要透传给上游的请求头（请求级作用域）。

    通常在工作流的入口（请求处理开始时）调用一次，之后本线程内的所有上游调用
    都会自动带上这些 header（如外部租户的鉴权 token）。

    类比 Java：在拦截器里把 header 放进 ThreadLocal。

    Args:
        headers: 要透传的头字典；None 表示清空本线程的头。
    """
    if headers:
        _forward_headers.value = dict(headers)
    else:
        _forward_headers.value = None


def _get_forward_headers() -> Dict[str, str]:
    """读取本线程当前要透传的请求头。

    Returns:
        透传头字典；本线程未设置过则返回空字典（调用方可安全 merge）。
    """
    return getattr(_forward_headers, 'value', None) or {}


class UpstreamConfig:
    """上游服务配置（普通类，非 Pydantic）。

    从环境变量加载，包含：
        base_url: 上游基础地址（去掉末尾斜杠，避免拼接双斜杠）
        timeout: HTTP 超时秒数（默认 30，本地/内网通常够）
        cache_ttl: 缓存有效期秒数（默认 300=5 分钟，平衡新鲜度和上游压力）
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:7001",
        timeout: int = 30,
        cache_ttl: int = 300,  # 5 minutes
    ):
        # rstrip("/") 去掉末尾斜杠，保证后续 url 拼接不会出现 //
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_ttl = cache_ttl


class UpstreamClient:
    """上游 njmind-modeler 的 HTTP 客户端。

    职责：封装所有上游调用，提供带缓存、带日志、带透传头、Fail-Closed 的访问层。
    所有数据（模板/Schema/指南/校验/表单 CRUD）都通过本类访问上游。

    设计模式（Java 视角）：
      - 门面模式（Facade）：对外暴露语义化方法（list_templates/validate_form），
        内部封装 URL、参数、错误处理、日志，调用方无需关心 HTTP 细节。
      - 单例：main.py 中创建一次，挂到 app.state.upstream 全程复用。

    使用方式：
        client = UpstreamClient(conversation_store=store)
        templates = client.list_templates()       # 读，自动缓存
        result = client.validate_form(config)     # 写，不缓存
    """

    def __init__(self, config: Optional[UpstreamConfig] = None, conversation_store=None):
        """初始化上游客户端。

        Args:
            config: 上游配置；None 时从环境变量 UPSTREAM_BASE_URL/UPSTREAM_TIMEOUT/
                    UPSTREAM_CACHE_TTL 读取（默认 127.0.0.1:7001, 30s, 300s）。
            conversation_store: 会话存储，用于持久化上游调用日志（调试/监控）。
                                None 时不记日志（只 warning 不影响主流程）。
        """
        import os

        if config is None:
            config = UpstreamConfig(
                base_url=os.getenv("UPSTREAM_BASE_URL", "http://127.0.0.1:7001"),
                timeout=int(os.getenv("UPSTREAM_TIMEOUT", "30")),
                cache_ttl=int(os.getenv("UPSTREAM_CACHE_TTL", "300")),
            )

        self.config = config
        # 创建 httpx 客户端（带连接池），类比 Java 的 OkHttpClient 单例
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout,
        )
        # 内存缓存：key → (data, timestamp)。注意非线程安全，但本场景读多写少、
        # 偶发重复回源可接受，故未加锁（避免锁开销）。
        self._cache: Dict[str, tuple] = {}  # key → (data, timestamp)
        self._conversation_store = conversation_store

        logger.info(f"UpstreamClient initialized: {config.base_url}")

    def _log_call(
        self,
        endpoint: str,
        request_data: Optional[Dict] = None,
        response_data: Optional[Dict] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        conv_id: Optional[str] = None,
    ):
        """持久化上游调用日志到数据库 call_logs 表（call_type='upstream'）。

        类比 Java 的 AOP 日志切面——每次上游调用自动记录，
        含请求/响应/耗时/状态码，方便调试和性能监控。
        日志保存失败不影响主流程（只 warning 不抛异常）。
        """
        if not self._conversation_store:
            return
        try:
            self._conversation_store.save_call_log(
                call_type="upstream",
                endpoint=endpoint,
                request_data=request_data,
                response_data=response_data,
                status_code=status_code,
                duration_ms=duration_ms,
                error_message=error_message,
                conv_id=conv_id,
            )
        except Exception as e:
            # 日志失败不能影响主流程，只 warning
            logger.warning(f"Failed to save upstream call log: {e}")

    def _headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """合并"透传头"和"额外头"。

        透传头来自 _get_forward_headers（本线程绑定的外部鉴权头），
        extra 是本次调用特有的头（如 Content-Type）。
        extra 优先级更高（后 merge 覆盖同名 key）。

        Args:
            extra: 额外头字典，可 None。

        Returns:
            合并后的头字典；如果都没有则返回 None（httpx 用默认头）。
        """
        headers = _get_forward_headers()
        if extra:
            headers = {**headers, **extra}
        return headers or None

    def close(self):
        """关闭 HTTP 客户端，释放连接池。在应用关闭时调用。"""
        self._client.close()

    def _get_cached(self, key: str) -> Optional[Any]:
        """读取缓存项（TTL 未过期才命中）。

        Args:
            key: 缓存键。

        Returns:
            缓存的数据；未命中或已过期返回 None（不主动清理过期项，惰性淘汰）。
        """
        if key in self._cache:
            data, ts = self._cache[key]
            # 未超过 cache_ttl 才算命中
            if time.time() - ts < self.config.cache_ttl:
                return data
        return None

    def _set_cached(self, key: str, data: Any):
        """写入缓存项（带当前时间戳）。"""
        self._cache[key] = (data, time.time())

    # ── Templates（模板读类，带缓存）──────────────────────────────

    def list_templates(self) -> List[str]:
        """从上游获取模板文件名列表。

        注意：此接口不走缓存（列表轻量、可能动态变化），但失败返回空列表（不抛异常）。

        Returns:
            模板文件名列表；上游不可用或异常时返回 []（Fail-Closed）。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/templates/list-templates"
        try:
            resp = self._client.get("/api/mcp/templates/list-templates", headers=self._headers())
            resp.raise_for_status()  # 非 2xx 抛 HTTPStatusError
            result = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"templateCount": len(result)},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            # Fail-Closed：失败记日志并返回空列表，让上层逻辑能继续（不至于崩）
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to list templates: {e}")
            return []

    def get_template(self, name: str) -> Optional[Dict[str, Any]]:
        """从上游获取单个模板的完整 JSON（带 TTL 缓存）。

        Args:
            name: 模板文件名，可带可不带 .json 后缀——
                  未带 .json 时自动补全，方便调用方传简短名。

        Returns:
            模板 JSON 字典；上游不可用或不存在时返回 None（Fail-Closed）。
        """
        import time
        start_time = time.time()
        # 自动补全 .json 后缀，降低调用方心智负担
        filename = name if name.endswith(".json") else f"{name}.json"
        endpoint = f"{self.config.base_url}/api/mcp/templates/{filename}"
        cache_key = f"template:{filename}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get(f"/api/mcp/templates/{filename}", headers=self._headers())
            resp.raise_for_status()
            template = resp.json()
            self._set_cached(cache_key, template)  # 命中后缓存
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"templateName": filename},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return template
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to get template '{filename}': {e}")
            return None

    # ── Schemas（JSON Schema 读类，带缓存）────────────────────────

    def list_schemas(self) -> List[str]:
        """从上游获取 JSON Schema 文件名列表（失败返回空列表）。"""
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/schemas/list-schemas"
        try:
            resp = self._client.get("/api/mcp/schemas/list-schemas", headers=self._headers())
            resp.raise_for_status()
            result = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"schemaCount": len(result)},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to list schemas: {e}")
            return []

    def get_schema(self, name: str) -> Optional[Dict[str, Any]]:
        """从上游获取单个 JSON Schema（带 TTL 缓存）。

        Args:
            name: Schema 文件名，可带可不带 .schema.json 后缀——
                  未带时自动补全为 name.schema.json。
        """
        import time
        start_time = time.time()
        # Schema 命名规范是 xxx.schema.json，自动补全
        filename = name if name.endswith(".json") else f"{name}.schema.json"
        endpoint = f"{self.config.base_url}/api/mcp/schemas/{filename}"
        cache_key = f"schema:{filename}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get(f"/api/mcp/schemas/{filename}", headers=self._headers())
            resp.raise_for_status()
            schema = resp.json()
            self._set_cached(cache_key, schema)
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"schemaName": filename},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return schema
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to get schema '{filename}': {e}")
            return None

    # ── Guide（填写指南，带缓存）──────────────────────────────────

    def get_guide(self) -> Optional[Dict[str, Any]]:
        """从上游获取填写指南 guide.json（带 TTL 缓存）。

        指南是 LLM 生成配置时的重要参考（字段类型关键词索引），读多写少，适合缓存。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/guides/guide.json"
        cache_key = "guide"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get("/api/mcp/guides/guide.json", headers=self._headers())
            resp.raise_for_status()
            guide = resp.json()
            self._set_cached(cache_key, guide)
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"guideLoaded": True},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return guide
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to get guide: {e}")
            return None

    # ── Validation（校验，委托给上游，不缓存）─────────────────────

    def validate_form(
        self,
        form_config: Dict[str, Any],
        mode: str = "CREATE",
    ) -> Dict[str, Any]:
        """通过上游 API 校验表单配置（不缓存，每次实时校验）。

        做了响应格式归一化：
          上游返回：   {pass: bool, errors: [str], warnings: [str]}
          归一化为：   {valid: bool, errors: [{message: str}], warnings: [str]}
        （把 errors 从字符串列表升级为对象列表，方便前端展示错误详情）

        Args:
            form_config: FormConfigVo JSON，作为裸 body 提交（无外层包装）。
            mode: 校验模式，"CREATE"（新建）或 "UPDATE"（更新），默认 CREATE。

        Returns:
            归一化后的校验结果字典。
            上游请求失败时返回 {valid: False, errors: [...], warnings: []}（Fail-Closed，
            确保调用方不会把"请求失败"误判为"校验通过"）。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/forms/validate?mode={mode}"
        try:
            resp = self._client.post(
                "/api/mcp/forms/validate",
                params={"mode": mode},
                json=form_config,  # bare JSON body, no wrapper —— 裸 JSON 体，无包装
                headers=self._headers(),
            )
            resp.raise_for_status()
            raw = resp.json()

            # 归一化上游响应格式
            # upstream: {pass: bool, errors: [str], warnings: [str]}
            # normalized: {valid: bool, errors: [{message}], warnings: [str]}
            is_valid = raw.get("pass", False)  # 缺省 False，Fail-Closed
            raw_errors = raw.get("errors", [])
            # 字符串错误升级为对象，统一 errors 元素结构
            normalized_errors = [
                {"message": e} if isinstance(e, str) else e
                for e in raw_errors
            ]

            result = {
                "valid": is_valid,
                "errors": normalized_errors,
                "warnings": raw.get("warnings", []),
            }

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formName": form_config.get("formName"), "fieldCount": len(form_config.get("formFieldConfigVos", []))},
                response_data={"valid": is_valid, "errorCount": len(normalized_errors), "warningCount": len(raw.get("warnings", []))},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )

            return result

        except Exception as e:
            # Fail-Closed：请求异常时返回"未通过"，避免被误判为通过
            # 记录失败日志
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formName": form_config.get("formName")},
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Upstream validation failed: {e}")
            return {
                "valid": False,
                "errors": [{"message": f"Upstream validation request failed: {e}"}],
                "warnings": [],
            }

    # ── Forms CRUD（表单增删改查，写类不缓存）─────────────────────

    def get_form(self, form_code: str) -> Optional[Dict[str, Any]]:
        """按 formCode 从上游获取已有表单配置（不缓存，因为可能被改动）。

        Args:
            form_code: 表单编码。

        Returns:
            表单 JSON 字典；失败返回 None（Fail-Closed）。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/forms/{form_code}"
        try:
            resp = self._client.get(f"/api/mcp/forms/{form_code}", headers=self._headers())
            resp.raise_for_status()
            result = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                response_data={"formCode": form_code},
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to get form '{form_code}': {e}")
            return None

    def create_form(self, form_config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """通过上游 API 创建表单（裸 JSON body）。

        Args:
            form_config: FormConfigVo JSON。

        Returns:
            上游返回的 {success, message} 等；失败返回 None（Fail-Closed）。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/forms/create"
        try:
            resp = self._client.post("/api/mcp/forms/create", json=form_config, headers=self._headers())
            resp.raise_for_status()
            result = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formName": form_config.get("formName"), "fieldCount": len(form_config.get("formFieldConfigVos", []))},
                response_data=result,
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formName": form_config.get("formName")},
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to create form: {e}")
            return None

    def update_form(
        self,
        form_code: str,
        form_config: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """通过上游 API 更新已有表单（裸 JSON body）。

        Args:
            form_code: 要更新的表单编码。
            form_config: 新的 FormConfigVo JSON。

        Returns:
            上游返回结果；失败返回 None（Fail-Closed）。
        """
        import time
        start_time = time.time()
        endpoint = f"{self.config.base_url}/api/mcp/forms/{form_code}/update"
        try:
            resp = self._client.post(
                f"/api/mcp/forms/{form_code}/update",
                json=form_config,
                headers=self._headers(),
            )
            resp.raise_for_status()
            result = resp.json()
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formCode": form_code, "fieldCount": len(form_config.get("formFieldConfigVos", []))},
                response_data=result,
                status_code=resp.status_code,
                duration_ms=duration_ms,
            )
            return result
        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={"formCode": form_code},
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
            )
            logger.error(f"Failed to update form '{form_code}': {e}")
            return None

    # ── Health（健康探测）─────────────────────────────────────────

    def health_check(self) -> bool:
        """探测上游是否可达。

        用一个轻量 GET（拉 guide.json）+ 5 秒短超时来判断。
        注意只看 HTTP 200，不验证响应内容，属于"连通性"检查而非"业务可用性"检查。

        Returns:
            True 表示上游返回 200；任何异常或非 200 返回 False（Fail-Closed）。
        """
        try:
            # timeout=5 覆盖默认 30s，避免健康检查时长时间卡住（尤其启动期）
            resp = self._client.get("/api/mcp/guides/guide.json", timeout=5, headers=self._headers())
            return resp.status_code == 200
        except Exception:
            return False

    def clear_cache(self):
        """清空内存缓存（用于强制刷新上游数据的场景，如上游模板更新后）。"""
        self._cache.clear()
        logger.info("Upstream cache cleared")
