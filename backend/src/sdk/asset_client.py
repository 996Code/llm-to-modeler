"""AssetClient 抽象 — 资产/数据来源的统一抽象接口。

【模块定位】
这是 pack(工具包)与上游系统(模板服务、表单配置服务、业务 API)之间的
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

【方法分两类】
1. 表单配置类操作(get_template / get_schema / validate_artifact /
   persist_artifact 等):抽象方法,子类必须实现。
2. 通用数据操作(submit_data / query_data):非抽象,默认抛
   NotImplementedError,子类按需覆写 —— 因为纯配置类 pack 不需要这些,
   而数据类 pack(如 leave_application)才需要。

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
    """资产来源的抽象基类。

    【职责】
    定义 pack 与上游系统交互的统一契约:取模板、取 schema、取 guide、
    校验制品、持久化制品,以及通用的数据提交/查询。

    【设计模式】
    - 抽象基类(ABC):Python 用 abc.ABC + @abstractmethod 强制子类实现,
      等价 Java 的 abstract class + abstract 方法;未实现的抽象方法被实例化
      时会直接抛 TypeError(类似 Java 不允许 new 抽象类)。
    - 模板方法 + 钩子:部分方法(get_form / submit_data / query_data)
      提供默认"未实现"实现,子类选择性覆写,而不是强制全部实现。

    【Java 类比】
    相当于:
        public abstract class AssetClient {
            public abstract Map<String,Object> getTemplate(String name);
            ... // 强制实现的抽象方法
            public Map<String,Object> submitData(...) {
                throw new UnsupportedOperationException("...");
            }
        }
    """

    # ── 表单配置类操作(原有,抽象方法强制实现) ──

    @abstractmethod
    def get_template(self, name: str) -> dict:
        """取模板 JSON。

        Args:
            name: 模板标识名。

        Returns:
            模板内容(dict,直接来自上游 JSON 解析结果)。

        Note:
            子类必须实现(abstract)。返回内容进入 prompt 前需经 sanitize_obj 清洗。
        """

    @abstractmethod
    def list_templates(self) -> list[str]:
        """列出所有可用模板名。

        Returns:
            模板名字符串列表。供 LLM 或前端展示"可选模板"。
        """

    @abstractmethod
    def get_schema(self, name: str) -> dict:
        """取 JSON Schema(用于校验表单结构)。

        Args:
            name: schema 标识名。

        Returns:
            JSON Schema(dict 形式)。
        """

    @abstractmethod
    def get_guide(self) -> dict:
        """取 guide.json(填表指引、字段说明等辅助生成的内容)。

        Returns:
            guide 内容(dict)。该内容会拼进 prompt 辅助 LLM 生成合规表单。
        """

    def get_form(self, form_code: str) -> Optional[dict]:
        """根据 formCode 查询已有表单配置。

        用于 modify / clone 类工具:先查回现有配置,再在其基础上修改,
        避免让 LLM 从零重建整个表单。

        Args:
            form_code: 表单唯一标识(上游系统的主键)。

        Returns:
            表单配置 dict;不存在时返回 None(由调用方决定是否追问用户)。

        Note:
            默认实现抛 NotImplementedError —— 这是一个"钩子"方法,非强制实现:
            纯数据类 pack 用不到,配置类 pack 用 HttpAssetClient 提供的实现。
            子类按需覆写。

        【Java 类比】
        等价 Java interface 里的 default 方法抛 UnsupportedOperationException:
        子类不实现也能编译通过,只有真调用到才报错。
        """
        # self.__class__.__name__ 取运行时子类类名,放进错误信息便于定位。
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 get_form; "
            "如需查询表单请覆写此方法或使用 HttpAssetClient"
        )

    @abstractmethod
    def validate_artifact(self, artifact: dict, mode: str) -> dict:
        """校验制品(生成的表单配置)是否符合上游规则。

        Args:
            artifact: 待校验的制品(表单配置 dict)。
            mode: 校验模式,"create"(新建)或 "update"(更新),
                  两者可能走不同校验规则(如 update 要求 formCode 已存在)。

        Returns:
            dict,固定结构 {valid: bool, errors: list, warnings: list}。
            - valid: 是否通过
            - errors: 阻断性错误列表(不通过则不能持久化)
            - warnings: 非阻断警告(可继续但提示用户)

        Note:
            abstract 方法,子类必须实现。
        """

    @abstractmethod
    def persist_artifact(self, artifact: dict, mode: str) -> dict:
        """持久化制品到上游(真正写入表单配置)。

        Args:
            artifact: 待持久化的制品(表单配置 dict)。
            mode: "create"(新建)或 "update"(更新),决定走 POST 还是 PUT 语义。

        Returns:
            dict,至少包含 {success: bool, ...},上游会带回 ID 等附加信息。

        Note:
            abstract 方法,子类必须实现。这是写操作,上游应有幂等/事务保护。
        """

    # ── 通用数据操作(插件化扩展,钩子方法) ──

    def submit_data(self, path: str, data: dict, headers: dict = None) -> dict:
        """提交数据到上游指定路径。

        为非配置类插件(如 leave_application)提供通用的"写"出口:
        pack 不再直接调 httpx,而是统一走这里,从而保证清洗/透传/重试一致。

        Args:
            path: 上游 API 路径(如 "/api/leave/submit")。
            data: 提交的数据体(会被序列化为 JSON)。
            headers: 额外请求头(典型场景:嵌入模式透传的 forward_headers,
                如鉴权 token、租户标识等,需原样带到上游)。

        Returns:
            上游返回的 JSON(已解析为 dict)。

        Raises:
            NotImplementedError: 默认实现抛出。子类(HttpAssetClient)按需覆写。

        Note:
            默认实现抛 NotImplementedError,纯配置类 pack 无需实现此方法。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 submit_data; "
            "如需提交数据请覆写此方法或使用 HttpAssetClient"
        )

    def query_data(self, path: str, params: dict = None, headers: dict = None) -> dict:
        """查询上游数据。

        为非配置类插件提供通用的"读"出口。

        Args:
            path: 上游 API 路径(如 "/api/leave/status")。
            params: 查询参数(会被拼成 query string)。
            headers: 额外请求头(典型场景:嵌入模式透传的 forward_headers)。

        Returns:
            上游返回的 JSON(已解析为 dict)。

        Raises:
            NotImplementedError: 默认实现抛出。子类(HttpAssetClient)按需覆写。

        Note:
            默认实现抛 NotImplementedError,纯配置类 pack 无需实现此方法。
            返回内容进入 prompt 前仍需经 sanitize_obj 清洗。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} 未实现 query_data; "
            "如需查询数据请覆写此方法或使用 HttpAssetClient"
        )
