"""UpstreamClient.resolve_base 请求级解析测试（唯一来源：宿主 services 表）。

固化解析语义：宿主下发即采用（含尾斜杠归一化）；未下发抛
ServiceUnresolvableError（fail-closed，不猜测地址）；服务表按线程隔离。
"""
import threading

import pytest

from services.upstream_client import (
    UpstreamClient, UpstreamConfig, ServiceUnresolvableError,
    set_request_services,
)


def _reset_thread_services():
    set_request_services(None)


def test_host_provided_base_used_directly():
    """宿主表提供该服务 → 直接采用（含尾斜杠归一化）。"""
    _reset_thread_services()
    client = UpstreamClient(config=UpstreamConfig())
    set_request_services({"njmind-modeler": "http://192.168.99.22/codeBack/"})
    assert client.resolve_base("njmind-modeler") == "http://192.168.99.22/codeBack"


def test_unprovided_service_raises():
    """宿主表为空 / 缺该 key → fail-closed 抛错，不猜测地址。"""
    _reset_thread_services()
    client = UpstreamClient(config=UpstreamConfig())
    # 表为空
    with pytest.raises(ServiceUnresolvableError, match="njmind-modeler"):
        client.resolve_base("njmind-modeler")
    # 表存在但缺该 key
    set_request_services({"other-service": "http://other:8000"})
    with pytest.raises(ServiceUnresolvableError, match="njmind-modeler"):
        client.resolve_base("njmind-modeler")


def test_has_service_follows_host_table_only():
    """has_service：宿主表有该服务 → True；没有 → False。"""
    _reset_thread_services()
    client = UpstreamClient(config=UpstreamConfig())
    assert client.has_service("njmind-modeler") is False
    set_request_services({"njmind-modeler": "http://host:1/codeBack"})
    assert client.has_service("njmind-modeler") is True
    assert client.has_service("other") is False


def test_services_are_thread_local():
    """服务地址表按线程隔离：其他线程的绑定不影响本线程。"""
    _reset_thread_services()
    client = UpstreamClient(config=UpstreamConfig())

    def _bind_in_other_thread():
        set_request_services({"njmind-modeler": "http://other-thread:9"})
        # 子线程内可解析
        assert client.resolve_base("njmind-modeler") == "http://other-thread:9"

    t = threading.Thread(target=_bind_in_other_thread)
    t.start()
    t.join()
    # 主线程未绑定 → fail-closed
    with pytest.raises(ServiceUnresolvableError):
        client.resolve_base("njmind-modeler")
