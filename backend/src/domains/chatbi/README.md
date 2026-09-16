# chatbi 插件

ChatBI(独立 NL2SQL BI 平台)迁移至 llm-to-modler 插件化的落点。
问数主链路(数据源→检索→SQL→执行→图表)、语义层版本、记忆/反馈闭环、
BI 可观测性均已迁入; 详细架构见 `domains/ARCHITECTURE.md`。

## 与原 ChatBI 的有意差异(产品决策, 非漏迁)

以下能力在原 ChatBI 中存在, 迁移时**有意裁剪或简化**, 记录于此避免
后续审计误判为缺口:

| 能力 | 原系统 | 本插件 | 决策依据 |
|---|---|---|---|
| Skills 规则管理 | 管理端 CRUD + preview + 版本回滚 | 只读(规则文件随 pack 分发, 修改走部署流程) | 生产治理取向: 规则即代码, 变更可审计可回滚(git), 避免运行时热改 SQL 约束 |
| 图谱同步冲突交互 | graph_sync_conflict 状态 + 三选项 + /consolidate/retry | 冲突/失败进任务日志(任务中心可见), 重新提交整理任务即重试 | 单管理员运维场景无并发编辑; 乐观锁(expected_version)仍在, 真冲突时安全失败不写脏数据 |
| slash command(/ds /sql /chart /explain)、输入 Token 预算 | 终端高级交互 | 未迁 | 平台已有统一 pipeline 进度 + 管理端 Trace; 高级用户能力待产品确认需求后再评估 |

## 关键链路入口

- 主工具: `tools/ask_data.py`(8 步管线) / `tools/switch_chart.py`
- 图谱: `schema_graph.py`(构建/扩展/JOIN 路径) + `graph_infer.py`(演化) + `graph_edit.py`(关系编辑校验)
- 记忆: `memory.py`(CRUD/召回/整理——按 用户+数据源 scope 隔离)
- 可观测: `query_stats.py`(每查询一行质量统计) + 管理端 metrics/slow-queries/health-detail 端点
- 阈值配置: `settings.schema.yaml`(检索/图谱/记忆/可观测/定时 全量可调)
