"""Demo mocks are exercised over real MCP HTTP, including concurrent calls."""
import importlib.util
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest


def load_fake_servers():
    path = Path(__file__).resolve().parents[2] / "tools/mcp-e2e/fake_servers.py"
    spec = importlib.util.spec_from_file_location("bid_demo_fake_servers", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo_mcp(monkeypatch):
    from sdk import mcp_client
    module = load_fake_servers()
    monkeypatch.setattr(module, "METROLOGY_TIMEOUT_SECONDS", 1.2)
    entered = threading.Event()
    inner = module.build_metrology_server()

    async def observed(scope, receive, send):
        async def receive_request():
            message = await receive()
            if b'BJ0000' in message.get('body', b''):
                entered.set()
            return message
        await inner(scope, receive_request, send)

    port, server = module.start_server(observed)
    mcp_client.register_server_loader(lambda code: {
        "url": f"http://127.0.0.1:{port}/mcp", "headers": {},
        "timeout": 5, "read_timeout": 5,
    } if code == "demo-test" else None)
    yield module, mcp_client, entered
    server.should_exit = True
    mcp_client.register_server_loader(lambda code: None)


def test_fee_estimate_uses_fixture_fee_yuan_and_urgent_multiplier(demo_mcp):
    module, client, _ = demo_mcp
    record = next(r for r in module.METROLOGY_INSTRUMENTS if r.get("fee_yuan", 0) > 0)
    output = client.call_tool("demo-test", "query_fee_estimate", {
        "name": record["name"], "quantity": 3, "urgent": "是"})
    assert output["status"] == "ok"
    assert output["data"][0]["unit_fee_yuan"] == float(record["fee_yuan"])
    assert output["data"][0]["subtotal_yuan"] == round(float(record["fee_yuan"]) * 4.5, 2)


def test_timeout_order_does_not_block_other_mcp_requests(demo_mcp):
    module, client, entered = demo_mcp
    with ThreadPoolExecutor(max_workers=2) as pool:
        slow = pool.submit(client.call_tool, "demo-test", "query_progress", {"order_no": "BJ0000"})
        assert entered.wait(timeout=3), "timeout request never reached server"
        started = time.monotonic()
        output = client.call_tool("demo-test", "query_workload", {})
        elapsed = time.monotonic() - started
        assert output["status"] == "ok"
        assert elapsed < module.METROLOGY_TIMEOUT_SECONDS / 2
        assert slow.result()["status"] == "unavailable"
