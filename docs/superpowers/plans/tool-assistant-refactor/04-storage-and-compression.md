# 阶段 4:存储重建 + 日志安全 + 压缩 sidechain + 清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development (recommended) or superpowers:executing-plans.

**Goal:** 重建 append-only 存储表（老数据不迁移），挂 RedactFilter，压缩改 forked sidechain（C.2-D），删除旧 graph/nodes/prompt_builder，全量回归。

**Architecture:** 新建 events + session_meta 两表；旧表 RENAME _legacy_ 留档；压缩在独立线程不阻塞主对话流；compact_trace 条目记录轨迹。

**Tech Stack:** SQLite / Python threading / logging.Filter

**前置条件:** 阶段 3 完成（Dispatcher 已工作，旧 graph 已停用但未删）

**权威来源:** v4 §5.2 ConversationManager、§7 阶段 4、C.2-D

---

## File Structure

```
backend/src/
├── engine/
│   ├── conversation.py          # 重写:append/load/save/list_meta/compress sidechain
│   ├── logging_filter.py        # 新建:RedactFilter
│   └── compression.py           # 新建:forked sidechain + compact_trace
└── services/
    └── conversation_store.py    # 重写:events/session_meta 表(旧表 RENAME)

backend/tests/
├── engine/test_conversation_append.py
├── engine/test_compression_sidechain.py
└── engine/test_logging_redact.py
```

**待删除（阶段 4 末）:**
- `backend/src/graph/graph.py`
- `backend/src/graph/nodes.py`
- `backend/src/llm/prompt_builder.py`

---

## Task 1: 存储 schema 重建（老数据不迁移）

**Files:**
- Modify: `backend/src/services/conversation_store.py`（重写表结构）
- Test: `backend/tests/engine/test_storage_rebuild.py`

**设计来源:** v4 §5.2。events 表（7 种 kind）+ session_meta 表。旧表 RENAME _legacy_。

- [ ] **Step 1: 写失败测试**
```python
def test_events_table_has_seven_kinds():
    """events 表 kind 列支持: user/assistant/tool_result/compacted/compact_trace/checkpoint/ask"""

def test_old_tables_renamed_to_legacy():
    """旧 conversations/messages 表 RENAME 为 _legacy_*。"""

def test_new_session_starts_empty():
    """新会话 events 表无历史,从零开始。"""

def test_append_only_never_updates():
    """写入只 INSERT 不 UPDATE。"""
```

- [ ] **Step 2: 实现 schema 重建**
```python
# conversation_store.py 关键改动:
# 1. 启动时检测旧表 → ALTER TABLE conversations RENAME TO _legacy_conversations
# 2. CREATE TABLE events (id, conv_id, kind, payload JSON, created_at)
# 3. CREATE TABLE session_meta (conv_id PK, title, summary, updated_at)
# 4. save() 改成 append() — 只 INSERT
```

- [ ] **Step 3: 跑测试 + Commit**
```bash
git commit -m "feat(storage): 重建 events/session_meta 表(append-only,老数据 RENAME _legacy_)"
```

---

## Task 2: ConversationManager — append/load/save/list_meta

**Files:**
- Modify: `backend/src/engine/conversation.py`（重写，阶段 0 是空壳）
- Test: `backend/tests/engine/test_conversation_append.py`

**设计来源:** v4 §5.2。load 按 kind 分流重建 messages + checkpoint + pending_ask。

- [ ] **Step 1: 写失败测试**
```python
def test_append_user_message():
    cm.append("conv1", "user", {"content": "你好"})
    # 读回验证

def test_load_rebuilds_messages_in_order():
    """user/assistant/tool_result 按序重建 messages。"""

def test_load_detects_pending_ask():
    """有 kind=ask 条目 → load 返回 pending_ask。"""

def test_save_separates_summary_and_extra():
    """ToolResult.summary 入历史,extra 不入(避免膨胀)。"""

def test_artifact_written_to_checkpoint():
    """artifact 写 kind=checkpoint,不进 messages。"""

def test_list_meta_does_not_join_events():
    """列表页只查 session_meta。"""
```

- [ ] **Step 2: 实现 ConversationManager**（委托重建后的 conversation_store）

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 3: pending_ask 持久化与恢复（C.2-A 存储部分）

**Files:**
- Modify: `backend/src/engine/conversation.py`
- Test: `backend/tests/engine/test_pending_ask_persist.py`

- [ ] **Step 1: 写测试**
```python
def test_pending_ask_persisted_as_event():
    """工具产出 ask → 写 kind=ask 条目(tool 名 + AskSpec + round)。"""

def test_crash_recovery_restores_pending_ask():
    """写 pending_ask 后崩溃,load 能恢复。"""
```

- [ ] **Step 2: 实现 + Commit**

---

## Task 4: RedactFilter 日志凭证脱敏

**Files:**
- Create: `backend/src/engine/logging_filter.py`
- Test: `backend/tests/engine/test_logging_redact.py`

**设计来源:** v4 附录 D.1 #2。正则 redact Bearer/sk-/cookie。

- [ ] **Step 1: 写失败测试**
```python
def test_redact_bearer_token():
    """logger.info(headers) 中 Authorization: Bearer xxx → ***REDACTED***。"""

def test_redact_cookie():
    """cookie 值被 redact。"""

def test_redact_sk_api_key():
    """sk-xxx 格式被 redact。"""

def test_normal_log_unchanged():
    """无凭证的日志原样输出。"""
```

- [ ] **Step 2: 实现 RedactFilter**
```python
import logging, re

class RedactFilter(logging.Filter):
    _PATTERNS = [
        (re.compile(r"(Bearer\s+)[^\s,]+", re.I), r"\1***REDACTED***"),
        (re.compile(r"(sk-)[a-zA-Z0-9]+"), r"\1***REDACTED***"),
        (re.compile(r"(cookie:\s*)[^\s,]+", re.I), r"\1***REDACTED***"),
    ]
    def filter(self, record):
        msg = record.getMessage()
        for pat, repl in self._PATTERNS:
            msg = pat.sub(repl, msg)
        record.msg = msg
        return True
```

- [ ] **Step 3: Engine 启动时挂载**
```python
# main.py 启动时:
logging.getLogger().addFilter(RedactFilter())
```

- [ ] **Step 4: 跑测试 + Commit**

---

## Task 5: 压缩 forked sidechain（C.2-D 核心）

**Files:**
- Create: `backend/src/engine/compression.py`
- Modify: `backend/src/engine/conversation.py`（compress 方法委托 sidechain）
- Test: `backend/tests/engine/test_compression_sidechain.py`

**设计来源:** v4 §5.2 compress + C.2-D。独立线程执行，主对话流不等待。

- [ ] **Step 1: 写失败测试**
```python
def test_compress_runs_in_background():
    """触发压缩 → 主对话流立即返回 keep-recent,不等压缩。"""

def test_compact_trace_written():
    """压缩完成 → 写 kind=compact_trace 条目(前后 token 数 + 摘要 + 降级标记)。"""

def test_compress_failure_does_not_block_main_flow():
    """压缩失败 → 三级保护兜底,主对话流不受影响。"""

def test_summarize_artifact_injected():
    """压缩调 tool.summarize_artifact() 拿状态补偿。"""
```

- [ ] **Step 2: 实现 compression.py**
```python
import threading
from concurrent.futures import ThreadPoolExecutor

class CompressionSidechain:
    """C.2-D:压缩在 forked 线程执行,不阻塞主对话流。"""
    def __init__(self, llm_client, store):
        self._llm = llm_client
        self._store = store
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="compress")
        self._cb = CompressionCircuitBreaker()  # 复用现有

    def compress_async(self, conv_id, messages, tool):
        """异步压缩,立即返回 keep-recent,压缩结果写 events。"""
        future = self._executor.submit(self._do_compress, conv_id, messages, tool)
        return messages[-KEEP_RECENT*2:]  # 立即返回近 N 轮

    def _do_compress(self, conv_id, messages, tool):
        try:
            # 1. 状态补偿
            compensation = tool.summarize_artifact(...)
            # 2. LLM 摘要
            summary = self._llm.chat(...)
            # 3. PTL 防御
            # 4. 写 compacted + compact_trace
            self._cb.record_success()
        except Exception as e:
            self._cb.record_failure()
            # 降级:截断
```

- [ ] **Step 3: 跑测试 + Commit**

---

## Task 6: 压缩三级保护升级

**Files:**
- Modify: `backend/src/engine/compression.py`

**设计来源:** v4 §5.2 + 附录 C.1 #7/#8/#9。

- [ ] **Step 1: 加 Summary Token 预留**（有效窗口 = 总窗口 - 20K）
- [ ] **Step 2: 加 PTL 防御**（摘要超限剥 20% 旧分组重试，上限 3 次）
- [ ] **Step 3: 加状态重启补偿**（调 tool.summarize_artifact + 工具能力复灌）
- [ ] **Step 4: 测试 + Commit**

---

## Task 7: SSE result payload 钩子化

**Files:**
- Modify: `backend/src/api/sse.py`

**设计来源:** v4 §8.3。result 的 payload 通过 tool.format_result() 拿，不再硬读 formFieldConfigVos。

- [ ] **Step 1: sse.py emit_result 改调 tool.format_result(state)**
- [ ] **Step 2: 统一 SSE schema 为 {type, tool, payload, summary}**
- [ ] **Step 3: Commit**

---

## Task 8: 删除旧代码（graph/nodes/prompt_builder）

**Files:**
- Delete: `backend/src/graph/graph.py`
- Delete: `backend/src/graph/nodes.py`
- Delete: `backend/src/llm/prompt_builder.py`

- [ ] **Step 1: 确认无引用**
```bash
cd backend && grep -rn "from src.graph\|import graph\|prompt_builder" src/ api/
# 应为空
```

- [ ] **Step 2: 删除文件**
- [ ] **Step 3: 跑全量测试确认无 import 错**
- [ ] **Step 4: Commit**
```bash
git commit -m "chore(phase4): 删除旧 graph/nodes/prompt_builder(已迁移到工具)"
```

---

## Task 9: 前端配套（接口同步改）

**Files:**
- Modify: `frontend/src/services/api.ts`（chat() 加 answers 参数）
- Modify: `frontend/src/stores/conversation.ts`（onResult 按 type 分流）
- Modify: `frontend/src/components/chat/ChatPanel.vue`（type=ask 渲染追问 UI）

- [ ] **Step 1: api.ts chat() 加 answers 参数透传**
- [ ] **Step 2: onResult 按 type 分流（config/ask/reply/error）**
- [ ] **Step 3: type=ask 渲染追问 UI（AskQuestion → 选项卡片）**
- [ ] **Step 4: type=error 渲染错误提示 + 重试**
- [ ] **Step 5: pipeline 阶段展示适配（加 select_tools 步）**
- [ ] **Step 6: 前后端契约测试**
- [ ] **Step 7: Commit**

---

## Task 10: 架构试金石 + 端到端全回归

- [ ] **Step 1: 试金石验证**
```bash
cd backend && grep -rE "formCode|formFieldConfigVos|fieldTitle|TYPE_TO_TEMPLATE" src/engine/ src/sdk/ src/adapters/
# 应为空(engine/sdk/adapters 零领域知识)
```

- [ ] **Step 2: DummyPack 端到端**（写一个 DummyTool + DummyPack 验证 Engine 不绑 njmind）

- [ ] **Step 3: 三意图 E2E + 追问 + 并发 + 压缩**

- [ ] **Step 4: header 透传（日志不泄漏）+ 落库确认 + 崩溃重放**

- [ ] **Step 5: 最终 Commit**
```bash
git commit --allow-empty -m "milestone: 工具助手架构改造完成 — Engine 零领域知识 + C.2 五项 + append-only + 安全防护"
```

---

## 阶段 4 完成检查（= 整个改造完成）

- [ ] `cd backend && python -m pytest -v` 全部 passed
- [ ] **架构试金石**: `grep -rE "formCode|formFieldConfigVos" backend/src/engine/ backend/src/sdk/ backend/src/adapters/` 无结果
- [ ] DummyTool + DummyPack 端到端跑通（证明 Engine 不绑 njmind）
- [ ] 三意图无功能回归
- [ ] **C.2 五项全部可用**:
  - [ ] C.2-A 追问重跑（pending_ask 持久化 + answers 重发 + 3 轮上限）
  - [ ] C.2-B 工具并发（is_concurrency_safe 分批 + context 延迟 apply）
  - [ ] C.2-C section 缓存（静态缓存 + cacheable=false 重算）
  - [ ] C.2-D 压缩 sidechain（forked 线程 + compact_trace）
  - [ ] C.2-E override/append（assemble 优先级）
- [ ] **接口约束**: SSE/请求体重构为 {type,tool,payload,summary}，前端配套同步改
- [ ] **老数据不迁移**: 旧表 _legacy_，新表从零开始
- [ ] **安全防护**: Unicode 清洗 + 日志 redact + 落库确认
- [ ] **存储**: append-only + 崩溃重放恢复
- [ ] 旧代码已删除（graph/nodes/prompt_builder）

---

## 全局完成

至此 `tool-assistant-refactor` 全部完成。Engine 零领域知识，njmind 收口到 pack，5 项 Claude Code 工程增强全部落地。可归档 OpenSpec change：

```bash
openspec archive tool-assistant-refactor
```
