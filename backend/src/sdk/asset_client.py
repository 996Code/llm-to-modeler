"""AssetClient 抽象 — 资产来源的抽象接口。

pack 用它取模板/schema/guide/校验/持久化,不关心是 HTTP 还是本地。
通用实现 HttpAssetClient 在 adapters/(阶段 1 实现)。

安全约定(阶段 1 强化):所有 get_* 方法返回的内容在进入 prompt 前
必须经过 Unicode 清洗(sdk.sanitize.sanitize_obj),防止上游数据
携带零宽字符/方向反转字符等隐写指令。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class AssetClient(ABC):
    """资产来源的抽象。"""

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
