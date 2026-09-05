"""scope_registry 单测 —— 前缀登记冲突/scope 签发契约。"""
import pytest

from sdk.scope_registry import (
    PrefixConflictError, is_scope_id_safe, new_scope_id,
    register_prefix, registered_prefixes, unregister_prefix,
)


class TestPrefixRegistry:
    def test_register_and_idempotent(self):
        register_prefix("zz_test", "pack_a")
        register_prefix("zz_test", "pack_a")          # 同 owner 幂等
        assert registered_prefixes()["zz_test"] == "pack_a"

    def test_conflict_raises(self):
        register_prefix("zz_conflict", "pack_a")
        with pytest.raises(PrefixConflictError, match="pack_b"):
            register_prefix("zz_conflict", "pack_b")

    def test_unregister_only_own(self):
        register_prefix("zz_own", "pack_a")
        unregister_prefix("zz_own", "other")           # 他人注销被忽略
        assert "zz_own" in registered_prefixes()
        unregister_prefix("zz_own", "pack_a")
        assert "zz_own" not in registered_prefixes()
        # 注销后他人可登记(热切换重装场景)
        register_prefix("zz_own", "pack_b")

    def test_invalid_prefix_rejected(self):
        for bad in ["", "a-b", "a b", "中文", "A!"]:
            with pytest.raises(ValueError):
                register_prefix(bad, "pack_x")


class TestScopeId:
    def test_new_scope_id_is_uuid(self):
        sid = new_scope_id()
        assert is_scope_id_safe(sid)

    def test_user_input_rejected(self):
        # 契约:用户可控值(库名/任意字符串)不是合法 scope_id
        for bad in ["我的库", "kb-001", "../../etc", "a" * 40, "", "00000000-0000-0000-0000-00000000000g"]:
            assert not is_scope_id_safe(bad)
        # 标准 UUID 形态合法(服务端生成的)
        assert is_scope_id_safe("00000000-0000-0000-0000-000000000000")
