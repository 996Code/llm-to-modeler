"""ChatBI 评测集回归 —— 表命中 recall@5 + SQL 骨架表名集断言。

评测形态参照 RAGFlow benchmark（doc/RAGFlow项目学习/笔记/09 §2）：
evalset.json 驱动 + recall 自算（不引 ranx）。夹具复用
test_chatbi_retrieval.py 的 FakeLLM/FakeSdkVector（import 复用；
tests.domains.test_chatbi_pipeline 已有跨测试文件 import 先例）。

Task 2 范围：六表语义模型夹具 + 内存检索栈冒烟（纯内存：
显式 scope 直连，无 db / 无 Milvus / 无真实 LLM）。
Task 3 范围：EVALSET 20 条驱动的 recall@5 / 表名集断言，消费本文件的
make_eval_fixture / SIX_TABLE_MODELS（接口见文件底部注释或 task-2-report）。

列名口径：以 evalset.json 各用例 expected_sql_skeleton 出现的列名为基准
（跨任务对齐裁决），确保检索文本能命中 + Task 3 骨架断言列名可用。
"""
import json
import re
from pathlib import Path

from domains.chatbi.indexing import build_index, model_to_text
from domains.chatbi.models import Column, Model, SemanticModelContent
from domains.chatbi.retrieval import retrieve
from domains.chatbi.stores import ChatBIEmbedder, ChatBIVectorStore

from tests.domains.test_chatbi_retrieval import FakeLLM, FakeSdkVector

EVALSET = json.loads(
    (Path(__file__).parent / "testdata" / "chatbi" / "evalset.json").read_text(
        encoding="utf-8"))

# 评测数据源 id（进 record metadata；scope 直连时仅作追溯标签）
EVAL_DS_ID = "ds-eval-0000"
# 显式 scope（UUID 形态，与 test_chatbi_retrieval 的直连测试同模式；
# build_index/retrieve 均支持 scope= 参数绕过 db 解析）
EVAL_SCOPE = "44444444-4444-4444-4444-444444444444"


# —— 六表业务域语义模型（中文 description/display_name = 检索文本核心）——
# 注意：Model 无 comment 字段，中文表注释进 description（model_to_text
# 会重复 2 遍增强语义）；Column 无 comment/is_primary_key，中文列注释进
# display_name（model_to_text 只取列的中文 display_name 进检索文本）。

def _col(name, cn, dtype="VARCHAR"):
    """列定义：英文名 + 中文 display_name（中文即列注释语义）。"""
    return Column(name=name, display_name=cn, data_type=dtype)


SIX_TABLE_MODELS = [
    Model(
        name="employee", display_name="员工表",
        description="员工基本信息：姓名、所属部门、入职日期、邮箱",
        columns=[
            _col("id", "员工编号", "BIGINT"),
            _col("name", "姓名"),
            _col("dept_id", "部门ID", "BIGINT"),
            _col("hire_date", "入职日期", "DATE"),
            _col("email", "邮箱"),
        ],
    ),
    Model(
        name="department", display_name="部门表",
        description="部门信息：部门名称、部门编号",
        columns=[
            _col("id", "部门编号", "BIGINT"),
            _col("name", "部门名称"),
        ],
    ),
    Model(
        name="salary", display_name="薪资表",
        description="员工薪资记录：工资金额、发放月份",
        columns=[
            _col("id", "主键", "BIGINT"),
            _col("emp_id", "员工ID", "BIGINT"),
            _col("amount", "工资金额", "DECIMAL"),
            _col("pay_month", "发放月份", "DATE"),
        ],
    ),
    Model(
        name="orders", display_name="订单表",
        description="销售订单记录：订单号、客户、产品、金额、数量、下单时间",
        columns=[
            _col("id", "主键", "BIGINT"),
            _col("order_no", "订单号"),
            _col("customer_id", "客户ID", "BIGINT"),
            _col("product_id", "产品ID", "BIGINT"),
            _col("amount", "订单金额", "DECIMAL"),
            _col("quantity", "购买数量", "INT"),
            _col("created_at", "下单时间", "TIMESTAMP"),
        ],
    ),
    Model(
        name="customer", display_name="客户表",
        description="客户基本信息：客户名称、联系电话、负责人、所属区域",
        columns=[
            _col("id", "客户编号", "BIGINT"),
            _col("name", "客户名称"),
            _col("phone", "联系电话"),
            _col("owner_id", "负责人ID", "BIGINT"),
            _col("region", "所属区域"),
        ],
    ),
    Model(
        name="product", display_name="产品表",
        description="产品信息：产品名称、产品类别、单价",
        columns=[
            _col("id", "产品编号", "BIGINT"),
            _col("name", "产品名称"),
            _col("category", "产品类别"),
            _col("price", "单价", "DECIMAL"),
        ],
    ),
]


# —— 假向量方案（同 FakeLLM.embeddings 的文本→假向量映射模式）——
# 6 表各占一维正交基（8 维向量空间，与 test_chatbi_retrieval 的假向量
# 同维度约定）；查询问题向量按 expected_tables 混合对应维度，保证
# 向量召回阶段能命中所指表（余弦相似度 > 0.35 阈值）。
_TABLE_DIM = {
    "employee": 0, "department": 1, "salary": 2,
    "orders": 3, "customer": 4, "product": 5,
}
_DIM = 8


def _table_vector(table_name):
    """表名 → 正交基向量（索引文本向量）。"""
    v = [0.0] * _DIM
    v[_TABLE_DIM[table_name]] = 1.0
    return v


def _query_vector(table_names):
    """期望表集合 → 均匀混合向量（查询向量）。

    单表: cos=1.0；k 表混合: cos(q, 任一期望表)=1/√k ≥ 0.35
    (k ≤ 6 时最小 0.408)；非期望表正交 cos=0 被阈值滤掉。
    """
    v = [0.0] * _DIM
    for t in table_names:
        v[_TABLE_DIM[t]] = 1.0
    return v


def register_query_vector(llm, question, table_names):
    """把评测问题的期望表向量注册进 FakeLLM（Task 3 逐用例调用）。"""
    llm.vector_map[question] = _query_vector(table_names)


def make_eval_fixture():
    """构建内存检索栈：六表索引 + 假 embedder + Fake 向量库。

    Returns:
        (store, embedder, llm):
          store: ChatBIVectorStore(FakeSdkVector()) —— 内存余弦检索
          embedder: ChatBIEmbedder(FakeLLM) —— 文本级缓存 + 假向量
          llm: FakeLLM —— vector_map 已注册六表索引文本的假向量；
              查询问题向量需调用方经 register_query_vector 注册
              （skip_llm_refine=True 时精筛不触发，refine 可不脚本化）

    接线要点：build_index/retrieve 均用显式 scope=EVAL_SCOPE 直连，
    不需要 db（无 PG / 无 Milvus / 无真实 LLM 的纯内存链路）。
    """
    llm = FakeLLM()
    # 索引文本 → 该表的正交基向量（精确文本映射，embed 出站即命中）
    for m in SIX_TABLE_MODELS:
        llm.vector_map[model_to_text(m)] = _table_vector(m.name)
    store = ChatBIVectorStore(FakeSdkVector())
    embedder = ChatBIEmbedder(llm)
    content = SemanticModelContent(models=SIX_TABLE_MODELS)
    result = build_index(content, EVAL_DS_ID, store, embedder, scope=EVAL_SCOPE)
    assert result.indexed_count == len(SIX_TABLE_MODELS), (
        f"六表索引未全部落库: {result.indexed_count}/{len(SIX_TABLE_MODELS)}"
        f" (error={result.error})")
    return store, embedder, llm


# ── Task 2: 夹具冒烟（检索链路纯内存可跑性验证）──────────────────

def test_fixture_smoke():
    store, embedder, llm = make_eval_fixture()
    register_query_vector(llm, "查询所有员工的名字", ["employee"])
    result = retrieve("查询所有员工的名字", store, embedder,
                      skip_llm_refine=True, scope=EVAL_SCOPE)
    assert result.models, f"夹具召回为空: {result.no_match_reason}"
    assert result.models[0]["name"] == "employee"


def test_fixture_all_tables_reachable():
    """六表各自的单表查询都能命中自己（正交基方案的逐表验证）。"""
    store, embedder, llm = make_eval_fixture()
    for m in SIX_TABLE_MODELS:
        q = f"查一下{m.display_name}的数据"
        register_query_vector(llm, q, [m.name])
        result = retrieve(q, store, embedder, skip_llm_refine=True,
                          scope=EVAL_SCOPE)
        hit = [x["name"] for x in result.models]
        assert m.name in hit, f"{m.name} 未被召回: {hit} ({result.no_match_reason})"


# ── Task 3: recall@5 基线 + SQL 骨架比对（管线回归护栏）──────────

def recall_at_k(expected: set, retrieved: list, k: int = 5) -> float:
    """期望表集合在 retrieved 前 k 名中的命中率（|期望∩top-k|/|期望|）。"""
    if not expected:
        return 1.0
    top = set(retrieved[:k])
    return len(expected & top) / len(expected)


def test_evalset_recall_baseline(capsys):
    """EVALSET 20 条 recall@5 基线断言（阶段 1 纯向量召回，skip_llm_refine）。

    基线定位=管线回归护栏（错即掉 0，防检索链路回归），非检索质量度量：
    正交基假向量方案下非期望表 cos=0 被 0.35 阈值滤除，recall 恒优——
    本数字只用于拦截 embed→索引→召回→阈值过滤链路的回归（任一环节
    断裂即掉 0），不是检索质量（embed 模型/真实语义匹配）的度量。
    """
    store, embedder, llm = make_eval_fixture()
    scores = []
    misses = []
    for case in EVALSET:
        register_query_vector(llm, case["query"], case["expected_tables"])
        result = retrieve(case["query"], store, embedder,
                          skip_llm_refine=True, scope=EVAL_SCOPE)
        got = [m["name"] for m in result.models]
        r = recall_at_k(set(case["expected_tables"]), got, 5)
        scores.append(r)
        if r < 1.0:
            misses.append((case["id"], case["expected_tables"], got[:5]))
    mean = sum(scores) / len(scores)
    print(f"\n=== ChatBI 检索基线 recall@5={mean:.3f} (n={len(scores)}) ===")
    print("基线定位=管线回归护栏（错即掉 0，防检索链路回归），非检索质量度量")
    for mid, exp, got in misses:
        print(f"  MISS {mid}: 期望{exp} 实得{got}")
    # 基线门槛：假 embedder 语义映射下 easy 全中，总体 ≥0.70
    assert mean >= 0.70, f"基线 {mean:.3f} 低于 0.70：{misses}"


# —— SQL 骨架比对（阶段 2 LLM 精筛替身 → 按召回表生成 SQL 骨架）——

def table_names_from_skeleton(sql: str) -> set:
    """FROM/JOIN 后的表名集合（大小写不敏感，忽略别名）。

    注意只取紧跟 FROM/JOIN 的第一个标识符（如 `orders o` 取 orders），
    遇 subquery 的 FROM 也按同规则提取（本评测集骨架无子查询）。
    """
    return set(t.lower() for t in re.findall(
        r"(?:FROM|JOIN)\s+([a-zA-Z_][a-zA-Z0-9_]*)", sql, re.IGNORECASE))


class SqlGeneratingFakeLLM(FakeLLM):
    """LLM 精筛替身：按召回候选表名生成 plausible SQL（精筛+生成一体）。

    对齐 retrieval._llm_refine 的真实契约（已读 parse_json_response 确认）：
      - 精筛走 llm.chat()（非 chat_json）→ 返回文本被
        parse_json_response 解析；
      - 期望格式 {"models": [...], "reason": ...}（精筛 prompt 末行约定），
        数组格式 retrieval 亦兼容（已防御）。

    retrieve 管线内只有精筛这一次 LLM 调用；真实链路的 SQL 生成在
    askData（精筛表名 → schema_context → LLM 生成 SQL），不在本管线。
    本替身把"按召回表名生成 plausible SQL"折叠进精筛调用（brief 设计：
    精筛替身+骨架记录），评测的是"召回表名 → 精筛选择 → SQL 骨架"
    的链路连通性，非 LLM 生成质量。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.last_generated_sql = ""

    def chat(self, messages, temperature=None, max_tokens=None,
             conv_id=None, stage=None, model=None):
        content = messages[0]["content"] if messages else ""
        # 候选行格式 "N. name=xxx type=... score=... desc=..."（retrieval
        # _llm_refine 拼装）→ 提取表名全选（正交基方案召回即期望）；
        # 同表集生成骨架 SQL 记入 last_generated_sql（模拟下游 askData
        # 的 SQL 生成——retrieve 管线内只有精筛这一次 chat 调用）
        names = sorted(set(re.findall(r"name=(\S+)", content)))
        if names:
            sql = "SELECT * FROM " + names[0]
            for t in names[1:]:
                sql += f" JOIN {t} ON 1=1"
            self.last_generated_sql = sql
        return json.dumps({"models": names,
                           "reason": "评测替身：召回候选全选并生成骨架"})


def test_evalset_sql_skeleton(capsys):
    """EVALSET 前 10 条全流程（召回→LLM 精筛替身）SQL 骨架表名集比对。

    基线定位同 recall 基线：管线回归护栏，非检索/生成质量度量——
    精筛与 SQL 生成为确定性替身，骨架表名命中率衡量"召回表名能
    正确流经精筛选择→SQL 生成骨架"的链路连通性。
    """
    store, embedder, llm = make_eval_fixture()
    llm.__class__ = SqlGeneratingFakeLLM  # make_eval_fixture 返回的替身升级
    llm.last_generated_sql = ""
    hits = 0
    checked = 0
    for case in EVALSET[:10]:  # 骨架比对先跑前 10 条（全量留 P1#5 扩容）
        register_query_vector(llm, case["query"], case["expected_tables"])
        result = retrieve(case["query"], store, embedder, llm=llm,
                          scope=EVAL_SCOPE)
        if not result.models:
            continue
        checked += 1
        gen_sql = llm.last_generated_sql  # 替身记录的生成 SQL
        expected_tables = table_names_from_skeleton(case["expected_sql_skeleton"])
        got_tables = table_names_from_skeleton(gen_sql)
        hit = bool(expected_tables & got_tables)
        hits += hit
        print(f"  {case['id']}: 期望{sorted(expected_tables)}"
              f" 生成{sorted(got_tables)} {'HIT' if hit else 'MISS'}")
    print(f"\n=== SQL 骨架表名命中率 {hits}/{checked} ===")
    assert checked > 0, "前 10 条全部未召回，骨架比对无从进行"
    assert hits >= 6, f"骨架命中 {hits}/{checked} <60%"
