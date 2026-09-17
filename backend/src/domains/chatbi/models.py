"""chatbi 插件共享数据模型 —— 自 chat-bi 移植的结构契约。

【移植来源】
  - SemanticModelContent 等 Pydantic 模型 ← chat-bi backend/app/schemas/semantic_layer.py
    (T012, SEM-002 spec; 字段 1:1 保留,含 source/confidence/Metric 注入防御校验)
  - DataSource 行结构 ← chat-bi backend/app/db/models.py DataSource/SemanticModel 表
    (SQLAlchemy → pack schema DDL;tenant 维度移除——宿主身份由引擎负责)

全插件各栈(语义层/图谱/检索/记忆/工具)统一 import 本模块的结构,
禁止各自另立结构。
"""
from __future__ import annotations

import re as _re
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# ── source 枚举(关系/字段的来源,影响人工复核优先级)─────────────
# 十六审 7.4 来源模型: db_comment(数据库注释) / manual_edit(管理员编辑,
# 兼容旧值 manual) / auto_inferred(LLM/规则推断) / foreign_key / name_pattern / ai_inferred
SourceStr = str  # 不用 Literal 锁死,允许扩展


class _Inferred(BaseModel):
    """带 source + confidence 的字段基类(所有可能被 AI 推断的字段继承)。"""
    model_config = ConfigDict(extra="forbid")

    source: SourceStr = Field(default="manual", description="字段来源")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0, description="置信度 0-1")


SemanticType = Literal["measure", "dimension", "key"]


class Column(_Inferred):
    """列定义。"""
    name: str
    display_name: str
    data_type: str = Field(description="数据库原始类型,如 DECIMAL(10,2)")
    semantic_type: SemanticType | None = None
    description: str | None = None


JoinType = Literal["INNER", "LEFT", "RIGHT", "FULL"]
Cardinality = Literal["N:1", "1:N", "1:1", "N:N"]


class Relationship(_Inferred):
    """表间关系(JOIN 依据;显式 relationship 让 Agent 直接读 JOIN 条件)。"""
    name: str
    target_model: str
    join_type: JoinType
    on: str = Field(description="JOIN ON 条件,如 orders.user_id = users.id")
    type: Cardinality = Field(description="基数 N:1/1:N/1:1/N:N")


MetricType = Literal["single", "composite"]

# F9 注入防御(供 API 层校验;schema 层不加校验以兼容已有数据)
_METRIC_DANGEROUS = _re.compile(r";|--|/\*|\*/", _re.IGNORECASE)
_DDL_DML_KEYWORDS = _re.compile(
    r"\b(DROP\s+(TABLE|DATABASE|SCHEMA)|TRUNCATE\s+\w+|ALTER\s+\w+|"
    r"EXEC(UTE)?\s+|INSERT\s+INTO|DELETE\s+FROM|UPDATE\s+\w+\s+SET)\b",
    _re.IGNORECASE,
)


class Metric(BaseModel):
    """指标定义(single=直接聚合公式;composite=子指标组合,SEM-005)。

    指标是数据模型的附属品,归表所有(GMV 属于 biz_orders)。
    """
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    formula: str
    type: MetricType = Field(default="single")
    condition: str | None = Field(default=None,
                                  description="过滤条件,如 status IN ('paid','shipped')")
    description: str | None = None
    factor_metric_names: list[str] | None = Field(
        default=None, description="仅 composite 必填:子指标名列表")
    co_occurrence: int = Field(default=0,
                               description="查询命中次数(运行时反哺递增,0=未命中)")
    source: SourceStr = Field(default="auto_inferred",
                              description="auto_inferred/manual/metric_suggestion")

    @model_validator(mode="after")
    def _composite_requires_factors(self) -> "Metric":
        if self.type == "composite":
            if not self.factor_metric_names:
                raise ValueError("composite metric 必须提供 factor_metric_names (SEM-005)")
        return self


class CalculatedField(BaseModel):
    """计算字段(行级表达式,区别于 Metric 的聚合)。"""
    model_config = ConfigDict(extra="forbid")

    name: str
    display_name: str
    formula: str


class Model(_Inferred):
    """语义层模型(对应一张表 + 它的列/关系/指标/计算字段)。"""
    name: str = Field(description="表名")
    display_name: str
    description: str | None = None
    columns: list[Column] = Field(default_factory=list)
    relationships: list[Relationship] = Field(default_factory=list)
    metrics: list[Metric] = Field(default_factory=list)
    calculated_fields: list[CalculatedField] = Field(default_factory=list)


class SemanticModelContent(BaseModel):
    """语义层 JSON 顶层结构(写入 chatbi_semantic_models.content)。"""
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, description="语义层 schema 版本")
    models: list[Model] = Field(default_factory=list)
    # 扫描时生成的示例问题(LLM 基于表/列/关系推断,前端空状态展示)
    sample_questions: list[str] = Field(default_factory=list)


# ── 数据源行结构(注册表 CRUD 与各栈共用的连接信息契约)──────────

@dataclass
class DataSourceInfo:
    """数据源连接信息(datasource_to_url/引擎/健康检查/扫描的统一入参)。

    password 以 Fernet 密文存储(security.encrypt_password),运行时解密使用;
    db_type: mysql | postgresql。
    """
    id: str = ""
    name: str = ""
    db_type: str = "postgresql"
    host: str = ""
    port: int = 5432
    database: str = ""
    username: str = ""
    password_plain: str = ""       # 运行时明文(解密后);序列化/落库前必须清空
    encrypted_password: str = ""
    is_active: bool = True
    scan_status: str = "idle"      # idle/scanning/done/failed
    scan_progress: int = 0
    scan_stage: str = ""
    scan_error: str = ""
    scanned_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def connection_kwargs(self) -> dict:
        """按 db_type 产出 psycopg3 / pymysql 的连接参数。"""
        return {
            "host": self.host, "port": self.port, "user": self.username,
            "password": self.password_plain, "database": self.database,
        }


# ── pack schema DDL(平台元数据表;业务数据源是外部库,不在这里)──────

CHATBI_DDL = [
    """CREATE TABLE IF NOT EXISTS chatbi_data_sources (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        db_type TEXT NOT NULL,
        host TEXT NOT NULL,
        port INTEGER NOT NULL,
        database TEXT NOT NULL,
        username TEXT NOT NULL,
        encrypted_password TEXT NOT NULL,
        is_active INTEGER NOT NULL DEFAULT 1,
        scan_status TEXT NOT NULL DEFAULT 'idle',
        scan_progress INTEGER NOT NULL DEFAULT 0,
        scan_stage TEXT DEFAULT '',
        scan_error TEXT DEFAULT '',
        scanned_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_ds_active ON chatbi_data_sources(is_active)",
    """CREATE TABLE IF NOT EXISTS chatbi_semantic_models (
        id TEXT PRIMARY KEY,
        data_source_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        content TEXT NOT NULL,
        is_current INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chatbi_semantic_version "
    "ON chatbi_semantic_models(data_source_id, version)",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_semantic_ds ON chatbi_semantic_models(data_source_id, is_current)",
    # 十七审 7.7: 索引 revision namespace 的 active 指针——读者按 scope 查
    # 当前生效分区(doc_id), 构建方先写新分区再原子翻转, 消灭 delete-first 空窗
    """CREATE TABLE IF NOT EXISTS chatbi_index_revisions (
        scope TEXT PRIMARY KEY,
        active_doc_id TEXT NOT NULL,
        version INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # 十八审 6.7: 索引构建台账——building/published/yielded 全记录,
    # 两代 grace 后按 doc_id 回收(含崩溃残余与让路半成品)
    """CREATE TABLE IF NOT EXISTS chatbi_index_builds (
        scope TEXT NOT NULL,
        version INTEGER NOT NULL,
        doc_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'building',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (scope, version)
    )""",
    # 十七审 7.6: merge 停用/冲突报告——refresh/rescan 落库后写入,
    # 语义页面经 GET /datasources/{id}/semantic/review 展示待复核清单
    """CREATE TABLE IF NOT EXISTS chatbi_merge_reports (
        data_source_id TEXT PRIMARY KEY,
        version INTEGER NOT NULL,
        report TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    # 十八审 6.4: 定时任务跨进程租约——refresh/health/purge 在多 worker
    # 部署下只有一个持有者执行(过期可被抢占)
    """CREATE TABLE IF NOT EXISTS chatbi_scheduler_leases (
        task_type TEXT PRIMARY KEY,
        holder TEXT NOT NULL,
        expires_at TEXT NOT NULL
    )""",
    # 十九审 6.1: chunk 身份反查表——超长 chunk_id 截断后业务身份
    # (type/name/owner_model)以 PG 为真源恢复, 不再依赖不可逆主键解码
    """CREATE TABLE IF NOT EXISTS chatbi_chunk_identities (
        scope TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        doc_id TEXT NOT NULL,
        type TEXT NOT NULL,
        name TEXT NOT NULL,
        owner_model TEXT,
        PRIMARY KEY (scope, chunk_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_chunk_ident_doc "
    "ON chatbi_chunk_identities(scope, doc_id)",
]


# ── M4: 保存查询 + 看板 (对标原系统 SavedQuery/Dashboard/DashboardWidget) ──

M4_DDL = [
    """CREATE TABLE IF NOT EXISTS chatbi_saved_queries (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        data_source_id TEXT NOT NULL,
        conversation_id TEXT,
        question TEXT NOT NULL,
        sql_text TEXT NOT NULL,
        sql_hash TEXT NOT NULL,
        result_summary TEXT,
        chart_config TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chatbi_sq_hash ON chatbi_saved_queries(user_id, data_source_id, sql_hash)",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_sq_user ON chatbi_saved_queries(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_sq_ds ON chatbi_saved_queries(data_source_id)",
    """CREATE TABLE IF NOT EXISTS chatbi_dashboards (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_dash_user ON chatbi_dashboards(user_id, updated_at DESC)",
    """CREATE TABLE IF NOT EXISTS chatbi_dashboard_widgets (
        id TEXT PRIMARY KEY,
        dashboard_id TEXT NOT NULL,
        question TEXT NOT NULL,
        query_sql TEXT,
        datasource_id TEXT NOT NULL,
        chart_type TEXT DEFAULT 'table',
        chart_option TEXT,
        position_x INTEGER DEFAULT 0,
        position_y INTEGER DEFAULT 0,
        width INTEGER DEFAULT 6,
        height INTEGER DEFAULT 4,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chatbi_w_dash ON chatbi_dashboard_widgets(dashboard_id, position_y, position_x)",
]
