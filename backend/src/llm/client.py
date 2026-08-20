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
  每次 LLM 调用自动持久化到 SQLite 的 call_logs 表（call_type='llm'），
  含请求参数、响应摘要、耗时、状态码，方便调试和监控。
  类比 Java 的 AOP 日志切面，但是手动埋点。
"""

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from openai import OpenAI
from pydantic import BaseModel

logger = logging.getLogger(__name__)


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

        if not config.api_key:
            # 没配 api_key 时只 warning 不抛异常：本地模型（LM Studio）常无需 key
            # 真正调用失败时由 SDK 抛错，这里给开发者一个提前提示
            logger.warning(
                "LLM_API_KEY not set — LLM calls will fail until configured."
            )

        # OpenAI SDK 客户端，兼容所有 OpenAI 兼容 API
        # 类比 Java 的 OkHttpClient 单例，全程复用底层连接
        # api_key 为空时给占位字符串：OpenAI SDK 要求非空，本地模型不校验
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "placeholder-key",  # 占位 key，本地模型忽略
            timeout=config.timeout,
        )

        logger.info(
            f"LLM client: model={config.model}, base_url={config.base_url}, "
            f"max_tokens={config.max_tokens}"
        )

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
        """持久化 LLM 调用日志到数据库 call_logs 表。

        类比 Java 的 AOP 日志切面——每次 LLM 调用自动记录，
        含请求/响应/耗时/状态码，方便调试和性能监控。
        日志保存失败不影响主流程（只 warning 不抛异常）。
        """
        if not self._conversation_store:
            return
        try:
            self._conversation_store.save_call_log(
                call_type="llm",
                endpoint=endpoint,
                request_data=request_data,
                response_data=response_data,
                status_code=status_code,
                duration_ms=duration_ms,
                error_message=error_message,
                conv_id=conv_id,
            )
        except Exception as e:
            logger.warning(f"Failed to save LLM call log: {e}")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        conv_id: Optional[str] = None,
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

        Returns:
            LLM 响应文本
        """
        start_time = time.time()  # 计时起点，用于算 duration_ms
        endpoint = f"{self.config.base_url}/chat/completions"
        # 构造请求参数快照（用于日志）：参数为 None 时回落到配置默认值
        # 类比 Java：Optional.ofNullable(temperature).orElse(config.temperature)
        request_data = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature if temperature is None else temperature,
            "max_tokens": self.config.max_tokens if max_tokens is None else max_tokens,
        }

        try:
            # 调用 OpenAI 兼容 API，类比 Java openai-java 的 service.createChatCompletion()
            response = self.client.chat.completions.create(
                model=self.config.model,
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

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                "content": result[:500],  # 截断避免过大，类比 Java log 的 StringUtils.truncate
                "finish_reason": choice.finish_reason,  # stop=正常结束, length=截断
                # model_dump() 是 Pydantic 转 dict，类比 Jackson 序列化对象为 Map
                "usage": response.usage.model_dump() if response.usage else None,  # token 用量
            }
            # prompt 总字符数（体积指标）：慢调用排查一眼定位是不是大 prompt
            messages_chars = sum(
                len(m.get("content") or "") for m in messages
            )
            self._log_call(
                endpoint=endpoint,
                # 日志只记数量/体积，不记完整内容（含用户隐私 + 太大）
                request_data={
                    "messages_count": len(messages),
                    "messages_chars": messages_chars,
                    **{k: v for k, v in request_data.items() if k != "messages"},
                },
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
            )

            return result

        except Exception as e:
            # 记录失败日志
            duration_ms = int((time.time() - start_time) * 1000)
            self._log_call(
                endpoint=endpoint,
                request_data={
                    "messages_count": len(messages),
                    "messages_chars": sum(len(m.get("content") or "") for m in messages),
                    **{k: v for k, v in request_data.items() if k != "messages"},
                },
                status_code=500,
                duration_ms=duration_ms,
                error_message=str(e),
                conv_id=conv_id,
            )
            logger.error(f"LLM chat failed: {e}")
            raise  # 重新抛出，让上层决定降级策略（不在此吞异常）

    def chat_json(
        self,
        messages: List[Dict[str, Any]],
        temperature: Optional[float] = None,
        conv_id: Optional[str] = None,
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
        request_data = {
            "model": self.config.model,
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
                "model": self.config.model,
                "messages": guided_messages,
                "temperature": temp,
                "max_tokens": self.config.max_tokens,
            }
            if not has_images:
                # response_format={"type": "json_object"} 让模型强制输出合法 JSON
                create_kwargs["response_format"] = {"type": "json_object"}

            response = self.client.chat.completions.create(**create_kwargs)  # ** 展开字典为关键字参数
            result = self._extract_json(response)  # 提取并解析 JSON，可能抛异常

            # 记录成功日志
            duration_ms = int((time.time() - start_time) * 1000)
            response_data = {
                "content": str(result)[:500],  # 截断避免日志过大
                "finish_reason": response.choices[0].finish_reason,
                "usage": response.usage.model_dump() if response.usage else None,
            }
            self._log_call(
                endpoint=endpoint,
                request_data={
                    "messages_count": len(guided_messages),
                    "messages_chars": sum(len(m.get("content") or "") for m in guided_messages),
                    **{k: v for k, v in request_data.items() if k != "messages"},
                },
                response_data=response_data,
                status_code=200,
                duration_ms=duration_ms,
                conv_id=conv_id,
            )

            return result
        except Exception:
            # 第一级失败（json_object 模式不支持或解析失败）：静默降级
            # 注意这里 pass 不记日志，因为降级是预期内的正常路径
            pass

        # Fall back to plain text + extraction
        # 中文说明：第二级降级——纯文本模式 + 手动 JSON 提取
        # 适用于不支持 response_format 的模型（如老版本 Qwen、本地 LM Studio）
        logger.info("json_object mode not supported, using plain text")
        raw = self.chat(guided_messages, temperature=temp, conv_id=conv_id)  # 复用 chat 走 Qwen3 回退
        return self._parse_json_from_text(raw)  # 三级容错解析

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

        # 三级全失败：彻底无法解析，抛错让上层决定（可能要重试或降级）
        raise ValueError(
            f"Could not extract JSON from LLM response: {text[:200]}..."
        )
