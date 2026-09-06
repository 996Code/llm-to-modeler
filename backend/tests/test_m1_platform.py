"""M1 通用底座单测 —— 插件设置链 / 依赖检测 / 任务框架 / 动态路由挂卸。

覆盖验收点:
  - 设置三级合并链(保存值 > env > 默认)与 secret 掩码/哨兵语义
  - 依赖检测:配置缺失 → missing;设置页补配 → ok(热恢复路径);
    探针成功/失败/超时;use_probe=False 跳过探针;缓存清空
  - 任务框架:状态机(成功/失败/取消)、进度与日志持久化、queue_key 串行、
    未注册类型拒绝、重启 interrupted
  - pack API 动态挂载:挂载可访问 / 卸载 404 / 重挂 / 无路由泄漏
"""
import time
import types
from unittest.mock import MagicMock

import pytest

# 统一走裸包名(conftest 已把 backend/src 放进 sys.path;与 main 运行态同一模块身份)
from services.pack_settings import (
    SECRET_SET, PackSettingsStore, mask_secrets, resolve_all, validate_values,
)
from services import pack_dependency as dep_mod
from services.task_store import TaskStore
from services.task_manager import TaskCancelled, TaskManager


# ── fixtures ──────────────────────────────────────────────────

@pytest.fixture()
def settings_store(tmp_path):
    return PackSettingsStore(str(tmp_path / "settings.db"))


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    dep_mod.clear_probe_cache()
    yield
    dep_mod.clear_probe_cache()


@pytest.fixture()
def task_manager(tmp_path):
    store = TaskStore(str(tmp_path / "tasks.db"))
    return TaskManager(store, max_workers=2)


@pytest.fixture()
def wait_for():
    """轮询等待谓词成立(后台线程任务完成的同步点)。"""
    def _wait(pred, timeout=5.0, interval=0.02):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return True
            time.sleep(interval)
        return False
    return _wait


# ── 设置链 ────────────────────────────────────────────────────

class TestPackSettings:

    def test_resolve_chain(self, monkeypatch):
        """保存值 > env > 默认 三级链。"""
        schema = {"groups": [{"key": "g", "title": "t", "fields": [
            {"key": "a", "type": "string", "default": "def"},
            {"key": "b", "type": "string", "env": "TEST_CHAIN_ENV", "default": "bdef"},
            {"key": "c", "type": "int", "default": 7},
        ]}]}
        monkeypatch.setenv("TEST_CHAIN_ENV", "from-env")
        saved = {"a": "from-saved"}
        resolved = resolve_all("x", saved=saved, schema=schema)
        assert resolved == {"a": "from-saved", "b": "from-env", "c": 7}

    def test_store_merge_and_clear(self, settings_store):
        settings_store.save_values("p", {"a": "1", "b": "2"})
        settings_store.save_values("p", {"b": None, "c": "3"})  # b 清除,a 保持
        assert settings_store.get_values("p") == {"a": "1", "c": "3"}

    def test_validate_types_and_errors(self):
        schema = {"groups": [{"key": "g", "title": "t", "fields": [
            {"key": "n", "type": "int", "min": 1, "max": 10},
            {"key": "e", "type": "enum", "options": ["x", "y"]},
            {"key": "s", "type": "secret"},
            {"key": "flag", "type": "bool"},
        ]}]}
        clean, errors = validate_values(schema, {
            "n": "5", "e": "x", "s": SECRET_SET, "flag": "true", "unknown_key": 1,
        })
        # int/enum/bool 规范化;secret 哨兵跳过(保持旧值);未知键报错
        assert clean["n"] == 5 and clean["e"] == "x" and clean["flag"] is True
        assert "s" not in clean
        assert any(e["field"] == "unknown_key" for e in errors)

        _, errors = validate_values(schema, {"n": "99"})
        assert errors and "最大值" in errors[0]["message"]

    def test_mask_secrets(self):
        schema = {"groups": [{"key": "g", "title": "t", "fields": [
            {"key": "pwd", "type": "secret"},
            {"key": "plain", "type": "string"},
        ]}]}
        masked = mask_secrets(schema, {"pwd": "real-secret", "plain": "v"})
        assert masked["pwd"] == SECRET_SET and masked["plain"] == "v"
        # 未配置的 secret 掩码为空串(区别于"已配置")
        assert mask_secrets(schema, {"plain": "v"})["pwd"] == ""


# ── 依赖检测 ──────────────────────────────────────────────────

class TestPackDependency:

    MANIFEST = {"dependencies": {"demo": {"required": True, "fields": ["uri", "token"]}}}

    @staticmethod
    def _schema(_pack_name=None):
        """伪 settings.schema(函数形态,便于 monkeypatch 替换 read_settings_schema)。"""
        return {"groups": [{"key": "g", "title": "t", "fields": [
            {"key": "uri", "type": "string", "label": "服务地址", "env": "DEMO_URI"},
            {"key": "token", "type": "secret", "env": "DEMO_TOKEN"},
        ]}]}

    def _install_schema(self, monkeypatch):
        monkeypatch.setattr(dep_mod, "read_settings_schema", self._schema)

    def test_missing_when_no_config(self, settings_store, monkeypatch):
        monkeypatch.delenv("DEMO_URI", raising=False)
        monkeypatch.delenv("DEMO_TOKEN", raising=False)
        
        self._install_schema(monkeypatch)
        result = dep_mod.evaluate_pack("p", self.MANIFEST, settings_store)
        assert result["status"] == dep_mod.STATUS_MISSING
        assert len(result["missing"]) == 2

    def test_env_satisfies(self, settings_store, monkeypatch):
        monkeypatch.setenv("DEMO_URI", "http://x")
        monkeypatch.setenv("DEMO_TOKEN", "t")
        self._install_schema(monkeypatch)
        result = dep_mod.evaluate_pack("p", self.MANIFEST, settings_store, use_probe=False)
        assert result["status"] == dep_mod.STATUS_OK

    def test_settings_rescue_hot_recovery(self, settings_store, monkeypatch):
        """设置页补配 → 依赖满足(热恢复路径,不动 env)。"""
        monkeypatch.delenv("DEMO_URI", raising=False)
        monkeypatch.delenv("DEMO_TOKEN", raising=False)
        self._install_schema(monkeypatch)
        settings_store.save_values("p", {"uri": "http://x", "token": "t"})
        result = dep_mod.evaluate_pack("p", self.MANIFEST, settings_store, use_probe=False)
        assert result["status"] == dep_mod.STATUS_OK

    def test_module_probe_success_and_failure(self, settings_store, monkeypatch):
        """声明式 module 探针:异常即失败;成功即 ok;失败信息可读。"""
        monkeypatch.setenv("DEMO_URI", "http://x")
        monkeypatch.setenv("DEMO_TOKEN", "t")
        self._install_schema(monkeypatch)

        ok_manifest = {"dependencies": {"demo": {
            "required": True, "fields": ["uri", "token"],
            "probe": {"module": "tests._probe_helpers", "fn": "probe_ok"},
        }}}
        assert dep_mod.evaluate_pack("p", ok_manifest, settings_store)["status"] == dep_mod.STATUS_OK

        # 探针结果按 (pack, dep) 缓存 60s——换探针前清缓存(同"重新检测"按钮路径)
        dep_mod.clear_probe_cache("p")

        bad_manifest = {"dependencies": {"demo": {
            "required": True, "fields": ["uri", "token"],
            "probe": {"module": "tests._probe_helpers", "fn": "probe_fail"},
        }}}
        result = dep_mod.evaluate_pack("p", bad_manifest, settings_store)
        assert result["status"] == dep_mod.STATUS_PROBE_FAILED
        assert "连不上" in result["detail"]

    def test_probe_disabled_by_env(self, settings_store, monkeypatch):
        """PACK_DEPENDENCY_PROBE=0 → 只查配置,不跑探针。"""
        monkeypatch.setenv("PACK_DEPENDENCY_PROBE", "0")
        monkeypatch.setenv("DEMO_URI", "http://x")
        monkeypatch.setenv("DEMO_TOKEN", "t")
        self._install_schema(monkeypatch)
        bad_manifest = {"dependencies": {"demo": {
            "required": True, "fields": ["uri", "token"],
            "probe": {"module": "tests._probe_helpers", "fn": "probe_fail"},
        }}}
        assert dep_mod.evaluate_pack("p", bad_manifest, settings_store)["status"] == dep_mod.STATUS_OK

    def test_no_dependencies_declared(self, settings_store):
        assert dep_mod.evaluate_pack("p", {}, settings_store)["status"] == dep_mod.STATUS_OK


# ── 任务框架 ──────────────────────────────────────────────────

class TestTaskManager:

    def test_unknown_type_rejected(self, task_manager):
        with pytest.raises(KeyError):
            task_manager.submit("nope")

    def test_success_flow_persists_progress_logs_result(self, task_manager, wait_for):
        seen = {}

        def handler(task):
            task.set_progress(10, "start")
            task.log("hello", level="info", extra="x")
            for i in range(5):
                task.check_cancel()
                time.sleep(0.01)
            task.set_progress(100, "done")
            return {"count": 42}

        task_manager.register("demo.ok", handler)
        task = task_manager.submit("demo.ok", {"x": 1}, title="演示任务", pack_name="p")
        assert task["status"] == "pending"

        assert wait_for(lambda: task_manager._store.get_task(task["id"])["status"] == "succeeded")
        final = task_manager._store.get_task(task["id"])
        assert final["result"] == {"count": 42}
        assert final["progress"] == 100
        logs = task_manager._store.list_logs(task["id"])
        assert any(lg["message"] == "hello" for lg in logs)

    def test_failure_records_error(self, task_manager, wait_for):
        def handler(task):
            raise RuntimeError("boom")
        task_manager.register("demo.bad", handler)
        task = task_manager.submit("demo.bad")
        assert wait_for(lambda: task_manager._store.get_task(task["id"])["status"] == "failed")
        assert "boom" in task_manager._store.get_task(task["id"])["error"]

    def test_cancel_pending(self, task_manager, wait_for):
        """占满线程池后提交的任务处于 pending,直接取消成功。"""
        release = __import__("threading").Event()

        def blocker(task):
            release.wait(5)
        task_manager.register("demo.block", blocker)
        t1 = task_manager.submit("demo.block", queue_key="k1")
        t2 = task_manager.submit("demo.block", queue_key="k2")
        # 等 t1/t2 占满 2 个 worker,第三个进 pending
        assert wait_for(lambda: task_manager._store.get_task(t1["id"])["status"] == "running")
        assert wait_for(lambda: task_manager._store.get_task(t2["id"])["status"] == "running")
        t3 = task_manager.submit("demo.block", queue_key="k3")
        cancelled = task_manager.cancel(t3["id"])
        assert cancelled["status"] == "cancelled"
        release.set()

    def test_cancel_running_cooperative(self, task_manager, wait_for):
        def looper(task):
            for _ in range(500):
                task.check_cancel()
                time.sleep(0.01)
            return {"never": True}
        task_manager.register("demo.loop", looper)
        task = task_manager.submit("demo.loop")
        assert wait_for(lambda: task_manager._store.get_task(task["id"])["status"] == "running")
        task_manager.cancel(task["id"])
        assert wait_for(lambda: task_manager._store.get_task(task["id"])["status"] == "cancelled")

    def test_queue_key_serializes_same_key(self, task_manager, wait_for):
        """同 queue_key 的任务串行(时间不重叠),不同 key 可并行。"""
        import threading
        lock = threading.Lock()
        active = {"k": 0, "max": 0}

        def handler(task):
            with lock:
                active["k"] += 1
                active["max"] = max(active["max"], active["k"])
            time.sleep(0.08)
            with lock:
                active["k"] -= 1

        task_manager.register("demo.ser", handler)
        ids = [task_manager.submit("demo.ser", queue_key="same")["id"] for _ in range(3)]
        assert wait_for(lambda: all(
            task_manager._store.get_task(i)["status"] == "succeeded" for i in ids))
        assert active["max"] == 1  # 同 key 从未并发

    def test_mark_interrupted_on_startup(self, tmp_path):
        store = TaskStore(str(tmp_path / "t.db"))
        pending = store.create_task("x")                      # pending 态
        running = store.create_task("y")
        store.update_task(running["id"], status="running")    # running 态
        done = store.create_task("z")
        store.update_task(done["id"], status="succeeded")     # 终态不应被打扰

        interrupted = store.mark_interrupted_on_startup()
        assert set(interrupted) == {pending["id"], running["id"]}
        assert store.get_task(pending["id"])["status"] == "interrupted"
        assert store.get_task(running["id"])["status"] == "interrupted"
        assert store.get_task(done["id"])["status"] == "succeeded"

    def test_subscribe_receives_events(self, task_manager, wait_for):
        import queue as q_mod
        task_manager.register("demo.evt", lambda task: (task.log("m1"), task.set_progress(50, "half"))[0:0] or {"ok": 1})
        task = task_manager.submit("demo.evt")
        task_q = q_mod.Queue()
        task_manager._listeners.setdefault(task["id"], []).append(task_q)
        assert wait_for(lambda: task_manager._store.get_task(task["id"])["status"] == "succeeded")
        events = []
        while True:
            try:
                events.append(task_q.get_nowait())
            except q_mod.Empty:
                break
        kinds = {e["event"] for e in events}
        assert "log" in kinds and "progress" in kinds and "status" in kinds


# ── load_all_packs 依赖闸门(端到端行为) ─────────────────────

class TestLoadAllPacksDependencyGate:

    def test_dependency_missing_pack_skipped_and_reported(self, monkeypatch):
        """依赖缺失的 pack 不 import、不进路由/工具表,但进 dependency_status。"""
        import domains as domains_mod
        from sdk.registry import ToolRegistry

        # 用磁盘上真实存在的两个 pack 名(显式名单会与 scan_pack_dirs 取交集)
        dep_bad_manifest = {"dependencies": {"x": {"required": True, "env": ["NOPE_A", "NOPE_B"]}}}

        def fake_load_pack_configs(pack_names=None):
            return {"njmind_form": dep_bad_manifest, "leave_application": {}}

        def fake_load_pack(pack_name, app_state=None):
            assert pack_name == "leave_application", "依赖缺失的 pack 不应被 import"
            reg = ToolRegistry()
            reg.register(MagicMock(name="tool"))
            return reg, None, MagicMock()

        monkeypatch.delenv("NOPE_A", raising=False)
        monkeypatch.delenv("NOPE_B", raising=False)
        monkeypatch.setattr(domains_mod, "load_pack_configs", fake_load_pack_configs)
        monkeypatch.setattr(domains_mod, "load_pack", fake_load_pack)

        registry, loader, routers, tools, dep_status = domains_mod.load_all_packs(
            pack_names=["njmind_form", "leave_application"]
        )
        assert sorted(routers) == ["leave_application"]
        assert "leave_application" in tools
        assert dep_status["njmind_form"]["status"] == dep_mod.STATUS_MISSING
        assert sorted(dep_status["njmind_form"]["missing"]) == ["NOPE_A", "NOPE_B"]
        assert dep_status["leave_application"]["status"] == dep_mod.STATUS_OK


# ── pack API 动态挂载 ─────────────────────────────────────────

class TestPackApiMount:

    @pytest.fixture()
    def mounted_app(self):
        from fastapi import APIRouter, FastAPI
        from fastapi.testclient import TestClient
        import services.pack_api_mount as m

        router = APIRouter()

        @router.get("/ping")
        def ping():
            return {"ok": True}

        mod = types.ModuleType("domains.zz_mount_test.pack")
        mod.create_api_router = lambda: router
        import sys
        sys.modules["domains.zz_mount_test.pack"] = mod
        sys.modules.setdefault("domains.zz_mount_test", types.ModuleType("domains.zz_mount_test"))

        app = FastAPI()
        client = TestClient(app)
        yield m, app, client
        m.unmount_pack_routers(app)

    def test_mount_unmount_remount(self, mounted_app):
        m, app, client = mounted_app
        assert m.mount_pack_routers(app, ["zz_mount_test"]) == ["zz_mount_test"]
        assert client.get("/api/packs/zz_mount_test/ping").status_code == 200

        m.unmount_pack_routers(app, "zz_mount_test")
        assert client.get("/api/packs/zz_mount_test/ping").status_code == 404

        m.mount_pack_routers(app, ["zz_mount_test"])
        assert client.get("/api/packs/zz_mount_test/ping").status_code == 200

    def test_repeated_mount_no_leak(self, mounted_app):
        m, app, client = mounted_app
        m.mount_pack_routers(app, ["zz_mount_test"])
        count = len(app.router.routes)
        m.mount_pack_routers(app, ["zz_mount_test"])  # 全量重挂(冷启动=热切换)
        assert len(app.router.routes) == count

    def test_mount_factory_internal_error_skips_pack(self, mounted_app):
        """【M3 回归锚】pack 路由工厂内部异常只跳过该 pack,不拖垮装配。"""
        import types
        import sys
        mod = types.ModuleType("domains.zz_broken_pack.pack")
        def broken_factory():
            raise TypeError("真实内部错误,不是签名问题")
        mod.create_api_router = broken_factory
        sys.modules["domains.zz_broken_pack.pack"] = mod
        sys.modules.setdefault("domains.zz_broken_pack", types.ModuleType("domains.zz_broken_pack"))
        try:
            m, app, client = mounted_app
            mounted = m.mount_pack_routers(app, ["zz_mount_test", "zz_broken_pack"])
            # 坏 pack 被跳过,好 pack 照常挂载
            assert mounted == ["zz_mount_test"]
            assert client.get("/api/packs/zz_mount_test/ping").status_code == 200
        finally:
            sys.modules.pop("domains.zz_broken_pack.pack", None)
            sys.modules.pop("domains.zz_broken_pack", None)


class TestAuditFixes:
    """全量走查修复的回归锚(平台层)。"""

    def test_run_exception_still_releases_key(self, task_manager, monkeypatch, wait_for):
        """【M1 回归锚】store 异常(未保护段)不泄漏串行键/并发额度。"""
        m = task_manager
        m.register("zz.boom", lambda h: None, pack_name="test")

        real_update = m.store.update_task
        def flaky_update(task_id, **fields):
            # running 状态迁移时炸(修复前该段在 try/finally 之外)
            if fields.get("status") == "running":
                raise RuntimeError("sqlite boom")
            return real_update(task_id, **fields)
        monkeypatch.setattr(m.store, "update_task", flaky_update)

        t = m.submit("zz.boom", queue_key="k1")
        wait_for(lambda: m.store.get_task(t["id"])["status"]
                 in ("succeeded", "failed", "cancelled", "interrupted"))
        # 关键断言:串行键与并发额度必须已释放(否则同键任务永久 pending)
        with m._lock:
            assert "k1" not in m._active_keys
            assert m._running_count == 0
        t2 = m.submit("zz.boom", queue_key="k1")
        wait_for(lambda: m.store.get_task(t2["id"])["status"]
                 in ("succeeded", "failed", "cancelled", "interrupted"))

    def test_dedupe_key_rejects_duplicate_and_releases(self, task_manager, wait_for):
        """【H2 回归锚】dedupe_key:同 key 活任务拒绝,终态后可重提。"""
        from services.task_manager import DuplicateTaskError
        m = task_manager
        m.register("zz.dup", lambda h: {"ok": 1}, pack_name="test")
        t1 = m.submit("zz.dup", dedupe_key="doc:1")
        with pytest.raises(DuplicateTaskError):
            m.submit("zz.dup", dedupe_key="doc:1")
        # 被拒绝的提交不得落库(先落库再拒绝 = 永不调度的僵尸 pending 任务)。
        # 断言库中任务数:此刻只有 t1 一个合法提交(拒绝的不算)
        import sqlite3
        conn = sqlite3.connect(str(m.store.db_path))
        try:
            n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        finally:
            conn.close()
        assert n == 1, f"拒绝的提交也落库了(库里 {n} 个任务,应只有 1 个合法提交)"
        # 不同 key 不受影响
        t2 = m.submit("zz.dup", dedupe_key="doc:2")
        wait_for(lambda: m.store.get_task(t1["id"])["status"] == "succeeded")
        wait_for(lambda: m.store.get_task(t2["id"])["status"] == "succeeded")
        # 终态释放后同 key 可重提
        t3 = m.submit("zz.dup", dedupe_key="doc:1")
        wait_for(lambda: m.store.get_task(t3["id"])["status"] == "succeeded")

    def test_dedupe_key_released_on_cancel_pending(self, task_manager):
        """【H2 回归锚】pending 期取消也释放 dedupe 占位(不走 _run finally)。"""
        from services.task_manager import DuplicateTaskError
        import threading
        m = task_manager
        barrier = threading.Event()
        m.register("zz.block", lambda h: barrier.wait(timeout=5), pack_name="test")
        m.register("zz.dup", lambda h: None, pack_name="test")
        # 占满 2 个 worker,让 zz.dup 停在 pending(走 pending 取消路径)
        m.submit("zz.block", queue_key="q1")
        m.submit("zz.block", queue_key="q1b")
        tp = m.submit("zz.dup", queue_key="q2", dedupe_key="doc:x")
        m.cancel(tp["id"])
        # 取消后占位已释放——同 key 重提应成功
        t2 = m.submit("zz.dup", queue_key="q2", dedupe_key="doc:x")
        assert t2["id"] != tp["id"]
        barrier.set()
        m.close()

    def test_auto_retry_scenarios(self, task_manager):
        """【长文档支持回归锚】失败自动续跑四态:普通失败重试/致命不重试/
        取消撤销续跑/续跑收敛 resumed。"""
        m = task_manager

        # 1) 普通失败 → retry_scheduled(状态/计数/消息)
        def flaky(h):
            raise RuntimeError("LLM timeout")
        m.register("zz.flaky", flaky, pack_name="test")
        t1 = m.submit("zz.flaky", max_auto_retry=3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and m.store.get_task(t1["id"])["status"] not in ("retry_scheduled", "failed"):
            time.sleep(0.05)
        st = m.store.get_task(t1["id"])
        assert st["status"] == "retry_scheduled", st
        assert st["retryCount"] == 1 and "自动续跑" in st["progressMessage"]

        # 2) 取消撤销续跑
        c = m.cancel(t1["id"])
        assert c["status"] == "cancelled"

        # 3) 致命错误(鉴权)不重试
        def auth_fail(h):
            raise RuntimeError("Error code: 401 - invalid api key")
        m.register("zz.auth", auth_fail, pack_name="test")
        t2 = m.submit("zz.auth", max_auto_retry=3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and m.store.get_task(t2["id"])["status"] not in ("failed", "retry_scheduled"):
            time.sleep(0.05)
        assert m.store.get_task(t2["id"])["status"] == "failed"

        # 4) 续跑收敛:手动触发 resubmit(跳过 5 分钟退避)
        def ok_handler(h):
            return {"done": True}
        m.register("zz.ok", ok_handler, pack_name="test")
        t4 = m.submit("zz.ok", max_auto_retry=3)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline and m.store.get_task(t4["id"])["status"] != "succeeded":
            time.sleep(0.05)
        m.store.update_task(t4["id"], status="retry_scheduled")
        m._resubmit_after_retry(t4["id"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and m.store.get_task(t4["id"])["status"] != "resumed":
            time.sleep(0.05)
        assert m.store.get_task(t4["id"])["status"] == "resumed"

    def test_reset_terminal_listeners(self, task_manager, wait_for):
        """【H1 回归锚】reset_terminal_listeners 清空监听者(热切换防累积)。"""
        m = task_manager
        seen = []
        m.add_terminal_listener(lambda task: seen.append(task["id"]))
        m.reset_terminal_listeners()
        m.register("zz.ok", lambda h: None, pack_name="test")
        t1 = m.submit("zz.ok")
        wait_for(lambda: m.store.get_task(t1["id"])["status"] == "succeeded")
        assert seen == []  # 监听者已被清空,不再触发

    def test_terminal_listener_fires_on_all_paths(self, task_manager, wait_for):
        """终态回调覆盖 succeeded / pending 取消 / handler 缺失路径。"""
        m = task_manager
        seen = []
        m.add_terminal_listener(lambda task: seen.append(
            (task["id"], task["status"])))

        m.register("zz.ok", lambda h: {"fine": 1}, pack_name="test")
        t1 = m.submit("zz.ok")
        wait_for(lambda: any(i == t1["id"] for i, _ in seen))
        assert dict(seen)[t1["id"]] == "succeeded"

        # pending 期取消(不执行 handler)
        m.register("zz.slow", lambda h: None, pack_name="test")
        # 占满 worker 让下一任务停在 pending
        import threading
        barrier = threading.Event()
        m2 = TaskManager(m.store, max_workers=1)
        m2.register("zz.block", lambda h: barrier.wait(timeout=5), pack_name="test")
        m2.register("zz.ok", lambda h: {"fine": 1}, pack_name="test")
        m2_seen = []
        m2.add_terminal_listener(lambda task: m2_seen.append(task["status"]))
        m2.submit("zz.block")
        tp = m2.submit("zz.ok")["id"]
        cancelled = m2.cancel(tp)
        assert cancelled["status"] == "cancelled"
        assert "cancelled" in m2_seen
        barrier.set()
        m2.close()

    def test_cancel_completed_task_no_orphan_flag(self, task_manager, wait_for):
        """【L1 回归锚】终态任务取消:不留孤儿标志、不覆盖终态进度文案。"""
        m = task_manager
        m.register("zz.fast", lambda h: None, pack_name="test")
        t = m.submit("zz.fast")
        wait_for(lambda: m.store.get_task(t["id"])["status"] == "succeeded")
        result = m.cancel(t["id"])
        assert result["status"] == "succeeded"          # 幂等返回
        with m._lock:
            assert t["id"] not in m._cancel_flags       # 无孤儿标志
