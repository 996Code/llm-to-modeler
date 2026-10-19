# 04 · 文档理解 DeepDoc

> RAGFlow 的文档解析子系统：版面识别、表格还原、OCR 与 8 个可插拔外部引擎。

## 1. 版面识别（deepdoc/vision/）
> 🗺 对应链路图：[../图/ragflow-doc-ingestion.html]（parse-chunk 视图）

- **11 类 block 标签**（layout_recognizer.py:34-46）：Text/Title/Figure/Figure caption/Table/Table caption/Header/Footer/Reference/Equation——图注/表注/公式独立分类
- footer/header/reference 判垃圾（:49）；乱码垃圾门 `(cid:N)` 模式有专项测试（test_layout_garbage_gate.py）
- YOLOv10 变体（LayoutRecognizer4YOLOv10，:189）
- **三态执行自适应**：本地 ONNX（rag/res/deepdoc，HF 自动下载 InfiniFlow/deepdoc）→ 远程 TensorRT DLA 服务（DEEPDOC_URL）→ 昇腾 NPU InferSession（:52-66,289-290）——一套代码三端跑

## 2. 表格与 OCR
> 🗺 对应链路图：[../图/ragflow-doc-ingestion.html]（parse-chunk 视图）

- TSR 表格结构还原：13 结构标签 Recognizer（table_structure_recognizer.py:30-45），行列合并→Markdown
- OCR：onnxruntime 自推理（ocr.py:32），非 Paddle 直接依赖；乱码检测自动降级
- **Excel 表格切块的列语义**（rag/app/table.py）：多级表头解析（合并区域涉及前两行即判复杂表头，:22-37）→ 列名去重（:43-63）→ 每行一 chunk，列角色存进 chunk_data JSON（Infinity 下用 json_extract_string 查询，:563-617）——**表格文档入知识库时保留列语义**，对问数场景有直接参考价值

## 3. 18 个 parser：10 内部格式 + 8 外部引擎
> 🗺 对应链路图：[../图/ragflow-doc-ingestion.html]（parse-chunk 视图）

- 内部：pdf/docx/excel/html/markdown/ppt/txt/epub/json/figure
- **可插拔外部引擎**：docling / mineru / mistral / monkeyocrv2 / opendataloader / paddleocr / somark / tcadp
- PDF 三路径：自研 RAGFlowPdfParser / PlainParser（pdf_parser.py:2070）/ VisionParser（:2091）
- naive 切块的 layout 引擎可插拔（DeepDOC/MinerU/Docling/Plain）

## 4. LLM 增强（入库前）
> 🗺 对应链路图：[../图/ragflow-doc-ingestion.html]（index-compile 视图）

- auto_keywords/auto_questions：入库前 LLM 补关键词与"假设问题"（task_executor.py:477-535）——检索时能被"问题式查询"命中
- TOC 提取：run_toc_from_text+assign_toc_levels 产 toc_kwd chunk（含 ids 区间）

## 对我们的启示

- 我们 md/txt/pdf/docx 四格式**升级不必自研视觉管线**——接 MinerU/Docling 库即可获得版面+表格还原（RAGFlow 自己也把 8 个外部引擎做成可插拔）
- 优先级：**表格还原 > 版面重排 > OCR**（njmind 业务文档以 Word/表格为主）
- auto_questions"假设问题"技巧可直接进我们 KG 入库链（提升问题式查询命中）
