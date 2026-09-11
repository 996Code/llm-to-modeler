"""knowledge_graph M3 导入流水线单测 —— mock LLM + 内存图/向量存储。

覆盖:成功全流程 / 幂等跳过 / force 重导清理 / chunk 级断点续跑 /
连续失败熔断 / 并发导入守卫 / 向量模式启用与降级 / 本体归纳任务。
真实 Neo4j/Milvus/LLM 的端到端由脚本化验收完成(不走 pytest)。
"""
import time
from types import SimpleNamespace

import pytest

from domains.knowledge_graph import runtime, tasks
from domains.knowledge_graph.store import KGStore
from services.pack_settings import PackSettingsStore
from services.task_manager import TaskManager
from services.task_store import TaskStore


# ── 测试替身 ─────────────────────────────────────────────────

class MockLLM:
    """按 chunk 文本内容脚本化的 LLM:提取出文中 [E:名] 标记的实体。

    fail_markers:prompt 含这些子串时抛错(制造单块失败);
    induce_result:本体归纳的固定返回。
    """

    def __init__(self):
        self.fail_markers: list = []
        self.extract_calls = 0
        self.embed_calls = 0
        # 默认"相关"(general 模板里无 domain/range 约束);测试端点约束
        # 校验时把 rel_type 改成带约束的类型制造违例(如 person→person 任职于)
        self.rel_type = "相关"
        self.induce_result = {
            "entity_types": [{"key": "widget", "label": "部件", "description": "d",
                              "examples": ["w1"]}],
            "relation_types": [{"key": "part_of", "label": "属于", "description": "d",
                                "domain": ["widget"], "range": ["widget"]}],
        }

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
        self.models_seen = getattr(self, "models_seen", [])
        if stage == "kg.extract" and model:
            self.models_seen.append(model)
        content = messages[0]["content"]
        if stage == "kg.extract":
            for marker in self.fail_markers:
                if marker in content:
                    raise RuntimeError(f"mock 抽取失败(命中标记 {marker})")
            self.extract_calls += 1
            import re
            names = re.findall(r"\[E:([^\]]+)\]", content)
            entities = [{"name": n, "type": "person", "description": "", "aliases": []}
                        for n in names]
            relations = []
            if len(names) >= 2:
                relations.append({"source": names[0], "target": names[1],
                                  "type": self.rel_type, "description": "", "evidence": ""})
            return {"entities": entities, "relations": relations}
        if stage == "kg.induce_schema":
            return self.induce_result
        return {}

    def embeddings(self, texts, conv_id=None, stage=None):
        self.embed_calls += 1
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeGraph:
    """内存图存储(覆盖 tasks.py 用到的全部方法)。"""

    def __init__(self):
        self.nodes = {}        # (kb, normalized) -> node
        self.edges = []        # dicts with kb/doc_id/source/target/type
        self.delete_calls = []

    def upsert_batch(self, kb_id, doc_id, entities, relations):
        import datetime
        now = datetime.datetime.utcnow().isoformat()
        for e in entities:
            key = (kb_id, e["normalized_name"])
            node = self.nodes.setdefault(key, {
                "id": f"{kb_id}:{e['normalized_name']}", "name": e["name"],
                "normalized_name": e["normalized_name"], "type": e.get("type"),
                "source_docs": [], "source_chunks": [],
                "aliases": list(e.get("aliases", [])),
                "description": e.get("description", ""),
                "created_at": now,
                "type_status": e.get("type_status", "approved"),
            })
            if e.get("aliases"):
                node["aliases"] = list(dict.fromkeys(node.get("aliases", []) + e["aliases"]))
            if doc_id not in node["source_docs"]:
                node["source_docs"].append(doc_id)
            for cid in (e.get("chunk_ids") or [e.get("chunk_id") or ""]):
                if cid and cid not in node["source_chunks"]:
                    node["source_chunks"].append(cid)
        for r in relations:
            self.edges.append({**r, "kb": kb_id, "doc_id": doc_id})
        return {"entities": len(entities), "relations": len(relations)}

    def list_entity_names(self, kb_id):
        return {k[1] for k in self.nodes if k[0] == kb_id}

    def list_entity_aliases(self, kb_id):
        out = {}
        for (kbid, norm), n in self.nodes.items():
            if kbid == kb_id:
                out[norm] = {"type": n.get("type") or "",
                             "aliases": list(n.get("aliases", [])),
                             "description": n.get("description", "")}
        return out

    def prune_aliases(self, kb_id, names):
        removed = 0
        for (kbid, norm), n in self.nodes.items():
            if kbid != kb_id:
                continue
            before = len(n.get("aliases", []))
            n["aliases"] = [a for a in n.get("aliases", []) if a not in names]
            removed += before - len(n["aliases"])
        return removed

    def list_entities(self, kb_id):
        out = []
        for (kbid, norm), n in self.nodes.items():
            if kbid == kb_id:
                out.append({"normalized": norm, "name": n.get("name", norm),
                            "type": n.get("type") or "",
                            "aliases": list(n.get("aliases", [])),
                            "source_chunks": list(n.get("source_chunks", [])),
                            "description": n.get("description", ""),
                            "created_at": n.get("created_at", "")})
        return out

    def list_connected_pairs(self, kb_id):
        out = set()
        for r in self.edges:
            if r.get("kb") != kb_id or r["source"] == r["target"]:
                continue
            out.add((r["source"], r["target"]))
            out.add((r["target"], r["source"]))
        return out

    def merge_entities(self, kb_id, doc_id, pairs):
        merged = 0
        for p in pairs:
            canon = p["canonical"]; frag = p["fragment"]
            ck, fk = (kb_id, canon), (kb_id, frag)
            if ck not in self.nodes or fk not in self.nodes:
                continue
            c = self.nodes[ck]; f = self.nodes[fk]
            # 碎片名 + 碎片别名都并入 canonical(碎片名不保留,检索就丢了)
            c["aliases"] = list(dict.fromkeys(
                [a for a in ([frag] + f.get("aliases", []) + c.get("aliases", []))
                 if a != canon]))
            c["source_docs"] = list(dict.fromkeys(
                c.get("source_docs", []) + f.get("source_docs", [])))
            c["source_chunks"] = list(dict.fromkeys(
                c.get("source_chunks", []) + f.get("source_chunks", [])))
            # canonical↔fragment 间边是合并后的自环,删;其余重定向
            self.edges = [e for e in self.edges
                          if not (e["kb"] == kb_id
                                  and {e.get("source"), e.get("target")} == {frag, canon})]
            for e in self.edges:
                if e["kb"] != kb_id:
                    continue
                if e.get("source") == frag:
                    e["source"] = canon
                if e.get("target") == frag:
                    e["target"] = canon
            del self.nodes[fk]
            merged += 1
        return {"merged": merged}

    def chunk_output(self, kb_id, chunk_id):
        ents = [{"name": n["name"], "type": n["type"]}
                for (kb, _), n in self.nodes.items()
                if kb == kb_id and chunk_id in n.get("source_chunks", [])]
        rels = [{"source": e["source"], "target": e["target"], "type": e["type"],
                 "description": e.get("description", ""), "evidence": e.get("evidence", "")}
                for e in self.edges if e["kb"] == kb_id and e.get("chunk_id") == chunk_id]
        return {"entities": ents, "relations": rels}

    def delete_document(self, kb_id, doc_id):
        self.delete_calls.append(doc_id)
        before_e = len(self.nodes)
        self.edges = [e for e in self.edges if not (e["kb"] == kb_id and e["doc_id"] == doc_id)]
        for key in list(self.nodes):
            node = self.nodes[key]
            if key[0] == kb_id and doc_id in node["source_docs"]:
                node["source_docs"].remove(doc_id)
                if not node["source_docs"]:
                    del self.nodes[key]
        return {"edges": 0, "orphanEntities": before_e - len(self.nodes)}

    def delete_scope(self, kb_id):
        return self.delete_kb(kb_id)

    def delete_kb(self, kb_id):
        self.nodes = {k: v for k, v in self.nodes.items() if k[0] != kb_id}
        self.edges = [e for e in self.edges if e["kb"] != kb_id]
        return {}

    def counts(self, kb_id):
        # 关系按 (source,type,target) 去重(平行边=块级留痕,与 Neo4j 实现同语义)
        uniq = {(e["source"], e.get("type"), e["target"])
                for e in self.edges if e["kb"] == kb_id}
        return {"entities": sum(1 for k in self.nodes if k[0] == kb_id),
                "relations": len(uniq)}

    def document_counts(self, kb_id, doc_id):
        return {
            "entities": sum(1 for n in self.nodes.values()
                            if n["id"].startswith(kb_id) and doc_id in n["source_docs"]),
            "relations": sum(1 for e in self.edges
                             if e["kb"] == kb_id and e["doc_id"] == doc_id),
        }


class FakeVector:
    def __init__(self):
        self.collections = {}   # kb -> dim
        self.rows = {}          # kb -> {chunk_id: row}
        self.deleted_docs = []

    def ensure_collection(self, kb_id, dim):
        self.collections.setdefault(kb_id, dim)

    def drop_collection(self, kb_id):
        self.collections.pop(kb_id, None); self.rows.pop(kb_id, None)

    def upsert_chunks(self, kb_id, items):
        self.rows.setdefault(kb_id, {}).update({i["chunk_id"]: i for i in items})
        return len(items)

    def delete_by_doc(self, kb_id, doc_id):
        self.deleted_docs.append(doc_id)
        self.rows[kb_id] = {k: v for k, v in self.rows.get(kb_id, {}).items()
                            if v["doc_id"] != doc_id}

    def existing_chunk_ids(self, kb_id, doc_id):
        return {cid for cid, r in self.rows.get(kb_id, {}).items()
                if r["doc_id"] == doc_id}

    def count(self, kb_id):
        return len(self.rows.get(kb_id, {}))


# ── fixtures ─────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    """临时环境:独立 SQLite + 文件目录 + 小参数配置 + 纯图谱模式。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "kg.db"))
    monkeypatch.setenv("KG_FILES_DIR", str(tmp_path / "files"))
    monkeypatch.delenv("LLM_EMBED_MODEL", raising=False)
    runtime.reset_runtime_cache()
    import services.task_manager as tm_mod
    _ = tm_mod  # noqa

    settings = PackSettingsStore(str(tmp_path / "settings.db"))
    settings.save_values("knowledge_graph", {
        "chunk_target_chars": 100, "chunk_overlap_chars": 0, "chunk_max_chars": 300,
        "llm_batch_size": 2, "llm_concurrency": 2, "llm_max_retries": 0,
        "failure_threshold": 5, "glossary_top_k": 100,
        # 测试默认关闭失败自动续跑:续跑有 5 分钟退避,会让失败类测试
        # 停在 retry_scheduled 而非终态(续跑语义由框架层测试单独覆盖)
        "import_max_auto_retry": 0,
    })

    fake_graph, fake_vector, llm = FakeGraph(), FakeVector(), MockLLM()
    monkeypatch.setattr(runtime, "get_graph", lambda state: fake_graph)
    monkeypatch.setattr(runtime, "get_vector", lambda state: fake_vector)

    task_store = TaskStore(str(tmp_path / "tasks.db"))
    manager = TaskManager(task_store, max_workers=2)
    app_state = SimpleNamespace(
        llm_client=llm, settings_store=settings, task_manager=manager,
    )
    tasks.register_tasks(manager, app_state)

    yield SimpleNamespace(
        store=runtime.get_kg_store(app_state), settings=settings, graph=fake_graph,
        vector=fake_vector, llm=llm, manager=manager, app_state=app_state,
        tmp_path=tmp_path,
    )
    manager.close()
    runtime.reset_runtime_cache()
    tasks._app_state = None


def _wait(manager, task_id, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = manager.store.get_task(task_id)
        if t["status"] in ("succeeded", "failed", "cancelled", "interrupted"):
            return t
        time.sleep(0.02)
    raise AssertionError("任务未在超时内完成")


def _make_doc(env, name: str, paragraphs: list):
    from domains.knowledge_graph.schema_templates import get_template_schema
    kb = env.store.create_kb(f"库-{name}", schema_json=get_template_schema("general"),
                             schema_template="general")
    content = "\n\n".join(paragraphs).encode("utf-8")
    doc = env.store.create_document(
        kb["id"], f"{name}.md", "text/markdown", len(content),
        "", __import__("hashlib").sha256(content).hexdigest())
    p = env.tmp_path / "files" / kb["id"]
    p.mkdir(parents=True, exist_ok=True)
    f = p / f"{doc['id']}.md"
    f.write_bytes(content)
    env.store.update_document(doc["id"], file_path=str(f))
    return kb, doc


# ── 用例 ─────────────────────────────────────────────────────

def _extract_alias_llm(fake_llm, alias_map: dict):
    """把 MockLLM 改造成支持 alias 输出的脚本:name 若在 alias_map 中,
    返回该 name 且 aliases 带其别名;用于构造"张小凡/鬼厉 分块抽取"。
    """
    orig = fake_llm.chat_json

    def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
        content = messages[0]["content"]
        if stage != "kg.extract":
            return orig(messages, temperature=temperature, conv_id=conv_id,
                        stage=stage, model=model)
        import re
        names = re.findall(r"\[E:([^\]]+)\]", content)
        entities = []
        for n in names:
            aliases = alias_map.get(n, [])
            entities.append({"name": n, "type": "person", "description": "",
                             "aliases": aliases})
        relations = []
        if len(names) >= 2:
            relations.append({"source": names[0], "target": names[1],
                              "type": "相关", "description": "", "evidence": ""})
        return {"entities": entities, "relations": relations}

    fake_llm.chat_json = chat_json.__get__(fake_llm, type(fake_llm))
    return fake_llm


class TestImportPipeline:

    def test_success_flow(self, env):
        kb, doc = _make_doc(env, "成功", [
            "# 章节\n[E:甲] 和 [E:乙] 是同事。",
            "后来 [E:丙] 也加入了。",
            "再后来 [E:丁] 离职了。",
        ])
        task = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        final = _wait(env.manager, task["id"])
        assert final["status"] == "succeeded", final["error"]
        assert final["result"]["status"] == "succeeded"
        assert final["result"]["entities"] == 4      # 甲乙丙丁
        assert final["result"]["relations"] >= 1     # 至少一章的任职于
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "succeeded" and d["entityCount"] == 4
        assert all(c["status"] == "done" for c in env.store.list_chunks(doc["id"]))
        # 幂等清理先跑了一次(首次导入 = 空操作清理)
        assert env.graph.delete_calls == [doc["id"]]
        # 词表生效:后续批次的 prompt 应含前批实体名(通过抽取调用数间接验证)
        assert env.llm.extract_calls == len(env.store.list_chunks(doc["id"]))

    def test_cross_batch_entity_disambiguation(self, env):
        """批次间实体消歧:张小凡 先被抽出(带别名 鬼厉),后续批次又抽出
        鬼厉 为独立实体——应并回张小凡,而不是另立节点。"""

        def chat_json(self, messages, temperature=None, conv_id=None, stage=None, model=None):
            content = messages[0]["content"]
            if stage != "kg.extract":
                return {}
            import re
            names = re.findall(r"\[E:([^\]]+)\]", content)
            entities = []
            for n in names:
                # 第一章:张小凡 带别名 鬼厉;第二章:鬼厉(碎片);
                # 第三章:林惊羽 带别名 惊羽;第四章:惊羽(碎片)
                if n == "张小凡":
                    aliases = ["鬼厉"]
                elif n == "林惊羽":
                    aliases = ["惊羽"]
                else:
                    aliases = []
                entities.append({"name": n, "type": "person", "description": "",
                                 "aliases": aliases})
            relations = []
            if len(names) >= 2:
                relations.append({"source": names[0], "target": names[1],
                                  "type": "相关", "description": "", "evidence": ""})
            return {"entities": entities, "relations": relations}

        env.llm.chat_json = chat_json.__get__(env.llm, type(env.llm))
        # 每章垫长到 > chunk_target(100):确保各章独立成块——否则相邻短章
        # 打包进同一块,鬼厉/曾书书 与 张小凡 同块,关系会变成合并后的自环
        pad = "夜色沉沉,山风掠过竹林。" * 12
        kb, doc = _make_doc(env, "消歧", [
            f"# 第一章\n[E:张小凡] 在青云门修行。{pad}",
            f"# 第二章\n[E:鬼厉] 与 [E:曾书书] 对饮。{pad}",
            f"# 第三章\n[E:林惊羽] 与张小凡并肩。{pad}",
            f"# 第四章\n[E:惊羽] 持剑而立。{pad}",
        ])
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded", t["error"]
        # 鬼厉 被并回张小凡(别名合并,不是新节点)
        nodes = env.graph.nodes
        keys = [k[1] for k in nodes if k[0] == kb["id"]]
        assert "张小凡" in keys and "鬼厉" not in keys, keys
        # 惊羽 也被收尾对账并回 林惊羽(同 person + 别名互指)
        assert "林惊羽" in keys and "惊羽" not in keys, keys
        # 张小凡 的别名已含 鬼厉
        zxf = nodes[(kb["id"], "张小凡")]
        assert "鬼厉" in zxf.get("aliases", []), zxf
        # 关系边重定向:鬼厉-曾书书 的关系现在挂到 张小凡-曾书书;
        # canonical↔碎片 之间的边(合并后是自环)被删除
        edges = [e for e in env.graph.edges if e["kb"] == kb["id"]]
        assert all(e["source"] != "鬼厉" and e["target"] != "鬼厉" for e in edges)
        assert any(e["source"] == "张小凡" and e["target"] == "曾书书" for e in edges), edges
        assert all(e["source"] != e["target"] for e in edges), edges
        # 收尾对账日志有"消歧"标记
        logs = env.manager.store.list_logs(t["id"])
        assert any("消歧" in (l.get("message") or "") for l in logs)

    def test_same_batch_alias_merge(self):
        """收尾对账纯函数:同类型+别名指认 → 合并;canonical 取最早入库;
        不同类型(青云山/青云门)绝不并。"""
        class MiniGraph:
            def __init__(self):
                self.nodes = {}
                self.edges = []
            def list_entities(self, kb):
                return [{"normalized": k, "name": k, "type": v["type"],
                         "aliases": list(v["aliases"]),
                         "source_chunks": [], "created_at": v["created_at"]}
                        for k, v in self.nodes.items()]
            def merge_entities(self, kb, doc, pairs):
                m = 0
                for p in pairs:
                    c, f = p["canonical"], p["fragment"]
                    if c not in self.nodes or f not in self.nodes:
                        continue
                    self.nodes[c]["aliases"] = list(dict.fromkeys(
                        self.nodes[c]["aliases"] + self.nodes[f]["aliases"] + [f]))
                    del self.nodes[f]
                    for e in self.edges:
                        if e[0] == f: e[0] = c
                        if e[1] == f: e[1] = c
                    m += 1
                return {"merged": m}

        g = MiniGraph()
        # 张小凡(t1 入库,别名鬼厉)↔ 鬼厉(t2 入库,别名张小凡):互指同型,
        # canonical 取最早入库的张小凡
        g.nodes = {
            "张小凡": {"type": "person", "aliases": ["鬼厉"], "created_at": "t1"},
            "鬼厉": {"type": "person", "aliases": ["张小凡"], "created_at": "t2"},
            "林惊羽": {"type": "person", "aliases": ["惊羽"], "created_at": "t3"},
            "惊羽": {"type": "person", "aliases": [], "created_at": "t4"},
            # 不同类型:互为别名也不并(山 vs 门)
            "青云山": {"type": "location", "aliases": ["青云门"], "created_at": "t5"},
            "青云门": {"type": "organization", "aliases": ["青云山"], "created_at": "t6"},
        }
        g.edges = [["鬼厉", "林惊羽"]]
        n = tasks._merge_same_type_alias_pairs(g, "kb", "d1")
        assert n == 2  # 鬼厉→张小凡、惊羽→林惊羽(青云山/青云门 类型不同,不并)
        assert set(g.nodes) == {"张小凡", "林惊羽", "青云山", "青云门"}
        # canonical 方向:保留最早入库的张小凡,鬼厉 进别名
        assert "鬼厉" in g.nodes["张小凡"]["aliases"]
        # 关系边重定向
        assert g.edges == [["张小凡", "林惊羽"]]
        # 幂等:再跑一遍无新合并
        assert tasks._merge_same_type_alias_pairs(g, "kb", "d1") == 0

    def _reconcile_mini_graph(self):
        class MiniGraph:
            def __init__(self):
                self.nodes = {}
                self.merged_pairs = []

            def list_entities(self, kb):
                return [{"normalized": k, "name": k, "type": v["type"],
                         "aliases": list(v["aliases"]),
                         "source_chunks": list(v.get("source_chunks", [])),
                         "created_at": v["created_at"]}
                        for k, v in self.nodes.items()]

            def merge_entities(self, kb, doc, pairs):
                m = 0
                for p in pairs:
                    c, f = p["canonical"], p["fragment"]
                    if c not in self.nodes or f not in self.nodes:
                        continue
                    self.nodes[c]["aliases"] = list(dict.fromkeys(
                        self.nodes[c]["aliases"] + self.nodes[f]["aliases"] + [f]))
                    self.nodes[c]["source_chunks"] = list(dict.fromkeys(
                        self.nodes[c].get("source_chunks", [])
                        + self.nodes[f].get("source_chunks", [])))
                    del self.nodes[f]
                    self.merged_pairs.append((c, f))
                    m += 1
                return {"merged": m}
        return MiniGraph()

    def test_reconcile_wukong_family_still_merges(self):
        """互指佐证守卫不误伤:孙悟空家族(石猴/美猴王/齐天大圣/老孙)
        通过真实指认链完整并回一名(线上西游记的合法大头)。"老孙"仅被
        孙悟空一人声称,指认具体,合法并回。"""
        g = self._reconcile_mini_graph()
        g.nodes = {
            "石猴": {"type": "person", "aliases": ["美猴王", "孙悟空"],
                     "created_at": "t1", "source_chunks": ["c0"]},
            "美猴王": {"type": "person", "aliases": ["孙悟空", "猴王"],
                       "created_at": "t1", "source_chunks": ["c0", "c1"]},
            "孙悟空": {"type": "person",
                       "aliases": ["美猴王", "齐天大圣", "石猴", "老孙"],
                       "created_at": "t1",
                       "source_chunks": ["c0", "c1", "c2", "c3"]},
            "齐天大圣": {"type": "person", "aliases": ["大圣", "猴王", "美猴王"],
                         "created_at": "t2", "source_chunks": ["c3"]},
            "老孙": {"type": "person", "aliases": [],
                     "created_at": "t5", "source_chunks": ["c1"]},
        }
        n = tasks._merge_same_type_alias_pairs(g, "kb", "d1")
        # 4 个碎片(美猴王/齐天大圣/石猴/老孙)各自并进 canonical,各计 1
        assert n == 4
        # canonical:同批入库(t1)时溯源块最多者胜 → 孙悟空(4 块)
        assert set(g.nodes) == {"孙悟空"}
        assert {"石猴", "美猴王", "齐天大圣", "老孙"} <= set(g.nodes["孙悟空"]["aliases"])
        # 幂等
        assert tasks._merge_same_type_alias_pairs(g, "kb", "d1") == 0

    def test_reconcile_generic_epithet_not_anchor(self):
        """泛称防火墙:妖怪/大王这类被互不相识的精怪各自声称的泛称,
        不得作为合并锚点把整片精怪链式并成一坨(线上 289 组合并事故)。"""
        g = self._reconcile_mini_graph()
        demons = ["赛太岁", "蜈蚣精", "白骨精", "金角", "银角"]
        g.nodes = {
            "赛太岁": {"type": "person", "aliases": ["大王", "老妖"], "created_at": "t1"},
            "蜈蚣精": {"type": "person", "aliases": ["大王", "老妖"], "created_at": "t2"},
            "白骨精": {"type": "person", "aliases": ["大王", "老妖"], "created_at": "t3"},
            "金角": {"type": "person", "aliases": ["大王"], "created_at": "t4"},
            "银角": {"type": "person", "aliases": ["大王"], "created_at": "t5"},
            "大王": {"type": "person", "aliases": ["老妖"], "created_at": "t6"},
            "老妖": {"type": "person", "aliases": [], "created_at": "t7"},
        }
        n = tasks._merge_same_type_alias_pairs(g, "kb", "d1")
        # "大王"声称者 5 个互不相识 → 锚点无效;"老妖"声称者 {蜈蚣精,
        # 白骨精, 大王} 互不相识 → 锚点无效;没有任何合法指认 → 零合并
        assert n == 0
        assert set(g.nodes) == set(demons) | {"大王", "老妖"}
        assert g.merged_pairs == []

    def test_reconcile_component_size_cap(self):
        """保险阀:即便互指校验放行,连通块超过成员上限也整块放弃。"""
        g = self._reconcile_mini_graph()
        # 15 个实体两两互指(声称彼此名字),构成 15 成员连通块
        names = [f"乙{i}" for i in range(15)]
        g.nodes = {nm: {"type": "person",
                        "aliases": [x for x in names if x != nm],
                        "created_at": f"t{i}"} for i, nm in enumerate(names)}
        n = tasks._merge_same_type_alias_pairs(g, "kb", "d1")
        assert n == 0
        assert len(g.nodes) == 15

    def test_resume_glossary_seeded_from_graph(self, env):
        """词表播种(根修):续跑任务的词表从图播种,第一批 prompt 就含
        既有实体名——此前续跑词表从零开始,是碎片的主要来源。"""
        kb, doc = _make_doc(env, "播种", ["[E:甲] [E:乙] 首段。", "[E:丙] 次段。"])
        store = env.store
        store.replace_chunks(doc["id"], kb["id"], [{"seq": 0, "text": "[E:甲] [E:乙] 首段。"},
                                                   {"seq": 1, "text": "[E:丙] 次段。"}])
        chunks = store.list_chunks(doc["id"])
        store.mark_chunk(chunks[0]["id"], "done")
        env.graph.upsert_batch(kb["id"], doc["id"], [
            {"name": "甲", "normalized_name": "甲", "type": "person",
             "description": "", "aliases": [], "chunk_ids": [chunks[0]["id"]]},
        ], [])
        store.update_document(doc["id"], import_status="partial", error="中断")

        prompts = []
        orig = env.llm.chat_json
        def capture(self, messages, **kw):
            prompts.append(messages[0]["content"])
            return orig(messages, **kw)
        env.llm.chat_json = capture.__get__(env.llm, type(env.llm))

        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded", t["error"]
        # 续跑第一批(块1)的 prompt 里就有既有实体 甲(词表播种生效)
        assert prompts and "- 甲(person)" in prompts[0]
        logs = env.manager.store.list_logs(t["id"])
        assert any("词表播种" in (l.get("message") or "") for l in logs)

    def test_idempotent_skip(self, env):
        kb, doc = _make_doc(env, "跳过", ["[E:甲] [E:乙]"])
        t1 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t1["status"] == "succeeded"
        calls_before = env.llm.extract_calls
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["result"].get("skipped") is True
        assert env.llm.extract_calls == calls_before  # 没有重复烧 LLM

    def test_extraction_model_override(self, env):
        """extraction_model 设置 → kg.extract 调用带模型覆盖;不设 = 不传。"""
        kb, doc = _make_doc(env, "模型覆盖", ["[E:甲] [E:乙]", "[E:丙] [E:丁]"])
        env.settings.save_values("knowledge_graph", {"extraction_model": "qwen-plus"})
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded", t["error"]
        # 每次抽取调用都带了覆盖模型
        assert env.llm.models_seen == ["qwen-plus"] * env.llm.extract_calls
        # 任务日志的抽取配置行留痕了覆盖模型
        logs = env.manager.store.list_logs(t["id"])
        cfg_log = next(l for l in logs if "抽取配置" in l["message"])
        assert "qwen-plus(覆盖)" in cfg_log["message"]
        assert cfg_log["data"].get("model_override") == "qwen-plus"

        # 清掉设置再导:不再传覆盖
        env.llm.models_seen = []
        env.settings.save_values("knowledge_graph", {"extraction_model": ""})
        kb2, doc2 = _make_doc(env, "默认模型", ["[E:戊] [E:己]"])
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb2["id"], doc2["id"])["id"])
        assert t2["status"] == "succeeded", t2["error"]
        assert env.llm.models_seen == []

    def test_force_reimport_cleans_and_redoes(self, env):
        kb, doc = _make_doc(env, "重导", ["[E:甲] [E:乙]", "[E:丙]"])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert env.store.get_document(doc["id"])["importStatus"] == "succeeded"
        t = _wait(env.manager, tasks.submit_import(
            env.app_state, kb["id"], doc["id"], force=True)["id"])
        assert t["status"] == "succeeded" and not t["result"].get("skipped")
        assert env.graph.delete_calls == [doc["id"], doc["id"]]  # 重导前清理
        assert env.store.get_document(doc["id"])["importStatus"] == "succeeded"

    def test_checkpoint_resume_only_failed_chunks(self, env):
        """单块失败 → partial;修复后重跑只重抽失败块(断点续跑)。"""
        pad = "背景铺垫文字。" * 12   # ~84 字,确保每段独立成块
        kb, doc = _make_doc(env, "续跑", [
            f"{pad}[E:甲] [E:乙]",
            f"{pad}坏块标记XYZ 出现在这一段 [E:丙]",
            f"{pad}[E:丁] 结尾",
        ])
        env.llm.fail_markers = ["坏块标记XYZ"]
        t1 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t1["status"] == "succeeded"
        assert t1["result"]["status"] == "partial" and t1["result"]["failedChunks"] >= 1
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "partial" and "失败" in d["error"]
        statuses = [c["status"] for c in env.store.list_chunks(doc["id"])]
        assert "failed" in statuses and "done" in statuses
        calls_after_first = env.llm.extract_calls

        # 修复"上游"后重跑:不 force → 复用 chunk,只重抽失败块
        env.llm.fail_markers = []
        deletes_before = len(env.graph.delete_calls)
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["status"] == "succeeded" and t2["result"]["status"] == "succeeded"
        re_extracted = env.llm.extract_calls - calls_after_first
        assert re_extracted == statuses.count("failed")
        assert all(c["status"] == "done" for c in env.store.list_chunks(doc["id"]))
        # 【B1 回归锚】断点续跑严禁清理图谱:已完成块的贡献只活在图里,
        # 清了又不重抽(done 块被跳过)等于静默丢数据
        assert len(env.graph.delete_calls) == deletes_before

    def test_resume_preserves_done_chunk_entities(self, env):
        """B1 主案:失败重跑后,已完成块抽取出的实体仍在图谱里。"""
        pad = "背景铺垫文字。" * 30   # 单段即超切块目标,确保两段独立成块
        kb, doc = _make_doc(env, "保留", [
            f"{pad}[E:甲] [E:乙]",
            f"{pad}坏块标记XYZ [E:丙]",
        ])
        env.llm.fail_markers = ["坏块标记XYZ"]
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert env.graph.counts(kb["id"])["entities"] == 2   # 甲乙已入图
        # 重跑(修复上游)后:甲乙必须还在(丙补上,共 3)
        env.llm.fail_markers = []
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["status"] == "succeeded"
        assert env.graph.counts(kb["id"])["entities"] == 3

    def test_relation_endpoint_constraint_enforced(self, env):
        """端点类型约束(代码强制):任职于要求目标 ∈ organization,
        person→person 违例被丢弃;无约束类型(相关)不受影响。"""
        kb, doc = _make_doc(env, "约束", ["# 章节\n[E:甲] 和 [E:乙] 是同事。"])
        env.llm.rel_type = "任职于"   # general 模板: 任职于 person→organization
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        assert t["result"]["entities"] == 2          # 实体不受影响
        assert t["result"]["relations"] == 0          # 违例关系被代码丢弃

        kb2, doc2 = _make_doc(env, "约束2", ["[E:丙] 和 [E:丁] 是同事。"])
        env.llm.rel_type = "相关"                     # 无 domain/range 约束
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb2["id"], doc2["id"])["id"])
        assert t2["result"]["relations"] == 1

    def test_glossary_touch_recency(self):
        """词表 LRU:重复出现的实体挪到尾部(渲染取尾 top-K)——主力角色
        不因"最早入库"被截出词表(同一人物分裂成多节点的温床)。"""
        from domains.knowledge_graph.tasks import _glossary_touch
        g: dict = {}
        _glossary_touch(g, [{"normalized_name": f"配角{i}", "type": "person"} for i in range(5)])
        _glossary_touch(g, [{"normalized_name": "配角0", "type": "person"}])   # 主力再出现
        _glossary_touch(g, [{"normalized_name": "新角色", "type": "person"}])
        order = list(g.keys())
        # 首位是未再出现的最早实体;最近触达的两个排在尾部(渲染取尾 top-K 命中它们)
        assert order[0] == "配角1"
        assert order[-2:] == ["配角0", "新角色"]
        assert order.index("配角0") > order.index("配角1")   # 重触达者后于未触达者

    def test_chunk_output_provenance(self, env):
        """块级溯源:实体带 source_chunks、关系带 chunk_id,块产出查询
        能精确回答"这一块抽出了什么"(实体跨块累积,关系逐条精确)。"""
        pad = "背景铺垫文字。" * 12
        kb, doc = _make_doc(env, "溯源", [
            f"{pad}[E:甲] 和 [E:乙] 共事",
            f"{pad}[E:甲] 与 [E:丙] 也认识",
        ])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        chunks = env.store.list_chunks(doc["id"])
        # 每块都有产出,且互不串扰
        out0 = env.graph.chunk_output(kb["id"], chunks[0]["id"])
        out1 = env.graph.chunk_output(kb["id"], chunks[1]["id"])
        names0 = {e["name"] for e in out0["entities"]}
        names1 = {e["name"] for e in out1["entities"]}
        assert "乙" in names0 and "丙" in names1
        assert "甲" in names0 and "甲" in names1      # 跨块实体两边都有溯源
        assert out0["relations"] and out1["relations"]
        assert all(r["source"] == "甲" for r in out0["relations"] + out1["relations"])
        # 关系按块精确归属
        srcs0 = {r["target"] for r in out0["relations"]}
        srcs1 = {r["target"] for r in out1["relations"]}
        assert "乙" in srcs0 and "丙" in srcs1 and not (srcs0 & srcs1)

    def test_circuit_breaker(self, env):
        env.settings.save_values("knowledge_graph", {"failure_threshold": 2})
        pad = "背景铺垫文字。" * 12
        kb, doc = _make_doc(env, "熔断", [
            f"{pad}[E:甲]", f"{pad}坏块标记A", f"{pad}坏块标记B", f"{pad}[E:乙]",
        ])
        env.llm.fail_markers = ["坏块标记A", "坏块标记B"]
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "failed"
        assert "熔断" in t["error"]
        d = env.store.get_document(doc["id"])
        assert d["importStatus"] == "failed" and "熔断" in d["error"]

    def test_concurrent_same_doc_rejected(self, env):
        env.llm.fail_markers = []  # 无失败
        # 用大文档 + 人为放慢抽取,让任务跑起来
        kb, doc = _make_doc(env, "并发", [f"[E:甲{i}] [E:乙{i}]" for i in range(30)])
        orig = env.llm.chat_json
        def slow(*a, **kw):
            time.sleep(0.05)
            return orig(*a, **kw)
        env.llm.chat_json = slow
        t1 = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        with pytest.raises(ValueError, match="进行中"):
            tasks.submit_import(env.app_state, kb["id"], doc["id"])
        _wait(env.manager, t1["id"])
        # 完成后可再次提交
        t2 = tasks.submit_import(env.app_state, kb["id"], doc["id"])
        assert _wait(env.manager, t2["id"])["status"] == "succeeded"

    def test_pending_cancel_releases_dedupe(self, env):
        """【H2 回归锚】排队期取消的任务也必须释放防重占位(框架 dedupe)。

        handler 不执行 → finally 不生效;框架在 cancel 的 pending 路径
        直接释放 dedupe 占位,否则该文档会永久报"已有进行中的导入任务"。
        """
        # 占满并发额度,让后续任务停留在 pending
        kb1, doc1 = _make_doc(env, "占位1", [f"[E:甲{i}]" for i in range(30)])
        kb2, doc2 = _make_doc(env, "占位2", [f"[E:乙{i}]" for i in range(30)])
        orig = env.llm.chat_json
        def slow(*a, **kw):
            time.sleep(0.05)
            return orig(*a, **kw)
        env.llm.chat_json = slow
        env.manager.submit("kg.import_document",
                           payload={"kb_id": kb1["id"], "doc_id": doc1["id"]},
                           title="占位1", queue_key=f"kg:{kb1['id']}")
        env.manager.submit("kg.import_document",
                           payload={"kb_id": kb2["id"], "doc_id": doc2["id"]},
                           title="占位2", queue_key=f"kg:{kb2['id']}")
        # pending 任务带 dedupe_key 占住 doc1(模拟提交后排队)
        pending_task = env.manager.submit(
            "kg.import_document",
            payload={"kb_id": kb1["id"], "doc_id": doc1["id"], "force": False},
            title="pending取消案", queue_key=f"kg:{kb1['id']}x",
            dedupe_key=f"kg.import:{doc1['id']}")
        # 取消 pending 任务(不执行 handler)
        cancelled = env.manager.cancel(pending_task["id"])
        assert cancelled["status"] == "cancelled"
        # 框架已释放占位 → 可以再次提交该文档
        t = tasks.submit_import(env.app_state, kb1["id"], doc1["id"])
        assert _wait(env.manager, t["id"])["status"] == "succeeded"

    def test_vector_mode_enabled_and_upserted(self, env, monkeypatch):
        monkeypatch.setenv("LLM_EMBED_MODEL", "mock-embed")
        kb, doc = _make_doc(env, "向量", ["[E:甲] [E:乙]", "[E:丙]"])
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        assert t["result"]["vectorEnabled"] is True
        kb_after = env.store.get_kb(kb["id"])
        assert kb_after["vectorEnabled"] and kb_after["vectorDim"] == 3
        assert env.vector.collections.get(kb["id"]) == 3
        assert env.vector.count(kb["id"]) == len(env.store.list_chunks(doc["id"]))

    def test_vector_backfill_on_rerun(self, env, monkeypatch):
        """向量缺口补偿:done 块缺向量时,重跑不得被幂等跳过挡住,须补写。

        场景:向量当批写入失败只告警(done 块重跑会被跳过)→ 缺口原本
        永久留存;修复后重跑已成功文档应走补偿路径(不重抽,只补向量)。
        """
        monkeypatch.setenv("LLM_EMBED_MODEL", "mock-embed")
        kb, doc = _make_doc(env, "向量补偿", ["[E:甲] [E:乙]", "[E:丙]"])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        chunk_ids = [c["id"] for c in env.store.list_chunks(doc["id"])]
        assert env.vector.count(kb["id"]) == len(chunk_ids)

        # 制造缺口:删掉某块的全部子段向量(模拟当批写入失败;子块 id = cid#i)
        cid0 = chunk_ids[0]
        for k in [k for k in list(env.vector.rows[kb["id"]]) if k.split("#", 1)[0] == cid0]:
            env.vector.rows[kb["id"]].pop(k)
        assert cid0 not in {c.split("#", 1)[0] for c in env.vector.existing_chunk_ids(kb["id"], doc["id"])}

        # 重跑(文档已 succeeded 且未 force):不得幂等跳过,应补齐缺口
        calls_before = env.llm.extract_calls
        t2 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t2["status"] == "succeeded"
        assert env.vector.count(kb["id"]) >= len(chunk_ids)         # 缺口补齐(子段数 ≥ 块数)
        assert cid0 in {c.split("#", 1)[0] for c in env.vector.rows[kb["id"]]}
        assert env.llm.extract_calls == calls_before                 # 没有重抽
        # 再跑一次:无缺口 → 幂等跳过
        t3 = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t3["result"].get("skipped") is True

    def test_startup_backfill_before_new_chunks(self, env, monkeypatch):
        """启动期对账:续跑任务先补历史向量缺口,再跑新块(用户诉求:
        重启时先检查已处理的是否正确、不正确补全,而不是最后才弄)。
        场景:首次跑到中途被杀(块0 done 但向量缺失,块1 pending)→
        重跑时块0 的向量在批循环之前就被补上。"""
        monkeypatch.setenv("LLM_EMBED_MODEL", "mock-embed")
        kb, doc = _make_doc(env, "启动对账", ["[E:甲] [E:乙]", "[E:丙] [E:丁]"])
        # 手工制造"中途被杀"状态:块0 done 且图谱有贡献,但向量全缺;块1 pending
        store = env.store
        store.replace_chunks(doc["id"], kb["id"], [{"seq": 0, "text": "[E:甲] [E:乙]"},
                                                   {"seq": 1, "text": "[E:丙] [E:丁]"}])
        chunks = store.list_chunks(doc["id"])
        store.mark_chunk(chunks[0]["id"], "done")
        env.graph.upsert_batch(kb["id"], doc["id"],
            [{"name": "甲", "normalized_name": "甲", "type": "person",
              "description": "", "aliases": [], "chunk_ids": [chunks[0]["id"]]}], [])
        store.update_document(doc["id"], import_status="partial", error="中途被杀")
        assert env.vector.count(kb["id"]) == 0    # 向量全缺

        embeds_before_gapfill = None
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        # 两块向量都齐了:块0 由启动期对账补,块1 由正常批处理写
        assert env.vector.count(kb["id"]) >= 2
        assert all(c["status"] == "done" for c in store.list_chunks(doc["id"]))
        # 日志里有"启动期对账"标记(而非只在收尾)
        logs = env.manager._store.list_logs(t["id"]) if hasattr(env.manager, "_store") else []
        assert any("启动期对账" in (l.get("message") or "") for l in logs)

    def test_vector_subchunked_for_long_chapter(self, env, monkeypatch):
        """长章块按段子切后入库:抽取按章(一块),向量按段(多子段)——
        本地 bge-m3 实际 4096 token 截断,长块尾部对向量是隐形的。"""
        monkeypatch.setenv("LLM_EMBED_MODEL", "mock-embed")
        kb, doc = _make_doc(env, "长章向量", ["# 巨章\n" + "张小凡修行大梵般若。" * 400])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        # 抽取一块(整章),向量多子段(cid#0/cid#1/…)
        chunks = env.store.list_chunks(doc["id"])
        assert len(chunks) == 1
        rows = list(env.vector.rows[kb["id"]].keys())
        assert len(rows) >= 2 and all("#" in r for r in rows)
        assert all(len(env.vector.rows[kb["id"]][r]["text"]) <= 1200 for r in rows)

    def test_targeted_chunk_retry(self, env):
        """定向重抽:chunk_ids 只补指定块(非 done),不动图谱、不重抽其他块。"""
        pad = "背景铺垫文字。" * 12
        kb, doc = _make_doc(env, "定向", [
            f"{pad}[E:甲] [E:乙]",
            f"{pad}坏块标记XYZ [E:丙]",
            f"{pad}[E:丁] 结尾",
        ])
        env.llm.fail_markers = ["坏块标记XYZ"]
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        failed = [c for c in env.store.list_chunks(doc["id"]) if c["status"] == "failed"]
        assert len(failed) == 1

        env.llm.fail_markers = []
        calls_before = env.llm.extract_calls
        deletes_before = len(env.graph.delete_calls)
        t = _wait(env.manager, tasks.submit_import(
            env.app_state, kb["id"], doc["id"], chunk_ids=[failed[0]["id"]])["id"])
        assert t["status"] == "succeeded" and t["result"]["status"] == "succeeded"
        assert env.llm.extract_calls == calls_before + 1        # 只重抽了指定块
        assert len(env.graph.delete_calls) == deletes_before    # 未清理图谱
        assert all(c["status"] == "done" for c in env.store.list_chunks(doc["id"]))

    def test_vector_degraded_without_model(self, env, monkeypatch):
        monkeypatch.setenv("EMBEDDING_BACKEND", "api")
        kb, doc = _make_doc(env, "降级", ["[E:甲]"])
        t = _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        assert t["status"] == "succeeded"
        assert t["result"]["vectorEnabled"] is False
        assert not env.store.get_kb(kb["id"])["vectorEnabled"]


class TestInduceSchema:

    def test_induce_stores_pending_proposal(self, env):
        kb, doc = _make_doc(env, "归纳", ["[E:甲] [E:乙]", "[E:丙]"])
        _wait(env.manager, tasks.submit_import(env.app_state, kb["id"], doc["id"])["id"])
        t = _wait(env.manager, tasks.submit_induce_schema(env.app_state, kb["id"])["id"])
        assert t["status"] == "succeeded", t["error"]
        assert t["result"]["entityTypes"] == 1
        schema = env.store.get_kb(kb["id"])["schema"]
        assert schema["pending_schema_induction"]["entity_types"][0]["key"] == "widget"
        # 原本体不被覆盖
        assert schema["entity_types"], "原实体类型应保留"


# ── 收尾描述连边(_backfill_mention_edges) ─────────────────────

class _MentionGraph:
    """最小图桩:直接喂 list_entities 形状的数据,验证连边决策逻辑。"""

    def __init__(self, entities, edges=None):
        self._entities = entities
        self.edges = list(edges or [])
        self.upserted = []

    def list_entities(self, kb_id):
        return self._entities

    def list_connected_pairs(self, kb_id):
        out = set()
        for r in self.edges:
            if r["source"] == r["target"]:
                continue
            out.add((r["source"], r["target"]))
            out.add((r["target"], r["source"]))
        return out

    def upsert_batch(self, kb_id, doc_id, entities, relations):
        self.edges.extend(relations)
        self.upserted.extend(relations)
        return {"entities": len(entities), "relations": len(relations)}


def _ent(norm, name=None, aliases=(), desc=""):
    return {"normalized": norm, "name": name or norm, "type": "person",
            "aliases": list(aliases), "source_chunks": ["c1"],
            "description": desc, "created_at": "2026-01-01"}


class TestMentionBackfill:

    KB, DOC = "kb1", "doc1"

    def test_mention_creates_fallback_edge(self):
        g = _MentionGraph([
            _ent("木德星君", desc="送孙悟空去御马监到任的星官"),
            _ent("孙悟空"),
        ])
        n = tasks._backfill_mention_edges(g, self.KB, self.DOC)
        assert n == 1
        rel = g.upserted[0]
        assert rel["source"] == "木德星君" and rel["target"] == "孙悟空"
        assert rel["type"] == "相关"
        assert "孙悟空" in rel["evidence"]
        assert rel["chunk_id"] == "desc-mention"

    def test_mention_via_alias_resolves_to_canonical(self):
        g = _MentionGraph([
            _ent("太白金星", desc="招安齐天大圣上天为官"),
            _ent("孙悟空", aliases=["齐天大圣", "大圣"]),
        ])
        n = tasks._backfill_mention_edges(g, self.KB, self.DOC)
        assert n == 1
        assert g.upserted[0]["target"] == "孙悟空"

    def test_alias_substring_not_double_linked(self):
        # 描述含"齐天大圣",其子串别名"大圣"同主不再重复连
        g = _MentionGraph([
            _ent("太白金星", desc="奉旨招安齐天大圣"),
            _ent("孙悟空", aliases=["齐天大圣", "大圣"]),
        ])
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 1

    def test_skip_when_pair_already_connected(self):
        g = _MentionGraph([
            _ent("木德星君", desc="送孙悟空去御马监到任的星官"),
            _ent("孙悟空"),
        ], edges=[{"source": "木德星君", "target": "孙悟空",
                   "type": "敌对", "chunk_id": "c1"}])
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 0
        assert not g.upserted

    def test_mutual_mention_single_edge(self):
        g = _MentionGraph([
            _ent("张三", desc="与李四结义"),
            _ent("李四", desc="受张三指点"),
        ])
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 1

    def test_single_char_name_ignored(self):
        g = _MentionGraph([
            _ent("甲", desc="孙家仆人出身"),
            _ent("孙"),
        ])
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 0

    def test_idempotent_rerun(self):
        g = _MentionGraph([
            _ent("木德星君", desc="送孙悟空去御马监到任的星官"),
            _ent("孙悟空"),
        ])
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 1
        # 重跑:已连边进入 connected 集合,不再补
        assert tasks._backfill_mention_edges(g, self.KB, self.DOC) == 0
        assert len(g.upserted) == 1


# ── 别名独占性校验(_drop_contested_aliases) ───────────────────

def _aliased(nm, aliases, type_="person"):
    return {"normalized_name": nm, "name": nm, "type": type_,
            "aliases": list(aliases)}


class TestAliasExclusivity:

    def test_generic_epithet_dropped_from_all_claimants(self):
        """泛称防火墙:互不相识的多方共同声称的别名("大王"),从所有
        声称者丢弃——线上 289 组误并事故的入库前拦截。"""
        ents = [_aliased("赛太岁", ["大王", "老妖"]),
                _aliased("蜈蚣精", ["大王", "老妖"]),
                _aliased("白骨精", ["大王"])]
        n = tasks._drop_contested_aliases(ents, {})
        assert n == 5  # 大王×3 + 老妖×2
        assert all(e["aliases"] == [] for e in ents)

    def test_unique_alias_kept(self):
        ents = [_aliased("孙悟空", ["美猴王", "齐天大圣"]),
                _aliased("牛魔王", ["平天大圣"])]
        assert tasks._drop_contested_aliases(ents, {}) == 0
        assert ents[0]["aliases"] == ["美猴王", "齐天大圣"]

    def test_claim_of_name_is_identification_not_contest(self):
        """声称别人的名字 = 认同同一身份,不算争议——碎片合并信号保留
        (张小凡 声称 鬼厉 / 鬼厉 声称 张小凡)。"""
        ents = [_aliased("张小凡", ["鬼厉"]),
                _aliased("鬼厉", ["张小凡"])]
        assert tasks._drop_contested_aliases(ents, {}) == 0
        assert ents[0]["aliases"] == ["鬼厉"] and ents[1]["aliases"] == ["张小凡"]

    def test_graph_side_contention_blocks_new_claimant(self):
        """续跑场景:图谱已存实体已声称"老妖",新批实体与它互不指认
        却也声称"老妖" → 从新批丢弃(图谱侧由收尾对账兜底)。"""
        graph = {"蜘蛛精": {"type": "person", "aliases": ["老妖"]}}
        ents = [_aliased("蜈蚣精", ["老妖", "百眼魔君"])]
        n = tasks._drop_contested_aliases(ents, graph)
        assert n == 1
        assert ents[0]["aliases"] == ["百眼魔君"]

    def test_graph_name_claimed_by_new_fragment_kept(self):
        """新批碎片声称图谱已存实体的名字 = 合并信号,不拦。"""
        graph = {"孙悟空": {"type": "person", "aliases": ["美猴王"]}}
        ents = [_aliased("美猴王", ["孙悟空"])]
        assert tasks._drop_contested_aliases(ents, graph) == 0
        assert ents[0]["aliases"] == ["孙悟空"]


# ── 图谱侧别名去争议(_prune_graph_aliases)与证据锚定 ──────────

class TestGraphAliasPruning:

    KB = "kb1"

    def test_prune_removes_contested_and_keeps_identity(self):
        """泛称从图里清掉,身份别名和名字互指(合并信号)不动。"""
        g = FakeGraph()
        g.upsert_batch(self.KB, "d1", [
            {"name": "赛太岁", "normalized_name": "赛太岁", "type": "person",
             "aliases": ["大王", "太岁凶妖"], "chunk_ids": ["c1"]},
            {"name": "蜈蚣精", "normalized_name": "蜈蚣精", "type": "person",
             "aliases": ["大王", "百眼魔君"], "chunk_ids": ["c1"]},
            {"name": "孙悟空", "normalized_name": "孙悟空", "type": "person",
             "aliases": ["美猴王"], "chunk_ids": ["c1"]},
        ], [])
        removed = tasks._prune_graph_aliases(g, self.KB)
        assert removed == 2  # 大王 × 2
        assert g.nodes[(self.KB, "赛太岁")]["aliases"] == ["太岁凶妖"]
        assert g.nodes[(self.KB, "蜈蚣精")]["aliases"] == ["百眼魔君"]
        assert g.nodes[(self.KB, "孙悟空")]["aliases"] == ["美猴王"]

    def test_prune_keeps_one_way_name_claim(self):
        """碎片声称 canonical 名字(单向指认)不是争议——合并信号保留。"""
        g = FakeGraph()
        g.upsert_batch(self.KB, "d1", [
            {"name": "孙悟空", "normalized_name": "孙悟空", "type": "person",
             "aliases": [], "chunk_ids": ["c1"]},
            {"name": "美猴王", "normalized_name": "美猴王", "type": "person",
             "aliases": ["孙悟空"], "chunk_ids": ["c1"]},
        ], [])
        assert tasks._prune_graph_aliases(g, self.KB) == 0
        assert g.nodes[(self.KB, "美猴王")]["aliases"] == ["孙悟空"]

    def test_prune_idempotent(self):
        g = FakeGraph()
        g.upsert_batch(self.KB, "d1", [
            {"name": "甲妖", "normalized_name": "甲妖", "type": "person",
             "aliases": ["大王"], "chunk_ids": ["c1"]},
            {"name": "乙妖", "normalized_name": "乙妖", "type": "person",
             "aliases": ["大王"], "chunk_ids": ["c1"]},
        ], [])
        assert tasks._prune_graph_aliases(g, self.KB) == 2
        assert tasks._prune_graph_aliases(g, self.KB) == 0

    def test_prune_then_merge_not_chained(self):
        """剪枝后,原本经泛称可达的实体不再被误并(289 组事故防回归)。"""
        g = FakeGraph()
        g.upsert_batch(self.KB, "d1", [
            {"name": "赛太岁", "normalized_name": "赛太岁", "type": "person",
             "aliases": ["大王"], "chunk_ids": ["c1"]},
            {"name": "蜈蚣精", "normalized_name": "蜈蚣精", "type": "person",
             "aliases": ["大王"], "chunk_ids": ["c1"]},
            {"name": "白骨精", "normalized_name": "白骨精", "type": "person",
             "aliases": ["大王"], "chunk_ids": ["c1"]},
        ], [])
        tasks._prune_graph_aliases(g, self.KB)
        n = tasks._merge_same_type_alias_pairs(g, self.KB, "d1")
        assert n == 0
        assert len([k for k in g.nodes if k[0] == self.KB]) == 3


class TestEvidenceAnchoring:

    def test_paraphrased_evidence_blank_kept_verbatim(self):
        rels = [
            {"source": "甲", "target": "乙", "type": "敌对",
             "evidence": "甲把乙打败了"},       # 转述
            {"source": "甲", "target": "丙", "type": "挚友",
             "evidence": "甲与丙结义"},          # 逐字
            {"source": "甲", "target": "丁", "type": "相关",
             "evidence": ""},                    # 空证据不动
        ]
        text = "那一日,甲与丙结义,誓同生死。"
        n = tasks._blank_unanchored_evidence(rels, text)
        assert n == 1
        assert rels[0]["evidence"] == ""
        assert rels[1]["evidence"] == "甲与丙结义"
        assert rels[2]["evidence"] == ""


# ── 称谓枢纽防火墙(_bridge_hubs) ──────────────────────────────

class TestBridgeHubGuard:
    """桥接型称谓节点(老妖 认领 黄风怪/黄袍怪)拒绝经它合并;合法大家族
    (孙悟空家族,声称对象经彼此指认传递连通)不受影响——通用判据:
    去掉 x 的指认边后,其声称的名字散落在 ≥2 个有外部佐证的连通分量。"""

    def _mini(self):
        class MiniGraph:
            def __init__(self):
                self.nodes = {}

            def list_entities(self, kb):
                return [{"normalized": k, "name": k, "type": v["type"],
                         "aliases": list(v["aliases"]),
                         "source_chunks": list(v.get("source_chunks", [])),
                         "created_at": v["created_at"]}
                        for k, v in self.nodes.items()]

            def merge_entities(self, kb, doc, pairs):
                m = 0
                for p in pairs:
                    c, f = p["canonical"], p["fragment"]
                    if c not in self.nodes or f not in self.nodes:
                        continue
                    self.nodes[c]["aliases"] = list(dict.fromkeys(
                        self.nodes[c]["aliases"] + self.nodes[f]["aliases"] + [f]))
                    del self.nodes[f]
                    m += 1
                return {"merged": m}
        return MiniGraph()

    def test_epithet_hub_blocked(self):
        g = self._mini()
        g.nodes = {
            "老妖": {"type": "person", "aliases": ["黄风怪", "黄袍怪"],
                     "created_at": "t1", "source_chunks": ["c20", "c28"]},
            "黄风怪": {"type": "person", "aliases": ["黄风大王"],
                       "created_at": "t2", "source_chunks": ["c20"]},
            "黄袍怪": {"type": "person", "aliases": ["奎木狼"],
                       "created_at": "t3", "source_chunks": ["c28"]},
        }
        assert tasks._merge_same_type_alias_pairs(g, "kb", "d1") == 0
        assert set(g.nodes) == {"老妖", "黄风怪", "黄袍怪"}

    def test_family_with_leaf_not_hub(self):
        """哑叶子(老孙 仅被 孙悟空 指认)不构成独立锚定分量——家族照常合并。"""
        g = self._mini()
        g.nodes = {
            "石猴": {"type": "person", "aliases": ["美猴王", "孙悟空"],
                     "created_at": "t1", "source_chunks": ["c0"]},
            "美猴王": {"type": "person", "aliases": ["孙悟空", "猴王"],
                       "created_at": "t1", "source_chunks": ["c0", "c1"]},
            "孙悟空": {"type": "person",
                       "aliases": ["美猴王", "齐天大圣", "石猴", "老孙"],
                       "created_at": "t1",
                       "source_chunks": ["c0", "c1", "c2", "c3"]},
            "齐天大圣": {"type": "person", "aliases": ["大圣", "猴王", "美猴王"],
                         "created_at": "t2", "source_chunks": ["c3"]},
            "老孙": {"type": "person", "aliases": [],
                     "created_at": "t5", "source_chunks": ["c1"]},
        }
        assert tasks._merge_same_type_alias_pairs(g, "kb", "d1") == 4
        assert set(g.nodes) == {"孙悟空"}



# ── 锚定收紧:别名不参与接地(_anchor_filter) ───────────────────

class TestAnchorNameOnly:

    def test_alias_grounding_no_longer_passes(self):
        """线上实锤:平顶山妖王被 LLM 安上 宝象国 的名字(黄袍怪)+
        别名(金角大王)——名字不在原文但别名在,旧版豁免让张冠李戴
        入图,跨章合并焊死假身份。收紧后按名字接地,整体丢弃。"""
        ents = [{"name": "黄袍怪", "normalized_name": "黄袍怪",
                 "type": "creature", "aliases": ["金角大王", "老魔"],
                 "description": ""}]
        text = "那魔道:吾乃金角大王、银角大王之兄。"
        kept, dropped = tasks._anchor_filter(ents, text)
        assert kept == [] and dropped == 1

    def test_name_grounding_still_passes_with_aliases(self):
        ents = [{"name": "孙悟空", "normalized_name": "孙悟空",
                 "type": "person", "aliases": ["美猴王"], "description": ""}]
        text = "孙悟空道:俺老孙来也!"
        kept, dropped = tasks._anchor_filter(ents, text)
        assert len(kept) == 1 and dropped == 0

    def test_heading_and_punct_still_dropped(self):
        ents = [{"name": "第三回 四海千山皆拱伏", "normalized_name": "x1",
                 "type": "event", "aliases": [], "description": ""},
                {"name": "他说,你去吧", "normalized_name": "x2",
                 "type": "person", "aliases": [], "description": ""}]
        text = "第三回 四海千山皆拱伏。他说,你去吧。"
        kept, dropped = tasks._anchor_filter(ents, text)
        assert kept == [] and dropped == 2
