"""SDK 强类型环境变量配置解析(三十审 P3-C / 三十一审 P1-A 统一配置层)。

背景: 三十一审 P1-A——runtime._retry_config 用 float() 解析一切,
`PACK_DDL_RETRY_ATTEMPTS=5` 得到 5.0, range(1, 5.0+1) 抛
TypeError, 真实 Uvicorn 上 ChatBI 全部 API 500 而 /health 仍 200
(假健康); ConversationStore._cfg 又是另一套实现, `1.9` 被
int(float()) 静默截断为 1。两套无类型 helper 的不一致正是回归
来源。

本模块提供唯一的强类型解析入口:
  - parse_int_env:  严格十进制整数字符串(拒绝小数/NaN/Infinity/
    前后缀垃圾), 返回 int;
  - parse_float_env: 有限非负 float。
共同约束: 缺省/空串 → default; 配置存在但类型/范围非法 →
ValueError(调用方在启动期抛出 = fail-fast, 不静默修正)。
"""
import math
import os
from typing import Union

__all__ = ["parse_int_env", "parse_float_env"]


def _raw(name: str) -> Union[str, None]:
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return None
    return val.strip()


def parse_int_env(name: str, default: int, minimum: int = 1) -> int:
    """严格正整数环境变量(次数/数量类配置的唯一入口)。

    拒绝: 小数("1.9"/"5.0")、非数字、NaN/Infinity、低于 minimum。
    返回值类型恒为 int——调用方的 range()/int 运算不再有类型回归
    (三十一审 P1-A: float 进 range 抛 TypeError 让全部 API 500)。
    """
    raw = _raw(name)
    if raw is None:
        return default
    # 严格十进制整数字符串: 不接受 "5.0"、"1e3"、" 5 " 已 strip、
    # "+5"/"-1"(负数走范围拒绝)、十六进制
    try:
        val = int(raw, 10)
    except ValueError:
        raise ValueError(
            f"配置 {name}={raw!r} 不是整数——启动失败(fail-fast), "
            f"合法范围 >= {minimum}, 缺省 {default}")
    if val < minimum:
        raise ValueError(
            f"配置 {name}={raw!r} 低于下限 {minimum}——启动失败"
            f"(fail-fast), 缺省 {default}")
    return val


def parse_float_env(name: str, default: float, minimum: float = 0.0) -> float:
    """有限非负浮点环境变量(秒数/比例类配置的唯一入口)。

    拒绝: 非数字、NaN/Infinity(含 "inf"/"nan" 大小写变体)、低于
    minimum。返回值类型恒为 float。
    """
    raw = _raw(name)
    if raw is None:
        return default
    try:
        val = float(raw)
    except ValueError:
        raise ValueError(
            f"配置 {name}={raw!r} 不是数字——启动失败(fail-fast), "
            f"合法范围 >= {minimum}, 缺省 {default}")
    if math.isnan(val) or math.isinf(val) or val < minimum:
        raise ValueError(
            f"配置 {name}={raw!r} 非法(NaN/Infinity/低于下限 "
            f"{minimum})——启动失败(fail-fast)")
    return val
