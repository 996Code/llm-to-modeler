"""PackState 单元测试 —— 启停状态的来源优先级 / 持久化 / 清洗。"""
import json
from pathlib import Path

import pytest

from src.services.pack_state import PackState

PACKS = ["njmind_form", "leave_application"]


def make_state(tmp_path, env=None, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setenv("PACKS_ENABLED", env or "")
        if not env:
            monkeypatch.delenv("PACKS_ENABLED", raising=False)
    return PackState(str(tmp_path / "pack_state.json"), PACKS)


def test_default_all_enabled(tmp_path, monkeypatch):
    """无文件 + 无 env → 全部启用(向后兼容)。"""
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    st = make_state(tmp_path)
    assert st.enabled_names() == set(PACKS)
    assert st.source == "all"


def test_env_as_initial_default(tmp_path, monkeypatch):
    """无文件 + env 已配置 → env 作为初始默认。"""
    monkeypatch.setenv("PACKS_ENABLED", "njmind_form")
    st = make_state(tmp_path)
    assert st.enabled_names() == {"njmind_form"}
    assert st.source == "env"
    assert not Path(st.state_path).exists()  # 只有切换操作才落盘


def test_file_overrides_env(tmp_path, monkeypatch):
    """状态文件存在 → 文件优先,env 不再生效。"""
    monkeypatch.setenv("PACKS_ENABLED", "njmind_form")
    path = tmp_path / "pack_state.json"
    path.write_text(json.dumps({"version": 1, "enabled": ["leave_application"]}), encoding="utf-8")
    st = PackState(str(path), PACKS)
    assert st.enabled_names() == {"leave_application"}
    assert st.source == "file"


def test_toggle_persists_and_survives_restart(tmp_path, monkeypatch):
    """切换 → 落盘;新实例(模拟重启)从文件还原。"""
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    path = tmp_path / "pack_state.json"
    st = PackState(str(path), PACKS)
    assert st.set_enabled("leave_application", False) is True
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["enabled"] == ["njmind_form"]

    reborn = PackState(str(path), PACKS)  # 重启后
    assert reborn.enabled_names() == {"njmind_form"}
    assert reborn.source == "file"


def test_toggle_noop_not_persisted(tmp_path, monkeypatch):
    """状态未变化的切换不落盘、返回 False。"""
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    path = tmp_path / "pack_state.json"
    st = PackState(str(path), PACKS)
    assert st.set_enabled("njmind_form", True) is False
    assert not path.exists()


def test_stale_names_cleaned(tmp_path, monkeypatch):
    """文件引用了磁盘上已删除的 pack → 交集清洗,不报错。"""
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    path = tmp_path / "pack_state.json"
    path.write_text(json.dumps({"version": 1, "enabled": ["njmind_form", "ghost"]}), encoding="utf-8")
    st = PackState(str(path), PACKS)
    assert st.enabled_names() == {"njmind_form"}


def test_unknown_pack_toggle_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    st = make_state(tmp_path)
    with pytest.raises(KeyError):
        st.set_enabled("no_such_pack", True)


def test_corrupted_file_falls_back_to_default(tmp_path, monkeypatch):
    """损坏的 JSON → 告警并按默认重新初始化(不抛异常)。"""
    monkeypatch.delenv("PACKS_ENABLED", raising=False)
    path = tmp_path / "pack_state.json"
    path.write_text("{not-json", encoding="utf-8")
    st = PackState(str(path), PACKS)
    assert st.enabled_names() == set(PACKS)
