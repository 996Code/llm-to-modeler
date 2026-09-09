"""AssetClient 抽象 — 资产/数据来源的统一抽象接口。

【模块定位】
这是 pack(工具包)与上游系统(资产服务、制品配置服务、业务 API)之间的
"中间层抽象"。所有对上游的读取(取模板/schema/guide/校验/持久化)和数据
读写(提交/查询业务数据)都必须通过 AssetClient,pack 不直接接触 HTTP 客户端。

【为什么抽象】
- 解耦:pack 只依赖接口,不关心上游是 HTTP 还是本地文件或 mock。
- 统一横切关注点:通用实现 HttpAssetClient(在 adapters/ 目录,阶段 1 实现)
  集中处理 sanitize 清洗、forward_headers 透传、连接池/重试/超时,
  避免每个工具各写一遍 httpx 调用(Java 类比:类似 Repository 模式 + RestTemplate 封装)。
- 可测试:工具单测时可注入 FakeAssetClient,无需起真实上游服务。

【安全约定 / Fail-Closed】(阶段 1 强化)
所有 get_* 方法返回的内容,在进入 prompt 前必须经过 Unicode 清洗
(sdk.sanitize.sanitize_obj),防止上游数据携带零宽字符 / 方向反转字符等
隐写指令。这是 prompt injection 防御的硬约束,见 sdk/sanitize.py。

【方法分三组】
1. 读资产(get_template / get_schema / get_guide / list_templates):
   静态配置数据,供 LLM 生成参考;抽象方法。
2. 制品操作(validate_artifact / persist_artifact / get_artifact):
   校验/落库预留/按标识查询;抽象方法。
3. 通用数据(submit_data / query_data + has_service):
   非配置类插件的读写出入口 + 地址可用性探测;非抽象,默认抛
   NotImplementedError,子类按需覆写。

【扩展(插件化阶段)】
- submit_data / query_data:通用数据提交/查询,供非配置类插件使用。
  pack 不再直接调 httpx,统一走 AssetClient,保证:
  1. sanitize_obj 清洗  2. forward_headers 传播  3. 连接池/重试/超时统一

【Java 类比】
对标 Spring 的 Repository 接口或 JdbcTemplate 的抽象层:
- ABC + @abstractmethod 等价 Java interface 的抽象方法。
- 提供默认抛 UnsupportedOperationException 的方法,等价 Java 8 interface
  的 default 方法 —— 子类按需覆写,不强制实现。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class AssetClient(ABC):
    """资产/数据来源的统一契约基类(SDK 通用层)。

    【两层拆分】
    - AssetClient(本类):通用数据契约——submit_data/query_data/
      has_service,任何插件(配置类/数据类)都只用得上这三个;
      其余方法为"未实现即抛"的钩子,不强制。
    - ConfigAssetClient(子类):配置制品业务契约——模板/schema/guide/
      校验/持久化六件事,abstractmethod 强制。配置类插件
      (njmind_form 等)的上游实现继承它;数据类插件(如 leave_application)
      只依赖本类,不必为六个用不到的方法背空壳实现。

    工具侧一律经 ctx.asset_client(鸭子类型)访问,标注哪个层
    由插件自行决定(数据类插件不该声明配置契约)。
    """

    # ── 通用数据契约(数据类插件的核心依赖;默认钩子) ──

    def has_service(self, service_name: str) -> bool:
        """该上游服务当前是否可解析出地址(宿主 services 表有该服务)。

        供工具 preflight 钩子做执行前提校验(fail-fast)。
        默认 True(非 HTTP 实现或测试桩不限制;HTTP 实现覆写为真实判定)。
        """
        return True

    def submit_data(self, path: str, data: dict, service_name: str,
                    headers: dict = None) -> dict:
        """提交数据到指定上游服务的相对路径(POST)。

        为非配置类插件(如数据提交类 pack)提供通用的"写"出口:
        pack 不再直接调 httpx,而是统一走这里,从而保证清洗/透传/重试一致。
        地址解析与配置类操作同一套(宿主 services 表按请求下发,未下发
        fail-closed,见 upstream_client.resolve_base)。

        Args:
            path: 相对该服务 base 的 API 路径(如 "/api/items")。
            data: 提交的数据体(会被序列化为 JSON)。
            service_name: pack manifest 声明的上游服务名(决定 base)。
            headers: 额外请求头(典型场景:嵌入模式透传的 forward_headers,
                如鉴权 token、租户标识等,需原样带到上游)。

        Returns:
            上游返回的 JSON(已解析为 dict)。

        Raises:
            NotImplementedError: 默认实现抛出。子类(HttpAssetClient)按需覆写。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 submit_data; "
            "如需提交数据请覆写此方法或使用 HttpAssetClient"
        )

    def query_data(self, path: str, service_name: str, params: dict = None,
                   headers: dict = None) -> dict:
        """查询上游数据(GET)。

        为非配置类插件提供通用的"读"出口。地址解析与 submit_data 同一套。

        Args:
            path: 相对该服务 base 的 API 路径(如 "/api/items/{id}")。
            service_name: pack manifest 声明的上游服务名(决定 base)。
            params: 查询参数(会被拼成 query string)。
            headers: 额外请求头(典型场景:嵌入模式透传的 forward_headers)。

        Returns:
            上游返回的 JSON(已解析为 dict)。

        Raises:
            NotImplementedError: 默认实现抛出。子类(HttpAssetClient)按需覆写。

        Note:
            返回内容进入 prompt 前仍需经 sanitize_obj 清洗。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 query_data; "
            "如需查询数据请覆写此方法或使用 HttpAssetClient"
        )

    # ── 配置制品契约(钩子形态;强制形态见 ConfigAssetClient) ──

    def get_template(self, name: str) -> dict:
        """取模板 JSON(配置类插件用;返回内容进 prompt 前需 sanitize)。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 get_template; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )

    def list_templates(self) -> list[str]:
        """列出所有可用模板名(配置类插件用)。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 list_templates; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )

    def get_schema(self, name: str) -> dict:
        """取 JSON Schema(配置类插件用,用于校验制品结构)。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 get_schema; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )

    def get_guide(self) -> Optional[dict]:
        """取 guide.json(生成指引;拼进 prompt 辅助 LLM 生成合规制品)。"""
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 get_guide; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )

    def get_artifact(self, entry_id: str) -> Optional[dict]:
        """按标识查询已有制品配置(增量修改/复制类工具的基线来源)。

        Args:
            entry_id: 制品唯一标识(上游系统的主键)。

        Returns:
            制品配置 dict;不存在时返回 None(由调用方决定是否追问用户)。

        Note:
            钩子方法,非强制:纯数据类 pack 用不到。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 get_artifact; "
            "如需查询制品请覆写此方法或使用 HttpAssetClient"
        )

    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验制品是否符合上游规则。

        Args:
            artifact: 待校验的制品(配置 dict)。
            mode: "create"(新建)或 "update"(更新),两者可能走不同校验规则。

        Returns:
            dict,固定结构 {valid: bool, errors: list, warnings: list}。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 validate_artifact; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )

    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """持久化制品到上游(写操作,上游应有幂等/事务保护)。

        Args:
            artifact: 待持久化的制品(配置 dict)。
            mode: "create"(新建)或 "update"(更新)。

        Returns:
            dict,至少包含 {success: bool, ...},上游会带回 ID 等附加信息。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 persist_artifact; "
            "配置类插件请继承 ConfigAssetClient 或使用 HttpAssetClient"
        )


class ConfigAssetClient(AssetClient):
    """配置制品类插件的上游契约(强制形态)。

    在通用 AssetClient 之上把模板/schema/guide/校验/持久化五件事
    升为 abstractmethod——配置类插件的上游实现(如 HttpAssetClient)
    继承本类,忘实现任何一个都会在实例化时报 TypeError(fail-fast)。
    get_artifact 保持钩子(并非所有配置流都需要按标识回查)。
    """

    @abstractmethod
    def get_template(self, name: str) -> dict:
        """取模板 JSON(继承 AssetClient 契约说明;此处升为强制)。"""

    @abstractmethod
    def list_templates(self) -> list[str]:
        """列出所有可用模板名(此处升为强制)。"""

    @abstractmethod
    def get_schema(self, name: str) -> dict:
        """取 JSON Schema(此处升为强制)。"""

    @abstractmethod
    def get_guide(self) -> Optional[dict]:
        """取 guide.json(此处升为强制)。"""

    @abstractmethod
    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验制品(此处升为强制;返回 {valid, errors, warnings})。"""

    @abstractmethod
    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """持久化制品(此处升为强制;返回 {success, ...})。"""
