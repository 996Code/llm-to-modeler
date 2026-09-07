"""LLM 全局限速器 —— 令牌桶 RPM/TPM 双桶 + 429 指数退避 + usage 校准。

【为什么在 client 层做】
服务商限额(TPM/RPM)按账号计,不按调用方计——聊天、检索、导入任务
共享同一个配额。限速必须在所有 LLM 调用的唯一出口(llm/client)收敛,
各调用方(kg 导入的批内并发、引擎路由)无需各自节流。

【令牌桶语义】
- rpm 桶:每 60/rpm 秒补 1 个请求令牌; acquire(1) 取不到就睡到有
- tpm 桶:每 60/tpm 秒补 1 个 token 令牌; acquire(估算token) 同理
- 估算 token:中文按 chars×0.6、英文按 words 粗估(限速是防撞限额,
  不是计费);每次成功响应后用真实 usage 校准估算系数(见 on_usage),
  换模型(不同 tokenizer)后 1~2 分钟内自动收敛,无需手动留余量
- 两桶串行获取(先请求后 token),任一不足则等待

【429 退避】
收到 429(限流)后按基数指数退避(30s→60s→...→上限 10 分钟),
与业务重试(kg 的 llm_max_retries)分开计数——限流等待不算业务失败,
不烧熔断。退避状态在进程内共享:同一时刻多线程都撞 429,只记最早
的恢复时间,后续调用统一等到该时刻(防并发踩踏)。

配置(env,运行时改 env 需重启;kg 插件设置页的热生效配置走 settings
读取后调 configure() 覆盖):
    LLM_RPM_LIMIT=0     # 0=不限
    LLM_TPM_LIMIT=0
"""
import logging
import os
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# 429 退避参数(类常量,不开放配置——退避策略稳定比可调重要)
_BACKOFF_BASE_SECONDS = 30.0
_BACKOFF_MAX_SECONDS = 600.0

# acquire 时 client 侧的固定加成(client.py 估算后 +200 兜底输出/格式开销),
# 校准回剥同一常量,保证估算口径一致
_ACQUIRE_FIXED_PAD = 200

# usage 校准的 EMA 平滑系数与置信区间:只用"可信样本"(prompt 可估部分
# 占比高的纯文本调用)更新系数,置信区间外不收敛——防个别离群响应
# (如缓存命中/极短输出)把系数带偏
_CALIBRATION_ALPHA = 0.3
_CALIBRATION_MIN_RATIO = 0.2   # 估算/真实 比值下限(超出视为离群,丢弃)
_CALIBRATION_MAX_RATIO = 5.0   # 上限
# 系数夹取范围:真实 tokenizer 不会比这更极端(纯中文 ~1.5 token/字
# 上界、纯 ASCII ~0.2 token/字符下界),防校准值漂出物理合理区间
_TOKENS_PER_CJK_MIN, _TOKENS_PER_CJK_MAX = 0.3, 2.0
_TOKENS_PER_OTHER_MIN, _TOKENS_PER_OTHER_MAX = 0.1, 0.8


def estimate_tokens(text: str) -> int:
    """粗估 token 数:按当前校准系数计算(初始值 ≈ 中文 0.6/字、
    ASCII 0.25/字符;随真实 usage 收敛)。

    系数是进程级状态(get_rate_limiter 持有),这里读模块级快照——
    校准更新频率低(每次成功响应),读写竞态最坏只差一代系数,无害。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return int(cjk * _CJK_COEFF[0] + other * _OTHER_COEFF[0]) + 1


# 校准系数的模块级存储 [当前值](列表包装使模块内函数可读写;
# RateLimiter.on_usage 持锁更新,estimate_tokens 无锁读——见上,竞态无害)
_CJK_COEFF = [0.6]
_OTHER_COEFF = [0.25]


class _TokenBucket:
    """线程安全令牌桶:capacity 容量,refill_per_sec 每秒补充速率。"""

    def __init__(self, capacity: float, refill_per_sec: float):
        self._capacity = float(capacity)
        self._refill = float(refill_per_sec)
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def refund(self, n: float = 1.0) -> None:
        """返还 n 个令牌(不越容量)。调用方持 RateLimiter 锁时本方法不再
        自取桶锁(避免死锁),因此只经 RateLimiter.refund 间接调用。"""
        self._tokens = min(self._capacity, self._tokens + n)

    def acquire(self, n: float = 1.0) -> float:
        """取 n 个令牌,不足时阻塞等待。返回实际等待秒数。"""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity,
                                   self._tokens + (now - self._last) * self._refill)
                self._last = now
                if self._tokens >= n:
                    self._tokens -= n
                    return waited
                deficit = n - self._tokens
            # 桶外睡眠(不持锁),按补充速率算需要多久
            sleep_for = deficit / self._refill if self._refill > 0 else 1.0
            sleep_for = min(sleep_for, 5.0)  # 单段最长 5s,醒来重算(防时钟漂移)
            time.sleep(sleep_for)
            waited += sleep_for


class RateLimiter:
    """全局 LLM 限速器:RPM/TPM 双桶 + 429 退避闸门。

    进程级单例(见 get_rate_limiter);所有 LLM 调用出站前 acquire,
    收到 429 后 wait_backoff()。limits=(0,0) 时全部直通(零开销)。
    """

    def __init__(self, rpm: int = 0, tpm: int = 0):
        self._rpm_bucket: Optional[_TokenBucket] = None
        self._tpm_bucket: Optional[_TokenBucket] = None
        self._backoff_until = 0.0  # monotonic;429 后的全局恢复时刻
        self._lock = threading.Lock()
        self.configure(rpm, tpm)

    def configure(self, rpm: int, tpm: int) -> None:
        """更新限速参数(桶重建;已在排队中的线程按旧桶走完,无竞态危害)。"""
        with self._lock:
            self._rpm = max(0, int(rpm))
            self._tpm = max(0, int(tpm))
            self._rpm_bucket = (
                _TokenBucket(capacity=self._rpm, refill_per_sec=self._rpm / 60.0)
                if self._rpm > 0 else None)
            self._tpm_bucket = (
                _TokenBucket(capacity=self._tpm, refill_per_sec=self._tpm / 60.0)
                if self._tpm > 0 else None)

    @property
    def limits(self) -> tuple:
        return (self._rpm, self._tpm)

    def acquire(self, est_tokens: int = 1) -> float:
        """出站前调用:按需等待 RPM/TPM 配额与 429 退避闸门。

        Returns:
            本次实际等待的总秒数(观测用;0=直通)。
        """
        waited = 0.0
        # 429 退避闸门:全进程统一等到恢复时刻(防多线程同时重试踩踏)
        while True:
            with self._lock:
                remain = self._backoff_until - time.monotonic()
            if remain <= 0:
                break
            time.sleep(min(remain, 5.0))
            waited += min(remain, 5.0)
        if self._rpm_bucket:
            waited += self._rpm_bucket.acquire(1.0)
        if self._tpm_bucket:
            waited += self._tpm_bucket.acquire(float(max(1, est_tokens)))
        return waited

    def on_rate_limited(self) -> float:
        """收到 429:登记全局退避窗口(指数增长,取当前窗口与新增的较大者)。"""
        with self._lock:
            now = time.monotonic()
            current = max(0.0, self._backoff_until - now)
            new_wait = min(_BACKOFF_MAX_SECONDS,
                           max(_BACKOFF_BASE_SECONDS, current * 2.0,
                               _BACKOFF_BASE_SECONDS))
            self._backoff_until = now + new_wait
            logger.warning(f"LLM 429 rate limited — 全局退避 {new_wait:.0f}s")
            return new_wait

    def on_success(self, usage: Optional[Dict[str, int]] = None,
                   est_tokens: Optional[int] = None) -> None:
        """成功响应:清退避窗口(服务商已恢复,不必等满窗口)。

        usage 校准(可选,两个参数都给才生效):用响应的真实 token 用量
        修正估算系数——总用量 = prompt_tokens + completion_tokens,
        completion 部分是"估算不可见"的增量(输出长度调用前未知),按
        历史平均输出占比折算回 prompt 侧再校准,否则系数会被系统性
        抬高。换模型(不同 tokenizer)后系数自动收敛,TPM 不再需要
        手动留余量。
        """
        with self._lock:
            self._backoff_until = 0.0
            if usage and est_tokens and est_tokens > 0:
                self._calibrate(usage, est_tokens)

    def _calibrate(self, usage: Dict[str, int], est_tokens: int) -> None:
        """EMA 校准估算系数(持 self._lock 调用)。

        - 只信"输出占比稳定"的样本:completion 占总量 >60% 的响应
          (极短 prompt 跑飞长输出)估算意义弱,跳过
        - 比值落在置信区间外(估算/真实 离群)丢弃,防个别异常带偏
        - 系数夹取在物理合理区间,EMA 平滑(α=0.3)渐进收敛
        """
        try:
            total = int(usage.get("total_tokens") or 0)
            prompt = int(usage.get("prompt_tokens") or 0)
            completion = int(usage.get("completion_tokens") or 0)
            if total <= 0 or prompt <= 0:
                return
            if completion > total * 0.6:  # 输出主导,估算参考价值低
                return
            est_prompt_est = est_tokens - _ACQUIRE_FIXED_PAD  # 剥掉 acquire 固定加成
            if est_prompt_est <= 0:
                return
            ratio = est_prompt_est / prompt
            if not (_CALIBRATION_MIN_RATIO <= ratio <= _CALIBRATION_MAX_RATIO):
                return
            # 校准目标:让 estimate_tokens(prompt) ≈ prompt 真实 token 数
            # → 系数按 1/ratio 修正(估算偏高则调低,反之调高)
            adjust = 1.0 / ratio
            new_cjk = _CJK_COEFF[0] * adjust
            new_other = _OTHER_COEFF[0] * adjust
            _CJK_COEFF[0] = min(_TOKENS_PER_CJK_MAX,
                                max(_TOKENS_PER_CJK_MIN,
                                    _CJK_COEFF[0] * (1 - _CALIBRATION_ALPHA)
                                    + new_cjk * _CALIBRATION_ALPHA))
            _OTHER_COEFF[0] = min(_TOKENS_PER_OTHER_MAX,
                                  max(_TOKENS_PER_OTHER_MIN,
                                      _OTHER_COEFF[0] * (1 - _CALIBRATION_ALPHA)
                                      + new_other * _CALIBRATION_ALPHA))
        except (TypeError, ValueError):
            pass  # usage 结构异常不影响主流程(校准是尽力而为)

    def refund(self, est_tokens: int = 1) -> None:
        """返还配额:同一次逻辑调用的内部重试/降级重复出站时调用。

        场景:chat_json 的 json_object 直连失败降级纯文本路径,两次出站
        只该占一次 RPM/TPM 配额——直连已扣的令牌在降级前返还,防限速
        下的吞吐被内部重试结构腰斩(长文档导入尤其敏感)。
        """
        with self._lock:
            if self._rpm_bucket is not None:
                self._rpm_bucket.refund(1.0)
            if self._tpm_bucket is not None:
                self._tpm_bucket.refund(float(max(1, est_tokens)))


# 进程级单例(client 层唯一出口;kg 插件设置页热生效走 configure 覆盖)
_limiter: Optional[RateLimiter] = None
_limiter_lock = threading.Lock()


def get_rate_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        with _limiter_lock:
            if _limiter is None:
                _limiter = RateLimiter(
                    rpm=int(os.getenv("LLM_RPM_LIMIT", "0") or 0),
                    tpm=int(os.getenv("LLM_TPM_LIMIT", "0") or 0),
                )
    return _limiter


def is_rate_limit_error(exc: Exception) -> bool:
    """识别 429 限流类错误(OpenAI SDK 的 RateLimitError 或消息体含 429/限流)。"""
    name = type(exc).__name__.lower()
    if "ratelimit" in name:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "限流" in msg or "too many requests" in msg


def is_fatal_llm_error(exc: Exception) -> bool:
    """识别"重试无意义"的致命错误:鉴权失败/欠费/配额用尽/模型不存在。

    任务框架据此决定终态失败(不再自动续跑,避免空转烧重试)。
    """
    name = type(exc).__name__.lower()
    if "authentication" in name or "permission" in name:
        return True
    msg = str(exc).lower()
    fatal_markers = (
        "401", "403", "invalid api key", "incorrect api key",
        "insufficient", "quota exceeded", "billing", "欠费", "余额不足",
        "unauthorized", "forbidden", "model not found", "api key",
        # 国产网关常见的"模型不存在"文案(dashscope 等):不补的话配错
        # extraction_model 会空转三轮自动续跑(~15 分钟)
        "model not exist", "model doesn't exist", "no such model",
        "invalid model", "unknown model", "模型不存在", "不存在的模型",
    )
    return any(m in msg for m in fatal_markers)
