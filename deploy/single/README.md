
## 部署契约

本目录只支持**单容器、单 Uvicorn worker**。`start.sh` 不传 `--workers`，
插件启停、定时任务和运行态都以这一约束设计。

不要把该镜像扩成多副本或改成多 worker；动态插件启停只会更新当前进程的
registry、routes 和任务 handler。未来若需要横向扩容，应先设计集中式插件状态
广播和 scheduler leader election，而不是直接增加副本数。
