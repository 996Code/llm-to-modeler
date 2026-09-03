"""leave_application 依赖的上游服务名（与 config.yaml services 声明同源）。

地址解析走通用机制（upstream_client.resolve_base）：宿主 services 表按请求
下发，未下发则 fail-closed 报错。
"""
SERVICE_NAME = "leave-system"
