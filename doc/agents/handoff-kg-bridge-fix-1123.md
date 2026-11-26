# 同源修复交接：KG 导入收尾 O(N²) 卡死 + aliases TEXT 列 unnest 报错

> llm-to-modler 已修（d6be309 + cafd3b4，线上验证过）。以下给同源系统对齐修复用。

## Bug 1：`_bridge_hubs` O(N²) 卡死（症状：导入"向量补全后卡死"）

**症状**：文档导入任务的向量补全秒级完成，之后收尾阶段单核 100% 空转几十分钟无日志，
py-spy 栈定位在 `_merge_alias_pass` → `_bridge_hubs`（tasks.py）。整体 CPU 监控看不出异常
（4 核盒子只烧 1 核 = 25%）。队列里后续任务全部堵死。

**根因**：`_bridge_hubs` 对每个多别名实体 x，把全库所有别名指认边重建一遍并查集 →
O(N²) 纯 Python。2.9 万实体 ≈ 8.8 亿次循环。

**修复**（数学等价的局部 BFS，语义零变化）：

1. 判定目标不变：去掉"x 作为声称者"的边后，x 的别名落在 ≥2 个有外部佐证的连通分量 → x 是桥。
2. **局部 BFS 替代全量并查集**：连通分量是图的性质，从 x 的别名出发泛洪得到同一组分量，
   单次成本从 O(N) 降到 O(邻域)。锚定判据化简为"分量多点即锚定"（可证：节点有非 x 邻居
   ⟺ 所在分量非单点）。
3. **关键陷阱——去边必须按"声称者身份"而非端点对**：边对 {x, a}（a ∈ x 的别名集）仅当
   a 不反向声称 x 时才删（互指边由 a 侧生成，必须保留）；BFS 允许经保留边穿过 x 节点。
   按端点对去边的退化实现会切碎分量、漏判 hub → 错误合并（正是这堵防火墙要防的事故方向）。
4. **必须补测试**：互指场景（x 声称 a/c，a/c 各自反向声称 x 且带外部边）→ x 不是桥、
   正常合并。现有测试全是单向声称，测不出这个陷阱。

## Bug 2：`prune_aliases` 报 `function unnest(text) does not exist`

**症状**：收尾日志出现"收尾别名去争议失败(不影响导入结论): function unnest(text) does
not exist"，泛称别名清理静默降级。

**根因**：旧版迁移把 `graph_nodes.aliases` / `graph_entity_refs.aliases` 建成了 TEXT
（DDL 后来才改成 TEXT[]，`CREATE TABLE IF NOT EXISTS` 不会改已有表）。TEXT 里存的是
JSON 数组字面量 `["a","b"]`，unnest 直接报错。

**修复**（store 初始化迁移里发现即修，幂等）：

1. `information_schema.columns` 检测列型为 text 才动，TEXT[] 零成本跳过。
2. **先 `ALTER COLUMN ... DROP DEFAULT`**——旧列带 `DEFAULT '[]'`（TEXT 语义），
   PG 拒绝自动转换到 text[]（实锤报错：default for column "aliases" cannot be cast
   automatically to type text[]）。
3. `UPDATE ... SET col = replace(replace(col,'[','{'),']','}') WHERE col LIKE '[%]'`
   （JSON 字面量 → PG 数组字面量，两层引号逗号结构兼容）。
4. `ALTER ... TYPE TEXT[] USING CASE WHEN 空/非 '{...}' 形态 THEN '{}' ELSE col::TEXT[] END`。
5. 补回 `SET DEFAULT '{}'`（与 DDL 对齐）。
6. 全程在 migrate_locked 的 advisory lock + 单事务里。

**范围是四个列**（第三处实锤后扩展）：`graph_nodes.aliases/source_docs/source_chunks`
+ `graph_entity_refs.aliases`。source_docs/source_chunks 同样是旧 TEXT——症状在流水线
首次跑通后才暴露：`document_counts` 的 `? = ANY(source_docs)` 报
`op ANY/ALL (array) requires array`（`chunk_output` 的 `ANY(source_chunks)` 同族）。
同库同治，别只迁 aliases。

**注意**：PgGraphStore 是懒构造（首次图谱操作才建），迁移不随进程启动跑——冷启动后
首个碰图谱的请求触发。要立即生效可在容器里手动构造一次 store。

## 运维注意（容器化部署的任务队列坑）

容器**重建**（recreate）会换 hostname，而 TaskManager 的 `instance_id = hostname`、
启动恢复只清理本实例任务 → 旧实例的 pending/running/retry_scheduled 任务永久孤儿化，
队列假死。处置：SQL 把旧 instance_id 的活任务标 `interrupted` 后重启，KG pack 的启动
恢复会按文档表自愈续跑（成功文档的任务行会被正确关闭）。重试定时器是进程内线程，
重部署即失效，同样会孤儿化 retry_scheduled。

## 验证清单

- 新互指测试用例过；消歧/防火墙既有用例全绿。
- 大库（2.9 万实体）导入收尾从小时级降到秒级。
- 收尾日志不再出现 unnest 报错；列型变 TEXT[] 后二次启动幂等。
