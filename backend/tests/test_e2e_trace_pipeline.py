"""链路打点的集成测试(真实 app + 不可达 LLM 的降级路径)。

背景事故:stream.py 的 input_data 键名被批量替换误改为驼峰
"conversationId",LangGraph 按 GraphState(snake_case)过滤后节点内
state["conversation_id"] 为 None——意图路由/工具执行打点全部静默丢失。
当时 260 项单测全绿(没有覆盖"HTTP 入口 → input_data → 节点 state"的
完整路径),只有真实链路暴露。本测试固化该路径:发一条真实 chat,
断言 intent_route 打点确实落库且带会话归属。
"""
import os
import sqlite3
import tempfile


def test_chat_writes_trace_points_end_to_end(monkeypatch):
    tmp = tempfile.mkdtemp()
    monkeypatch.setenv("DATABASE_PATH", os.path.join(tmp, "t.db"))
    monkeypatch.setenv("PACK_STATE_PATH", os.path.join(tmp, "p.json"))
    monkeypatch.setenv("ADMIN_TOKEN", "t1")
    monkeypatch.setenv("LLM_BASE_URL", "http://127.0.0.1:9/v1")  # 不可达:走 fallback
    monkeypatch.setenv("LLM_API_KEY", "x")

    # conftest 已把 backend/src 放进 sys.path，直接 import main 即可；
    # 此前 sys.path.insert(0, ".") 会把 cwd（backend/ 根目录）也放进
    # 搜索路径，触发隐式命名空间包问题导致 main 模块不正确加载
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app) as c:
        conv = c.post("/api/conversations", json={"title": "t"},
                      headers={"X-User-Id": "u"}).json()
        resp = c.post("/api/config/chat",
                      json={"message": "你好", "conversation_id": conv["id"]},
                      headers={"X-User-Id": "u"})
        assert resp.status_code == 200

        conn = sqlite3.connect(os.path.join(tmp, "t.db"))
        rows = conn.execute(
            "SELECT payload FROM events WHERE kind='trace' AND conv_id=?",
            (conv["id"],),
        ).fetchall()
        stages = [__import__("json").loads(r[0]).get("stage") for r in rows]
        # LLM 不可达 → fallback chat;但两级路由决策与工具执行(失败)仍应打点
        assert "intent_route" in stages, f"意图路由打点缺失(仅 {stages})"
        assert "tool_execute" in stages, f"工具执行打点缺失(仅 {stages})"
        assert all(r for r in rows)
