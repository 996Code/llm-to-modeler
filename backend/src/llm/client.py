"""
LLM 客户端模块。

同步的 OpenAI 兼容客户端。所有方法都是同步的（SYNC），
因为 LangGraph 节点函数是同步的，直接调用不走 async。

核心设计（Java 视角）：
  - 同步客户端：类比 Java 的 OkHttp 同步调用（client.newCall(request).execute()）
  - OpenAI 兼容：支持 OpenAI / Qwen / 本地 LM Studio 等所有兼容 OpenAI API 的服务
  - reasoning_content 回退：Qwen3 推理模型把输出放在 reasoning_content 字段而非 content

Qwen3 推理模型处理：
  Qwen3 等推理模型会先"思考"（reasoning），思考内容放在 reasoning_content 字段。
  当 content 为空且 finish_reason='length'（输出被截断）时，
  实际输出在 reasoning_content 里，需要回退读取。

LLM 调用日志：
  每次 LLM 调用自动持久化到 PG 的 call_logs 表（call_type='llm'），
  含请求参数、响应摘要、耗时、状态码，方便调试和监控。
  类比 Java 的 AOP 日志切面，但是手动埋点。
"""

import json
import logging

from sdk.observability import CALL_TYPE_LLM
import os
import re
import time
from typing import Any, Dict, List, Optional

# 会话上下文:日志层读线程绑定的 conv_id 兜底(call_context 无任何依赖,无循环)
from sdk.call_context import current_conversation_id, current_pack_name

import httpx
from openai import OpenAI
from pydantic import BaseModel


def _sanitize_messages_for_log(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """日志用的消息清洗:多模态图片转占位符,其余原样(浅拷贝,不动调用方)。

    图片 content 是 data URL base64(单张可达数 MB),入库会撑爆 call_logs
    且对追溯无意义——prompt 的"文字部分"才是排查依据。占位符保留长度信息,
    便于判断"是不是传了张巨图导致慢/超限"。
    """
    sanitized = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            sanitized.append(m)  # 纯文本消息:原样(引用共享,append-only 场景安全)
            continue
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url") or ""
                part = {
                    **part,
                    "image_url": {
                        **(part.get("image_url") or {}),
                        "url": f"<image data omitted: {len(url)} chars>",
                    },
                }
            parts.append(part)
        sanitized.append({**m, "content": parts})
    return sanitized

logger = logging.getLogger(__name__)

# 全局限速器(RPM/TPM 令牌桶 + 429 退避):所有 LLM 调用唯一出口处收敛
from llm.rate_limit import (  # noqa: E402
    estimate_tokens, get_rate_limiter, is_rate_limit_error,
)
# 平台嵌入缓存键(P1#8 T2:embeddings 出站前查,命中零出站)
from llm.embed_cache import EmbedCache, embed_cache_key  # noqa: E402


class LLMConfig(BaseModel):
    """LLM 配置（Pydantic 模型，从环境变量加载）。

    类比 Java 的 @ConfigurationProperties，用环境变量注入配置。

    关键配置项：
        base_url: LLM API 地址（OpenAI/Qwen/本地 LM Studio）
        model: 模型名（如 qwen3-32b）
        temperature: 采样温度（0.1 = 偏确定性，意图识别/生成配置用低温度）
        max_tokens: 最大输出 token 数（200000 给推理模型足够的思考空间）
        timeout: 请求超时秒数（本地模型可能很慢，300 秒兜底）
    """
    base_url: str = "http://127.0.0.1:1234/v1"
    api_key: str = ""
    model: str = "qwen/qwen3.6-35b-a3b"
    temperature: float = 0.1
    max_tokens: int = 200000  # 推理模型需要大的 token 空间
    timeout: int = 300       # 本地模型推理慢，超时给足


class LLMClient:
    """同步的 OpenAI 兼容 LLM 客户端。

    所有方法同步（非 async），因为 LangGraph 节点是同步函数。
    如果在 async 上下文调用，需要用 asyncio.to_thread 包装。

    使用方式：
        client = LLMClient(config)
        # 纯文本对话
        reply = client.chat(messages)
        # JSON 格式对话（意图识别/字段解析等需要结构化输出的场景）
        result = client.chat_json(messages)
    """

    async def probe(self) -> tuple[str, str]:
        """LLM 网关连通性探活(P1-4: 探测逻辑归 client 单一实现)。

        Returns: (status, detail)。status ∈ ok/fail/unconfigured。
        GET {base}/models(3s 超时)——插件健康面板/依赖检测复用,
        不再各自手拼 HTTP。
        """
        cfg = self.config
        base = getattr(cfg, "base_url", "") or ""
        api_key = getattr(cfg, "api_key", "") or ""
        model = getattr(cfg, "model", "") or ""
        detail = f"model={model}"
        if not base:
            return "unconfigured", "未配置 base_url"
        try:
            import httpx
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            async with httpx.AsyncClient(timeout=3.0) as ac:
                r = await ac.get(f"{base.rstrip('/')}/models", headers=headers)
            status = "ok" if r.status_code < 400 else "fail"
            return status, f"{detail} · /models HTTP {r.status_code}"
        except Exception as e:
            return "fail", f"{detail} · 探针失败: {str(e)[:120]}"

    @property
    def embed_model(self) -> str:
        """当前 embeddings 后端的实际出站模型名(P1-1 单一口径)。

        解析收敛在 sdk/embedding(单一事实源):local 恒 bge-m3;api 取
        LLM_EMBED_MODEL。插件(如 KG 导入的向量索引指纹)据此判断
        "模型是否变了",不再各自读平台 env。
        """
        from sdk.embedding import embed_model as _embed_model
        return _embed_model()

    def __init__(self, config: Optional[LLMConfig] = None, conversation_store=None):
        """初始化 LLM 客户端。

        Args:
            config: LLM 配置，为 None 时从环境变量加载
            conversation_store: 会话存储，用于持久化 LLM 调用日志
        """
        if config is None:
            # 从环境变量加载配置，类比 Spring @Value("${LLM_MODEL:默认值}")
            # float()/int() 转换：因为 getenv 返回 str，类比 Java Integer.parseInt
            config = LLMConfig(
                base_url=os.getenv("LLM_BASE_URL", "http://127.0.0.1:1234/v1"),
                api_key=os.getenv("LLM_API_KEY", ""),
                model=os.getenv("LLM_MODEL", "qwen/qwen3.6-35b-a3b"),
                temperature=float(os.getenv("LLM_TEMPERATURE", "0.1")),
                max_tokens=int(os.getenv("LLM_MAX_TOKENS", "200000")),
                timeout=int(os.getenv("LLM_TIMEOUT", "300")),
            )

        self.config = config  # 保存配置实例，后续方法读 model/temperature 等
        self._conversation_store = conversation_store  # 可选的日志存储，None 时跳过日志

        # 完整 prompt 入库开关(LLM_LOG_FULL,默认开):管理端全链路追溯的核心。
        # 开=call_logs 记录完整 messages(图片转占位符)与完整响应;
        # 关=退回旧行为(仅条数/字符量 + 响应截断 500 字,隐私/体积优先)。
        self._log_full = os.getenv("LLM_LOG_FULL", "1").strip().lower() not in (
            "0", "false", "no", "off",
        )

        # 平台嵌入缓存(LRU+TTL 24h,P1#8):embeddings 出站前查——
        # 重复文本零出站(检索链路同 query 反复编码是主收益场景)。
        # 容量 EMBED_CACHE_MAX 可调(默认 4096 条;单条约数 KB 向量,
        # 上限内存占用 ~几十 MB 量级,TTL 到期惰性淘汰)
        self._embed_cache = EmbedCache(
            max_entries=int(os.getenv("EMBED_CACHE_MAX", "4096")))

        # chat 结果缓存(P1#8 T3):独立实例,与嵌入缓存互不挤占,
        # 参数同款(LRU 上限同源 EMBED_CACHE_MAX,TTL 24h)。
        # 默认关(chat 混用途缓存错层风险,LLM_CACHE_ENABLED 显式开启)
        self._chat_cache = EmbedCache(
            max_entries=int(os.getenv("EMBED_CACHE_MAX", "4096")))

        if not config.api_key:
            # 没配 api_key 时只 warning 不抛异常：本地模型（LM Studio）常无需 key
            # 真正调用失败时由 SDK 抛错，这里给开发者一个提前提示
            logger.warning(
                "LLM_API_KEY not set — LLM calls will fail until configured."
            )

        # OpenAI SDK 客户端，兼容所有 OpenAI 兼容 API
        # 类比 Java 的 OkHttpClient 单例，全程复用底层连接
        # api_key 为空时给占位字符串：OpenAI SDK 要求非空，本地模型不校验
        # max_retries 可配(env LLM_MAX_RETRIES, 默认 2 = SDK 默认):
        # 5xx/连接失败时 SDK 自动指数退避重试。LLM 网关整体不可达的部署
        # (重试注定失败, 只拖慢路由兜底)可调 0 让失败快速冒泡到上层降级。
        # httpx 传输层保活(线上实证修复 2026-10-17):SDK 默认连接池养空闲
        # 长连接,而网关侧负载均衡约 60s 掐空闲连接——池里复用死连接时
        # 请求无限挂到 LLM_TIMEOUT 超时(民法典导入卡死 1h+ 全 300s
        # timeout 的根因;容器内新连接直连同款请求 62s 成功)。keepalive
        # 探测让死连接在写前被 TCP 层发现,空闲 30s 回收让池不养陈连接
        import httpx as _httpx
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "placeholder-key",  # 占位 key，本地模型忽略
            timeout=config.timeout,
            max_retries=int(os.getenv("LLM_MAX_RETRIES", "2")),
            http_client=_httpx.Client(
                timeout=config.timeout,
                limits=_httpx.Limits(
                    max_connections=20, max_keepalive_connections=4,
                    keepalive_expiry=30.0),
                transport=_httpx.HTTPTransport(retries=1),
            ),
        )

        logger.info(
            f"LLM client: model={config.model}, base_url={config.base_url}, "
            f"max_tokens={config.max_tokens}"
        )

    def close(self):
        """关闭底层客户端（OpenAI SDK + rerank httpx，释放连接池与 socket）。

        由应用生命周期调用（main.lifespan 关闭段），与 upstream.close() /
        checkpointer 连接关闭同一收口思路——SDK 客户端不显式关闭时，
        连接池里的 socket 要等 GC 才释放。关闭失败只记 warning，
        不阻断其余组件的清理（@PreDestroy 链式调用不应互相影响）。
        """
        client = getattr(self, "client", None)
        if client is not None:
            try:
                client.close()
            except Exception as e:
                logger.warning(f"Failed to close LLM client: {e}")
        rerank_client = getattr(self, "_rerank_client", None)
        if rerank_client is not None:
            try:
                rerank_client.close()
            except Exception as e:
                logger.warning(f"Failed to close rerank client: {e}")

    def _log_call(
        self,
        endpoint: str,
        request_data: Optional[Dict] = None,
        response_data: Optional[Dict] = None,
        status_code: Optional[int] = None,
        duration_ms: Optional[int] = None,
        error_message: Optional[str] = None,
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
    ):
        """持久化 LLM 调用日志到数据库 call_logs 表。

        类比 Java 的 AOP 日志切面——每次 LLM 调用自动记录，
        含请求/响应/耗时/状态码，方便调试和性能监控。
        日志保存失败不影响主流程（只 warning 不抛异常）。

        stage 是调用环节标签（route_pack/route_tool/generate 等），
        合并进 request_data 落库——管理端链路追踪据此区分"这次 LLM
        调用是在做意图路由还是配置生成"，无需改 call_logs 表结构。
        """
        if not self._conversation_store:
            return
        if not conv_id:
            # 线程绑定的会话上下文兜底(stream.py 在 graph 工作线程绑定):
            # 调用方忘传 conv_id 时日志仍能关联会话,链路不断
            conv_id = current_conversation_id()
        try:
            if stage:
                request_data = {**(request_data or {}), "stage": stage}
            self._conversation_store.save_call_log(
                call_type=CALL_TYPE_LLM,
                endpoint=endpoint,
                request_data=request_data,
                response_data=response_data,
                status_code=status_code,
                duration_ms=duration_ms,
                error_message=error_message,
                conv_id=conv_id,
                pack_name=current_pack_name(),
            )
        except Exception as e:
            logger.warning(f"Failed to save LLM call log: {e}")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """发送对话请求，返回纯文本响应（同步）。

        核心逻辑：
        1. 调 OpenAI 兼容 API 的 chat/completions
        2. 处理 Qwen3 推理模型：content 为空时回退读 reasoning_content
        3. 自动记录调用日志（请求/响应/耗时）

        Qwen3 reasoning_content 回退机制：
        推理模型会先思考（reasoning），思考内容放 reasoning_content。
        当 content 为空（finish_reason='length' 输出被截断）时，
        实际输出在 reasoning_content，需要回退读取。

        Args:
            messages: 消息列表 [{role, content}, ...]
            temperature: 采样温度，None 用配置默认值（0.1）
            max_tokens: 最大输出 token，None 用配置默认值（200000）
            conv_id: 会话 ID（用于日志关联）
            model: 模型名覆盖，None 用配置默认值。批量任务(如知识图谱
                抽取)可用更快/更便宜的模型，对话链路不受影响

        Returns:
            LLM 响应文本
        """
        start_time = time.time()  # 计时起点，用于算 duration_ms
        endpoint = f"{self.config.base_url}/chat/completions"
        # chat 结果缓存(P1#8 T3):LLM_CACHE_ENABLED 默认关——chat 是混用途
        # 出口(路由/抽取/生成共用),缓存错层风险高,只对全同参数命中
        # (messages 逐项 role/content + model 显式覆盖优先 + temperature)。
        # 查在限速 acquire 之前:命中零出站,不应占 RPM/TPM 配额。
        # 多模态 content(list)的 sig 构造可能失败——失败跳过缓存,
        # 不阻断主链路(裁决:缓存是增强项,主链路永远直通)。
        cache_key = None
        if os.getenv("LLM_CACHE_ENABLED", "0").strip().lower() in (
                "1", "true", "yes", "on") and messages:
            try:
                sig = json.dumps([[m.get("role"), m.get("content")]
                                  for m in messages], ensure_ascii=False)
                cache_key = embed_cache_key(
                    model or self.config.model,
                    "chat|t{}|m{}".format(
                        self.config.temperature if temperature is None
                        else temperature,
                        # max_tokens 入键:截断口径不同的回复不得互相复用
                        # (显式传参优先,缺省取配置——与 chat() 出站一致)
                        self.config.max_tokens if max_tokens is None
                        else max_tokens),
                    [sig])
            except (TypeError, ValueError):
                # content 含不可序列化对象:放弃本次缓存,走原链路
                cache_key = None
            if cache_key is not None:
                hit = self._chat_cache.get(cache_key)
                if hit is not None:
                    # 命中:零出站零等待——duration=0,单独记 cache 日志
                    # (未命中路径仍走下方原观测,duration 真实)
                    self._log_call(
                        endpoint="chat:cache",
                        request_data={
                            "stage": stage, "cache_hit": True,
                            "count": len(messages)},
                        response_data={}, status_code=None, duration_ms=0,
                        conv_id=conv_id)
                    return hit
        # 全局限速:RPM/TPM 配额等待(未配置限额时零开销直通)。
        # 多模态 content(list)不计入估算——只按文本部分粗估已够防撞限额
        _est = estimate_tokens("".join(
            m.get("content") if isinstance(m.get("content"), str) else ""
            for m in messages)) + 200
        _rate_wait = get_rate_limiter().acquire(_est)
        # 单次调用级模型覆盖(None = 配置默认)
        use_model = model or self.config.model
        # 构造请求参数快照（用于日志）：参数为 None 时回落到配置默认值
        # 类比 Java：Optional.ofNullable(temperature).orElse(config.temperature)
        request_data = {
            "model": use_model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }

        try:
            # 调 OpenAI 兼容 API，类比 Java openai-java 的 service.createChatCompletion()
            response = self.client.chat.completions.create(
                model=use_model,
                messages=messages,
                temperature=self.config.temperature if temperature is None else temperature,
                max_tokens=self.config.max_tokens if max_tokens is None else max_tokens,
            )

            # choices[0]：取第一个候选回复（n=1 时只有一个），类比 Java response.getChoices().get(0)
            choice = response.choices[0]
            message = choice.message  # 消息对象，含 content / reasoning_content 等字段

            content = message.content  # 正常输出在这里

            # Fallback for reasoning models (Qwen3): when content is empty
            # and finish_reason is 'length', the output is in reasoning_content
            # 中文说明：Qwen3 推理模型把思考过程放 reasoning_content，content 可能为空
            if not content:
                # getattr 安全取属性：若 message 无 reasoning_content 字段则返回 None
                # 类比 Java 反射 field.get() + null 判断
                rc = getattr(message, "reasoning_content", None)
                if rc:
                    # 命中回退：content 空 + reasoning_content 有值，
                    # 通常是 finish_reason='length'（token 用完，思考没收住）
                    logger.warning(
                        f"content empty (finish_reason={choice.finish_reason}), "
                        f"using reasoning_content fallback"
                    )
                    content = rc

            result = content or ""  # 兜底空串，避免 None 传给调用方

            # 成功即清 429 退避闸门;带真实 usage 时顺带校准 TPM 估算系数
            # (换模型后系数自动收敛,TPM 无需手动留余量)
            get_rate_limiter().on_success(
                usage=response.usage.model_dump() if response.usage else None,
                est_tokens=_est)
            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                # 完整入库开关:开=完整内容(全链路追溯);关=截断 500 字(旧默认)
                "content": result if self._log_full else result[:500],
                "finish_reason": choice.finish_reason,  # stop=正常结束, length=截断
                # model_dump() 是 Pydantic 转 dict，类比 Jackson 序列化对象为 Map
                "usage": response.usage.model_dump() if response.usage else None,  # token 用量
                # 限速等待耗时(观测:调用慢是限流排队还是模型本身)
                **({"rateLimitWaitMs": int(_rate_wait * 1000)} if _rate_wait > 0.5 else {}),
            }
            # prompt 总字符数（体积指标）：慢调用排查一眼定位是不是大 prompt
            messages_chars = sum(
                len(m.get("content") or "") for m in messages
            )
            self._log_call(
                endpoint=endpoint,
                # LLM_LOG_FULL=1(默认):完整 messages 入库(图片转占位符)——
                # 管理端链路视图可直接回放当时的 prompt;关闭时只记数量/体积
                request_data={
                    "messages_count": len(messages),
                    "messages_chars": messages_chars,
                    **{k: v for k, v in request_data.items() if k != "messages"},
                    **({"messages": _sanitize_messages_for_log(messages)} if self._log_full else {}),
                },
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
                stage=stage,
            )

            # 成功才回填 chat 缓存(异常/失败路径不缓存坏值);
            # 未开启或 sig 构造失败时 cache_key=None,这里天然跳过
            if cache_key is not None:
                self._chat_cache.put(cache_key, result)

            return result

        except Exception as e:
            if is_rate_limit_error(e):
                # 429 限流:登记全局退避窗口(后续所有 LLM 调用统一等待),
                # 错误标记 rate_limit 供上层区分(不算业务失败,不烧熔断)
                get_rate_limiter().on_rate_limited()
            # 记录失败日志(完整模式下带上 prompt——失败排查最需要的就是当时发了什么)
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={
                    "messages_count": len(messages),
                    "messages_chars": sum(len(m.get("content") or "") for m in messages),
                    **{k: v for k, v in request_data.items() if k != "messages"},
                    **({"messages": _sanitize_messages_for_log(messages)} if self._log_full else {}),
                },
                status_code=429 if is_rate_limit_error(e) else 500,
                duration_ms=duration_ms,
                error_message=str(e),
                conv_id=conv_id,
                stage=stage,
            )
            logger.error(f"LLM chat failed: {e}")
            raise  # 重新抛出，让上层决定降级策略（不在此吞异常）

    def chat_json(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """发送对话请求并解析 JSON 响应。

        两级降级策略（适配不支持 structured output 的模型）：
        1. 优先用 json_object response_format 模式（模型原生支持 JSON 输出）
        2. 失败则降级为纯文本 + 手动 JSON 提取（_parse_json_from_text）

        多模态支持：消息含图片时跳过 json_object 模式（该模式不支持图片），
        用纯文本模式 + JSON 提取。

        使用场景：意图识别（要工具名）、字段解析（要字段列表）等需要结构化输出的场景。

        Args:
            messages: 消息列表（支持多模态 content）
            temperature: 采样温度
            conv_id: 会话 ID
            model: 模型名覆盖，None 用配置默认值(降级纯文本路径同样透传)

        Returns:
            解析后的 JSON 字典
        """
        temp = self.config.temperature if temperature is None else temperature

        # Detect multimodal content (skip json_object mode if images present)
        # 中文说明：检测是否有多模态消息（content 是 list 通常意味着含图片）
        # any() 短路求值，类比 Java stream().anyMatch()
        has_images = any(
            isinstance(m.get("content"), list) for m in messages
        )

        # Add explicit JSON instruction
        # 复制一份 messages 再追加，避免污染调用方传入的 list（类比 Java 防御性拷贝）
        guided_messages = list(messages)
        if not has_images:
            # 纯文本场景才追加 system 指令：强制模型只输出 JSON
            # 这条指令对"不守规矩"的模型很关键，否则它可能输出"好的，这是结果：{...}"
            guided_messages.append({
                "role": "system",
                "content": (
                    "重要：你必须只输出有效的 JSON，不要输出任何其他内容。"
                    "不要输出思考过程、解释或 markdown 代码块。直接输出 JSON。"
                ),
            })

        # Try json_object mode first (only for text-only messages)
        start_time = time.time()
        endpoint = f"{self.config.base_url}/chat/completions"
        # 全局限速:json_object 直连路径与 self.chat 同样出站,必须同样取配额
        # (曾漏:该路径绕过 chat 的 acquire,导致 chat_json 类调用不限速)
        _est = estimate_tokens("".join(
            m.get("content") if isinstance(m.get("content"), str) else ""
            for m in guided_messages)) + 200
        _rate_wait = get_rate_limiter().acquire(_est)
        # 单次调用级模型覆盖(None = 配置默认)
        use_model = model or self.config.model
        request_data = {
            "model": use_model,
            "messages": guided_messages,
            "temperature": temp,
            "max_tokens": self.config.max_tokens,
            # 多模态时 response_format 置 None：OpenAI 的 json_object 模式不支持图片
            "response_format": "json_object" if not has_images else None,
        }

        try:
            # 动态构造 create 参数：多模态时不带 response_format
            # 类比 Java Builder 模式按条件加参数
            create_kwargs = {
                "model": use_model,
                "messages": guided_messages,
                "temperature": temp,
                "max_tokens": self.config.max_tokens,
            }
            if not has_images:
                # response_format={"type": "json_object"} 让模型强制输出合法 JSON
                create_kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**create_kwargs)  # ** 展开字典为关键字参数
            result = self._extract_json(response)  # 提取并解析 JSON，可能抛异常
            # 成功清退避闸门;usage 校准(chat_json 的估算含追加的 JSON
            # 指令与 est 一致,校准口径统一)
            get_rate_limiter().on_success(
                usage=response.usage.model_dump() if response.usage else None,
                est_tokens=_est)

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                # 完整入库开关:开=完整解析结果(全链路追溯);关=截断 500 字
                "content": str(result) if self._log_full else str(result)[:500],
                "finish_reason": response.choices[0].finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
            }
            self._log_call(
                endpoint=endpoint,
                request_data={
                    "messages_count": len(guided_messages),
                    "messages_chars": sum(len(m.get("content") or "") for m in guided_messages),
                    **{k: v for k, v in request_data.items() if k != "messages"},
                    **({"messages": _sanitize_messages_for_log(guided_messages)} if self._log_full else {}),
                },
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
                stage=stage,
            )

            return result
        except Exception as _e:
            # 429 限流要登记退避(全局闸门);其他异常(json_object 不支持/
            # 解析失败)是预期内降级,静默走纯文本路径
            if is_rate_limit_error(_e):
                get_rate_limiter().on_rate_limited()
                raise  # 限流不降级——纯文本路径同样会撞限,直接上抛让上层等退避
            # 直连失败落一条 warn 观测(不计入正式调用日志的成功流;排查
            # "为什么每次 chat_json 都两倍请求"的第一现场),然后降级
            logger.warning(
                f"chat_json json_object path failed, falling back to plain text "
                f"(model={use_model}, stage={stage}): {_e}")
            # 防重复计费:降级路径走 self.chat 会再 acquire 一次配额——
            # 直连这次已扣的 RPM/TPM 令牌按同量返还,两次出站只占一次配额
            get_rate_limiter().refund(_est)
            # 注意这里不再静默:降级是预期内的正常路径,但每次双倍请求
            # 对限速下的长文档导入是实打实的吞吐损失,必须可见

        # Fall back to plain text + extraction
        # 中文说明：第二级降级——纯文本模式 + 手动 JSON 提取
        # 适用于不支持 response_format 的模型（如老版本 Qwen、本地 LM Studio）
        logger.info("json_object mode not supported, using plain text")
        raw = self.chat(guided_messages, temperature=temp, conv_id=conv_id,
                        stage=stage, model=model)  # 复用 chat 走 Qwen3 回退;模型覆盖同样透传
        return self._parse_json_from_text(raw)  # 三级容错解析

    # ── Embeddings(知识图谱插件的向量检索用;通用能力,任何 pack 可用) ──

    def embeddings(
        self,
        texts: List[str],
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
        mode: str = "document",
    ) -> List[List[float]]:
        """批量向量化;后端由 EMBEDDING_BACKEND 决定(同步)。

        - local(默认): 进程内 onnx 推理(bge-m3,见 local_embeddings),
          零外部依赖零费用;``LLM_EMBED_MODEL`` 默认 bge-m3
        - api: OpenAI 兼容 /v1/embeddings,模型由 ``LLM_EMBED_MODEL``
          指定;未配置直接抛 RuntimeError,由调用方降级(纯图谱模式)

        mode 只影响 local 后端(bge-m3 检索前缀,非对称检索对):
        - ``document``(默认): 直传编码——入库/文档侧,现行为不变
        - ``query``: 查询侧加官方 instruction 前缀(encode_queries)
        - api 后端无官方查询/文档区分,mode 忽略(直传,debug 留痕)

        注意:两种后端维度一致(云端 1024 / 本地 1024),切换后端
        无需重建知识库向量;但 bge-m3 与 text-embedding-v3
        语义空间不同,检索结果可能有差异,建议固定一种后端使用。

        Returns:
            与输入等长等序的向量列表。

        Failure:
            ValueError: mode 非 document/query。
            RuntimeError: 模型未配置/本地初始化失败。
            SDK 原生异常: 网络/鉴权/模型不存在(调用方决定重试或降级)。
        """
        if mode not in ("document", "query"):
            raise ValueError(f"未知 embeddings mode: {mode}")
        from sdk.embedding import embed_backend, embed_cache_label
        backend = embed_backend()
        texts = [str(t) for t in texts]
        # 缓存键 model 口径 = 该后端实际出站模型(sdk/embedding 单一源):
        # api 用 LLM_EMBED_MODEL(切模型键即变,旧向量不串味);local 用
        # 常量标签——不用 config.model,那是 chat 模型名,切 chat 模型
        # 不应冲掉嵌入缓存
        key_model = embed_cache_label()
        cache_key = embed_cache_key(key_model, mode, texts)
        hit = self._embed_cache.get(cache_key)
        if hit is not None:
            # 命中:零出站零推理——duration=0,单独记一条 cache 日志
            # (未命中路径仍走各后端原观测,duration 真实)
            self._log_call(
                endpoint="embeddings:cache",
                request_data={
                    "stage": stage or "embeddings", "count": len(texts),
                    "mode": mode, "cache_hit": True},
                response_data={"count": len(hit)},
                status_code=None, duration_ms=0, conv_id=conv_id)
            return hit
        if backend == "local":
            result = self._embeddings_local(
                texts, conv_id=conv_id, stage=stage, query=(mode == "query"))
        else:
            if mode == "query":
                logger.debug(
                    "embeddings mode=query ignored on api backend "
                    f"(stage={stage}): OpenAI 兼容端点无查询/文档区分,直传")
            result = self._embeddings_api(texts, conv_id=conv_id, stage=stage)
        # 只有成功返回才回填(异常路径不缓存坏值)
        self._embed_cache.put(cache_key, result)
        return result

    def _embeddings_local(
        self,
        texts: List[str],
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
        query: bool = False,
    ) -> List[List[float]]:
        """本地 onnx 推理后端(观测与 api 后端同规格:耗时/条数入调用日志)。

        query=True 走 encode_queries(查询侧加 bge-m3 检索前缀),
        观测 request_data 记 mode 字段区分两侧。
        """
        from llm.local_embeddings import get_local_embedder
        texts = [str(t) for t in texts]
        mode = "query" if query else "document"
        embedder = get_local_embedder()
        start = time.monotonic()
        try:
            vectors = (embedder.encode_queries(texts) if query
                       else embedder.encode(texts))
            duration_ms = int((time.monotonic() - start) * 1000)
            self._log_call(
                endpoint="embeddings:local/bge-m3",
                request_data={
                    "stage": stage or "embeddings", "count": len(texts),
                    "chars": sum(len(t) for t in texts),
                    "backend": "local", "mode": mode,
                },
                response_data={"count": len(vectors), "dim": len(vectors[0]) if vectors else 0},
                status_code=None,
                duration_ms=duration_ms,
                conv_id=conv_id,
            )
            return vectors
        except Exception as e:
            self._log_call(
                endpoint="embeddings:local/bge-m3",
                request_data={"stage": stage or "embeddings", "count": len(texts),
                              "chars": sum(len(t) for t in texts),
                              "backend": "local", "mode": mode},
                response_data={},
                status_code=500,
                duration_ms=int((time.monotonic() - start) * 1000),
                error_message=str(e),
                conv_id=conv_id,
            )
            raise

    def _embeddings_api(
        self,
        texts: List[str],
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> List[List[float]]:
        """云端 API 后端(OpenAI 兼容 /v1/embeddings)。"""
        from sdk.embedding import embed_model_or_raise
        model = embed_model_or_raise()
        texts = [str(t) for t in texts]
        start = time.monotonic()
        error = None
        vectors: List[List[float]] = []
        # 全局限速(嵌入调用也占 TPM 配额;RPM 已在 chat 出口收敛)
        _est = estimate_tokens(" ".join(texts))
        _rate_wait = get_rate_limiter().acquire(_est)
        try:
            resp = self.client.embeddings.create(model=model, input=texts)
            # 按 index 归位(SDK 不保证返回有序)
            ordered = sorted(resp.data, key=lambda d: d.index)
            vectors = [list(item.embedding) for item in ordered]
            if len(vectors) != len(texts):
                raise ValueError(f"embeddings 数量不符: {len(vectors)} != {len(texts)}")
            get_rate_limiter().on_success(
                # embeddings 响应多数网关带 usage;没有就只清退避不校准
                usage=getattr(resp, "usage", None) and {
                    "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": 0,
                    "total_tokens": getattr(resp.usage, "total_tokens", 0) or 0,
                } or None,
                est_tokens=_est)
            return vectors
        except Exception as e:
            error = str(e)
            if is_rate_limit_error(e):
                get_rate_limiter().on_rate_limited()
            raise
        finally:
            # 与 chat 同款观测:入 call_logs(文本量大时只记条数/字符量,不记全文)
            self._log_call(
                endpoint=f"embeddings:{model}",
                request_data={
                    "stage": stage or "embeddings",
                    "count": len(texts),
                    "chars": sum(len(t) for t in texts),
                },
                response_data={"dim": len(vectors[0]) if vectors else None, "count": len(vectors)},
                status_code=None if not error else 500,
                duration_ms=int((time.monotonic() - start) * 1000),
                error_message=error,
                conv_id=conv_id,
            )

    # ── rerank(外部 API,Cohere/Jina 协议) ─────────────────────

    _rerank_client: Optional["httpx.Client"] = None

    def rerank(
        self,
        query: str,
        texts: List[str],
        conv_id: Optional[str] = None,
        stage: Optional[str] = None,
    ) -> List[int]:
        """重排序:query 与 texts 的相关度排序(只接外部 API,不绑本地模型)。

        协议参照 Cohere/Jina(/rerank 非 OpenAI 官方端点,不走 self.client):
        POST {base_url}/rerank {model, query, documents, top_n}
        → {"results": [{"index", "relevance_score"}, ...] 按分数降序}

        配置(env,与 embeddings 的 LLM_EMBED_MODEL 同款按调用读取):
            RERANK_BASE_URL / RERANK_MODEL / RERANK_API_KEY
            RERANK_TIMEOUT(默认 3——排序增强不值得长等,超时即降级)
        未配置抛 RuntimeError;失败抛原异常。降级决策归调用方
        (rerank 是排序质量项不是数据正确性项,蓝图 P0#4)。

        Returns:
            按相关度降序的原文索引列表(如 [2,0,1] = texts[2] 最相关)。
        """
        base = os.getenv("RERANK_BASE_URL", "").strip().rstrip("/")
        model = os.getenv("RERANK_MODEL", "").strip()
        timeout = float(os.getenv("RERANK_TIMEOUT", "3"))
        api_key = os.getenv("RERANK_API_KEY", "").strip()
        texts = [str(t) for t in texts]
        start = time.monotonic()
        error: Optional[str] = None
        indices: List[int] = []
        results: List[Dict[str, Any]] = []   # try 前初始化:finally 观测读它
        # env 检查在 try 内抛:降级路径(开关开但平台未配置)也落 call_logs
        # 留痕,管理端能看到"为何每次都降级"而不是只有服务端 warning
        try:
            if not base or not model:
                raise RuntimeError(
                    "RERANK_BASE_URL/RERANK_MODEL 未配置,重排不可用")
            if self._rerank_client is None:
                self._rerank_client = httpx.Client(timeout=timeout)
            resp = self._rerank_client.post(
                f"{base}/rerank",
                json={"model": model, "query": query,
                      "documents": texts, "top_n": len(texts)},
                headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
            )
            # 手动状态码检查(等价 raise_for_status;非 2xx 时 resp.text 入错误消息)
            if resp.status_code != 200:
                raise RuntimeError(
                    f"rerank HTTP {resp.status_code}: {str(resp.text)[:200]}")
            results = (json.loads(resp.text) or {}).get("results") or []
            indices = [int(r["index"]) for r in results]
            if sorted(indices) != list(range(len(texts))):
                raise ValueError(
                    f"rerank results 与文档数不符: {len(indices)} 项,"
                    f"索引集 {sorted(indices)} != 0..{len(texts) - 1}")
            return indices
        except Exception as e:
            error = str(e)
            raise
        finally:
            # 观测与 embeddings 同款:入 call_logs(不记文档全文,只记条数/分数)
            scores = [float(r.get("relevance_score") or 0.0)
                      for r in results if isinstance(r, dict)]
            self._log_call(
                endpoint=f"rerank:{model}",
                request_data={"stage": stage or "rerank",
                              "query": query[:200], "count": len(texts)},
                response_data={
                    "order": indices[:20],
                    "topScore": round(max(scores), 4) if scores else None,
                },
                status_code=None if not error else 500,
                duration_ms=int((time.monotonic() - start) * 1000),
                error_message=error,
                conv_id=conv_id,
            )

    # ── JSON 提取辅助方法 ────────────────────────────────

    def _extract_json(self, response) -> Dict[str, Any]:
        """从 LLM 响应对象提取并解析 JSON。

        处理 Qwen3 推理模型：content 为空时回退读 reasoning_content。
        最终都走 _parse_json_from_text 做容错解析。
        """
        choice = response.choices[0]
        message = choice.message

        content = message.content  # 正常输出字段

        # Reasoning model fallback
        # 中文说明：Qwen3 推理模型 content 可能为空，回退读 reasoning_content
        if not content:
            rc = getattr(message, "reasoning_content", None)
            if rc:
                content = rc

        if not content:
            # 两个字段都空：模型完全没输出，无法解析，抛错触发上层降级
            raise ValueError("LLM returned empty content")

        return self._parse_json_from_text(content)  # 交给三级容错解析器

    def _parse_json_from_text(self, text: str) -> Dict[str, Any]:
        """从任意文本中提取 JSON 对象（三级容错）。

        LLM 输出的 JSON 可能不干净，三级策略逐级降级：
        1. 直接 json.loads（最理想：纯 JSON 文本）
        2. 提取 markdown 代码块 ```json ... ``` 里的内容
        3. 找第一个 { 到最后一个 } 的子串（处理 JSON 前后有文字的情况）

        三级都失败抛 ValueError。
        """
        text = text.strip()  # 去首尾空白，避免首尾换行影响解析

        # Direct parse
        # 第一级：最理想情况，整段就是合法 JSON，直接 loads
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass  # 失败则继续下一级，类比 Java 的 try-catch 链式降级

        # Markdown code block
        # 第二级：模型可能把 JSON 包在 ```json ... ``` 代码块里
        # re.search + re.DOTALL：让 . 匹配换行，跨多行匹配代码块内容
        # 分组 (.*?) 非贪婪匹配最内层内容
        code_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
        if code_match:
            try:
                return json.loads(code_match.group(1).strip())  # group(1) 取第一个分组
            except json.JSONDecodeError:
                pass  # 代码块内容仍非法，继续下一级

        # First { to last }
        # 第三级：兜底——取第一个 { 到最后一个 } 的子串
        # 适用场景：JSON 前后有"好的，结果如下："或后面有解释文字
        first = text.find("{")  # 首个左花括号位置，找不到返回 -1
        last = text.rfind("}")  # 最后一个右花括号位置
        # 双重校验：找到 + 顺序正确（last > first），避免单字符或乱序误判
        if first != -1 and last != -1 and last > first:
            try:
                return json.loads(text[first:last + 1])  # 切片含 last，故 +1
            except json.JSONDecodeError:
                pass

        # Python repr form
        # 第四级：部分网关/模型的 json_object 模式失效时，content 会输出
        # Python repr（单引号 dict、True/False/None 字面量）而非 JSON。
        # ast.literal_eval 安全解析 Python 字面量（不执行代码），
        # 再 json 序列化往返一次保证纯 JSON 兼容形态。
        # 实证：2026-09-30 996code 网关三个模型均输出 repr（call_logs 佐证）。
        candidate = text[first:last + 1] if (first != -1 and last != -1 and last > first) else text
        try:
            import ast
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, (dict, list)):
                return json.loads(json.dumps(parsed, ensure_ascii=False))
        except (ValueError, SyntaxError, TypeError):
            # TypeError: repr 含 b'x'/1j 等 literal_eval 可解析但 json.dumps
            # 不可序列化的值（实证 3.12）——按解析失败降级，不逃逸外层
            pass

        # 四级全失败：彻底无法解析，抛错让上层决定（可能要重试或降级）
        raise ValueError(
            f"Could not extract JSON from LLM response: {text[:200]}..."
        )
