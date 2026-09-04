"""knowledge_graph pack - 知识库/知识图谱插件。

【能力总览】
  - 多知识库管理(每库独立本体/图谱/向量 collection,物理隔离)
  - 文档导入(md/txt/pdf/docx)→ 后台任务 LLM 抽取实体关系 → Neo4j 图谱
    + Milvus 向量(增量幂等,chunk 级 checkpoint)
  - 在线图谱浏览(节点/边查询、点击展开)
  - 混合检索问答(图谱子图 + 向量召回 → LLM 综合回答;对话工具 + REST)

【三存储分工】
  SQLite(conversations.db 同库 kg_ 前缀表) = 元数据与 checkpoint
  Neo4j  = 图谱(kb_id 隔离,(kb_id, normalized_name) NODE KEY)
  Milvus = 向量(每库一个 collection kg_{kb_id}_v1,删库即 drop)
"""
