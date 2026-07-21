"""AssetClient 抽象 — 资产来源的抽象接口。

pack 用它取模板/schema/guide/校验/持久化,不关心是 HTTP 还是本地。
通用实现 HttpAssetClient 在 adapters/(阶段 1 实现)。

安全约定(阶段 1 强化):所有 get_* 方法返回的内容在进入 prompt 前
必须经过 Unicode 清洗(sdk.sanitize.sanitize_obj),防止上游数据
携带零宽字符/方向反转字符等隐写指令。

扩展(插件化阶段):
- submit_data / query_data: 通用数据提交/查询,供非配置类插件使用。
  pack 不再直接调 httpx,统一走 AssetClient,保证:
  1. sanitize_obj 清洗  2. forward_headers 传播  3. 连接池/重试/超时统一
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class AssetClient(ABC):
    """资产来源的抽象。"""

    # ── 表单配置类操作(原有) ──

    @abstractmethod
    def get_template(self, name: str) -> dict:
        """取模板 JSON。"""

    @abstractmethod
    def list_templates(self) -> list[str]:
        """列出所有模板名。"""

    @abstractmethod
    def get_schema(self, name: str) -> dict:
        """取 JSON Schema。"""

    @abstractmethod
    def get_guide(self) -> dict:
        """取 guide.json。"""

    @abstractmethod
    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验制品。mode ∈ {"create", "update"}。
        返回 {valid: bool, errors: list, warnings: list}。"""

    @abstractmethod
    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """持久化制品到上游。mode ∈ {"create", "update"}。
        返回 {success: bool, ...}。"""

    # ── 通用数据操作(插件化扩展) ──

    def submit_data(self, path: str, data: dict, headers: dict = None) -> dict:
        """提交数据到上游指定路径。

        Args:
            path: 上游 API 路径(如 "/api/leave/submit")
            data: 提交的数据体
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON(dict)

        默认实现抛 NotImplementedError,子类按需覆写。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 submit_data; "
            "如需提交数据请覆写此方法或使用 HttpAssetClient"
        )

    def query_data(self, path: str, params: dict = None, headers: dict = None) -> dict:
        """查询上游数据。

        Args:
            path: 上游 API 路径(如 "/api/leave/status")
            params: 查询参数
            headers: 额外请求头(如 forward_headers)

        Returns:
            上游返回的 JSON(dict)

        默认实现抛 NotImplementedError,子类按需覆写。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 query_data; "
            "如需查询数据请覆写此方法或使用 HttpAssetClient"
        )
