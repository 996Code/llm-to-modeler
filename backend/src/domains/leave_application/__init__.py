"""leave_application pack - 请假申请提交。

演示非配置类插件：artifact_type="data"，前端渲染 data-card。

工具:
  - submit_leave: 提交请假申请(CompositeTool, 3步管线)
  - query_leave_status: 查询请假审批状态(Tool)

架构约定:
  - 所有上游调用走 AssetClient.submit_data/query_data
  - 不直接使用 httpx,保证 sanitize/headers/连接池统一管理
  - 上游 base_url 通过环境变量 ASSET_BASE_URL 配置
"""
