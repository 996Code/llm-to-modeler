# ChatBI 原版参考截图(设计/交互对标基准)

> 2026-09-15 从运行中的原版 ChatBI v2(localhost:5174)逐页截取。
> 用途:插件版页面设计与交互优化的对照基准——"之前在 chatbi 做得多好看"的实物档案。

## 截图清单

### 语义层(设计重点,插件版首要对标对象)
| 文件 | 内容 | 关键设计点 |
|---|---|---|
| chatbi-semantic-view.png | 语义层主页 | 左表列表(中文名+表名+列/关系/指标计数)+右详情双栏;顶部搜索框 |
| chatbi-semantic-table-detail.png | 表详情 | 表中文名/描述行内编辑(双击);列表格(中文名双击编辑/语义类型下拉/来源徽标📋注释🔑外键🤖LLM) |
| chatbi-semantic-table-metrics.png | 指标/关系区 | 指标 CRUD(新增/编辑/删除);关系表(基数/JOIN/ON);被引用关系 |
| chatbi-semantic-versions.png | 版本历史抽屉 | 版本列表+回滚+版本 diff |
| chatbi-semantic-graph.png | 图谱 tab | G6 全图(社区着色/中心度节点大小/搜索/缩放/连线建关系/新增关系对话框/详情面板) |

### 对话页
| 文件 | 内容 | 关键设计点 |
|---|---|---|
| chatbi-chat-result.png | 查询结果 | 查询流程步骤条(每步耗时+LLM次数+token);图表工具栏(导出Excel/保存看板/完整流程) |
| chatbi-chat-fulltrace.png | 完整流程弹窗 | 步骤表格+可展开详情(SQL原文/预思考/自愈前后SQL对比)+结果表 |
| chatbi-chat-save-dashboard.png | 保存到看板弹窗 | 选择已有看板/新建+组件命名 |

### 其他页面
| 文件 | 内容 | 关键设计点 |
|---|---|---|
| chatbi-datasources.png | 数据源管理 | 全量健康检查/元数据刷新;扫描进度条+阶段文字;启停切换 |
| chatbi-dashboard.png | 看板 | 看板Tab页签+编辑布局模式+gridstack拖拽+widget页脚(行数·刷新时间) |
| chatbi-history.png | 历史列表 | 标题/轮次/时间 |
| chatbi-history-detail.png | 历史详情 | ConversationDetailDrawer:预思考/SQL+自愈对比/结果表/图表全量回放 |
| chatbi-observability.png | 系统页 | 组件健康/审计日志/慢查询 |
| chatbi-skills.png | 规则页 | Skills 卡片(Markdown预览/编辑抽屉/方言子文件) |
| chatbi-memory.png | 记忆页 | 记忆卡片列表(类型标签/linkage元数据/来源对话跳转/新建/整理) |

## 插件版对标状态

- [x] 看板拖拽(gridstack makeWidget/change增量保存/页脚) — d3865e5
- [x] 图谱 tab(G6 社区着色/搜索/详情面板) — d3865e5
- [x] 记忆管理页 — d3865e5
- [x] 本轮链路弹窗(对标"完整流程"轻量版) — d3865e5
- [ ] **语义层双栏+行内编辑+指标CRUD+版本历史** — 待做(当前只有折叠面板平铺)
- [ ] 历史详情回放(预思考/自愈对比)
- [ ] 规则(Skills)管理页
- [ ] 对话页步骤展开(SQL/自愈对比/token分阶段)
