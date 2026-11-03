# 方向选定记录（direction-approved）

## 第一轮（2026-11-03 下午，被否）

展示了三张概念稿 + 一张备选（截图存于会话 artifacts；文件 design-demos/dir-1.html 仪器控制台 / dir-2.html 暗色链路 / dir-3.html 包豪斯标准台 / backup-report.html 运行报告）。

**用户原话**：「我觉得都不太好看，而且你需要基于当前的功能吧？」

裁决：三张概念稿全部否决；已逐字记入品味档案（~/.huashu-design/experience.md L1）。

## 第二轮（2026-11-03 晚，经 AskUserQuestion 三问）

**用户选择**：
1. 「原布局不动，纯视觉精装（推荐）」——现有顶栏/页签/表格/筛选栏/插件卡全部原样保留，只重做色彩系统、字体层级、卡片质感
2. 「飞书/钉钉式企业风」——蓝白干净、轻盈卡片、柔和圆角、清爽间距
3. 第一稿载体：「管理端·概览（还是它）」

执行口径：1:1 复刻真实概览页（frontend/src/admin/AdminApp.vue 外壳 + OverviewTab.vue 九卡结构，真实 /api/admin/stats 数据），只动视觉层。产出 design-demos/polished-v1.html 与前后对照页 index.html。
