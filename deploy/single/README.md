
## 多副本部署约束(四十审)

动态插件启停(toggle/recheck)只保证**单副本**一致性:

- 本目录的单容器部署(单 uvicorn worker)不受影响;
- 多副本/多 Pod 且**不共享** `PACK_STATE_PATH` 卷时, 每个副本各自
  看到 holder=1, 动态启停会被放行但其他副本 runtime 不同步——
  必须改用 `PACKS_ENABLED` 环境变量 + 滚动重启;
- 多副本**共享**状态卷时, holders 检测会看到多实例并自动拒绝
  动态管理(503), 同样走集中配置 + rollout。

一句话: 多副本 = 静态配置; 动态启停 = 单副本。

### 自动校验(四十一审)

多副本部署在环境变量声明后, 动态管理入口自动 fail-closed:

```bash
# 多副本/多 Pod 非共享状态卷的部署必须设置:
PACK_DYNAMIC_PACK_MGMT=off   # toggle/recheck 一律 503, 提示走
                             # PACKS_ENABLED + 滚动重启
```

未设置(缺省 `on`)适用于单副本/共享卷部署。该开关由部署清单
管理, 与镜像一起下发——不依赖运行时人工记忆。
