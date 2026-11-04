# SearXNG 联网搜索栈部署手册（3.110）

> **2026-11-04 已部署上线**（searxng + searxng-mihomo 双容器，20/20 全绿，avg 0.6s）。
> 方案：SearXNG（google 修复版引擎）+ mihomo（挂机场订阅），为知识库运营侧调研工具提供联网搜索底座。
> **不进客服回答链路**（用户裁决的受控架构不变）。
> 本地验证记录：250 连击零限流 + 容器化端到端 20/20 全绿；3.110 上线复验 20/20 全绿。

## 架构

```
SearXNG 容器 (searxng)          ← JSON API，仅 compose 内网
    │ http://mihomo:7890
    ▼
mihomo 容器 (searxng-mihomo)    ← 挂机场订阅，url-test 自动选优节点
    │ 只代理 google 系域名，其余 DIRECT
    ▼
海外节点 → Google
```

## 目录约定（参照 3.110 惯例：配置 /data/base，运行数据 /data/data）

```
/data/base/searxng/                  ← 配置（compose + 两个服务配置 + 引擎补丁）
├── docker-compose.yml
├── mihomo/config.yaml               ← 含订阅 URL，权限 600，不入库
├── searxng/settings.yml
└── searxng/engines/*.py (5 个)      ← PR searxng/searxng#6892 引擎补丁

/data/data/searxng/                  ← 运行数据
└── mihomo/                          ← 订阅缓存、健康检查数据（不重要，可随时删）
```

## 部署步骤

1. **建目录传文件**（从本地物料目录 scp）：
   ```bash
   ssh root@192.168.3.110 'mkdir -p /data/base/searxng/{mihomo,searxng/engines} /data/data/searxng/mihomo'
   scp deploy-3110/docker-compose.yml root@192.168.3.110:/data/base/searxng/
   scp deploy-3110/searxng-settings.yml root@192.168.3.110:/data/base/searxng/searxng/settings.yml
   scp deploy-3110/engines/*.py root@192.168.3.110:/data/base/searxng/searxng/engines/
   scp deploy-3110/mihomo-config.yaml root@192.168.3.110:/data/base/searxng/mihomo/config.yaml
   ```

2. **服务器上填两处占位符**（凭证不入库，手工编辑）：
   - `/data/base/searxng/mihomo/config.yaml`：`REPLACE_WITH_SUBSCRIPTION_URL` → 机场订阅 URL
   - `/data/base/searxng/searxng/settings.yml`：`REPLACE_WITH_RANDOM_32BYTES_ON_SERVER` → `openssl rand -hex 16` 的输出
   - `chmod 600 /data/base/searxng/mihomo/config.yaml`

3. **起栈**：
   ```bash
   cd /data/base/searxng && docker compose up -d
   ```

4. **验证**（全绿才算完成）：
   ```bash
   # a. mihomo 拉到订阅且选了真节点（不是"剩余流量"信息节点）
   docker exec searxng-mihomo sh -c \
     'wget -q -O- http://127.0.0.1:9090/proxies/PROXY' | grep -o '"now":"[^"]*"'
   # b. 端到端：google 搜索返回精准结果
   docker run --rm --network searxng_default curlimages/curl -s \
     "http://searxng:8080/search?q=JJG+544&format=json&engines=google" | head -c 400
   # c. 抽查质量：首条应为检定规程相关，unresponsive 里无 google
   ```

## 已踩坑速查（本地全踩过一遍）

| 坑 | 症状 | 解 |
|---|---|---|
| mihomo 配置路径 | 挂 /etc/mihomo 不读，日志 "Can't find config" | 必须挂 `/root/.config/mihomo/config.yaml` |
| provider 路径 | fatal: path is not subpath of SAFE_PATHS | path 必须在 `/root/.config/mihomo/` 下 |
| 7890 连不上 | SearXNG 报 proxy error | `allow-lan: true` + `bind-address: 0.0.0.0` |
| 假节点 | 当前节点显示"剩余 xx GB" | PROXY 组用 `url-test` + filter 正则过滤 |
| 引擎补丁不生效 | 日志仍请求 /wml/search | **镜像预编译 pyc 会盖过 volume 挂载的 .py**；compose entrypoint 已固化 `rm -f .../google*.pyc` 再 exec 原入口（重建容器自动处理） |
| engines 参数 | `engines=go`/`bi` shortcut 不生效 | API 调用传引擎全名 `google`/`bing` |

## 运维

- **google 引擎补丁**：PR #6892 合入官方镜像后，删掉 compose 里 5 个 engines 挂载 + `docker compose up -d` 重建即回官方版
- **订阅更新**：mihomo 每 24h 自动拉；手动触发 `docker restart searxng-mihomo`
- **节点切换**：自动 url-test；google 出现 CAPTCHA 时多为节点 IP 被盯，重启 mihomo 换节点即可
- **日志**：`docker logs searxng` / `docker logs searxng-mihomo`；运行数据只有订阅缓存（/data/data/searxng/mihomo），可随时清

## 后续（未做，待立项）

- MCP pack 封装（web_search 工具 → SearXNG JSON API + 调用侧缓存）挂知识库运营侧
- 接入方式：llm-modeler 的 compose 网络连到 searxng_default，或 searxng 加入 llm-modeler 网络
