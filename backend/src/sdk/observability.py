"""观测常量与通用调用记录(分层治理 P0-2)。

【为什么存在】call_type 字符串("llm"/"graph"/"vector"/...)散落各处,
前端标签表/类型筛选/概览卡各自硬编码——新增一路检索就得改 N 处,
漏一处就是"观测键名没同步"前科(P2#12 fulltext 首发即犯)。常量收敛
SDK 单一事实源;log_pack_call 是 KG _log_retrieval_call 的通用化
(任何 pack 的库调用观测都走同一 helper,conv_id/pack 解析同规则)。
"""
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 缺装配只报一次(模块级 flag 防重复刷屏——每次库调用都走本 helper,
# 无 flag 会在观测持续缺失期间每条调用刷一行 error)
_missing_store_warned = False

# call_logs.call_type 枚举(前端 labels.ts 的 CALL_TYPE_META 与此对齐)
CALL_TYPE_LLM = "llm"
CALL_TYPE_UPSTREAM = "upstream"
CALL_TYPE_GRAPH = "graph"
CALL_TYPE_VECTOR = "vector"
CALL_TYPE_FULLTEXT = "fulltext"


def log_pack_call(app_state, call_type: str, endpoint: str,
                  request_data: Dict, response_data: Dict,
                  duration_ms: int, error: Optional[str] = None,
                  conv_id: Optional[str] = None) -> None:
    """pack 级库调用入 call_logs(失败不影响调用方主流程)。

    stage 并入 request_data(与 llm/client.py 约定一致,管理端据此
    区分环节)。conv_id 解析规则与 llm/upstream 相同:显式参数优先,
    thread-local 兜底(graph 工作线程绑定了会话上下文,漏传也能关联)。
    """
    try:
        cs = getattr(app_state, "conversation_store", None)
        if cs is None:
            # 缺装配不得静默——观测数据会无声丢失, 排查时只能靠猜。
            # 报一次后不再刷屏(本 helper 每条库调用都过, 见模块级 flag 注释)。
            global _missing_store_warned
            if not _missing_store_warned:
                _missing_store_warned = True
                logger.error(
                    "观测未装配: app_state.conversation_store 缺失, "
                    "pack 调用日志(call_logs)将不落库——检查装配链"
                    "(main.py 是否把 conversation_store 挂上 app.state)")
            return
        if not conv_id:
            from sdk.call_context import current_conversation_id
            conv_id = current_conversation_id()
        from sdk.call_context import current_pack_name
        cs.save_call_log(
            call_type=call_type,
            endpoint=endpoint,
            request_data=request_data,
            response_data=response_data,
            status_code=None if not error else 500,
            duration_ms=duration_ms,
            error_message=error,
            conv_id=conv_id,
            pack_name=current_pack_name(),
        )
    except Exception as e:  # 观测写失败不拖垮调用方
        logger.warning(f"pack 调用日志写入失败({endpoint}): {e}")
