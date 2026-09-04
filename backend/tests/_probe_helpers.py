"""依赖检测探针的测试辅助模块(manifest 里 module 路径指向这里)。"""


def probe_ok(settings: dict) -> None:
    """永远成功的探针。"""
    return None


def probe_fail(settings: dict) -> None:
    """永远失败的探针(抛异常 = 连不上)。"""
    raise ConnectionError("模拟: 服务连不上")
