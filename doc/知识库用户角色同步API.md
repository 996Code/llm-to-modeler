# 知识库用户/角色同步 API（对接指南）

> 面向对接方：把你们系统里的**用户**、**角色**同步到平台，并保证运行时请求带上**同一套 id** 的身份。全部接口为普通 HTTP REST，任何能发 HTTP 请求的程序（定时任务、消息消费者、手工 curl）都能调。

你要做的就两件事：

1. **推数据**——调本文 §3–§6 的接口把用户/角色目录同步过来：首次全量，之后增量；
2. **带身份**——你们调平台的对话/检索接口时，带上与同步目录一致的用户 id 和角色（§8）。权限过滤和按用户的数据隔离靠这一步生效。

---

## 1. 接入准备

### 基地址

```
http://<服务器地址>/ai-modeler/api/packs/knowledge_graph
```

下文所有接口路径都拼在这个基地址后面。

### 鉴权（三种方式任选其一，推荐第一种）

| 方式 | 你要怎么做 |
|---|---|
| **MCP 密钥（推荐）** | 每个请求带头 `X-Api-Key: <MCP 密钥>`。密钥向平台管理员索取（即平台管理端「设置 → MCP」里生成的那把，轮换后约 5 秒生效） |
| 管理口令 | 每个请求带头 `X-Admin-Token: <ADMIN_TOKEN>`（向平台管理员索取） |
| Bearer token | 先 `POST /ai-modeler/api/auth/token`、body `{"secret": "<ADMIN_TOKEN>"}` 换取 24h 有效的 token，之后每个请求带 `Authorization: Bearer <token>` |

规则：带了 `X-Api-Key` 就必须正确——不匹配返回 401；平台尚未配置 MCP 密钥时该方式返回 503（换用口令方式即可）。若部署为开放模式（平台未配置口令），三种方式都不需要——以平台方告知为准。

## 2. 你要传的数据（就这三个字段）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | string | 是 | 你方系统的唯一标识（工号/账号/角色编码），≤128 字符；user 和 role 两类各自内部唯一 |
| `name` | string | 否 | 展示名（姓名/角色名），≤128 字符；不传则平台展示 `id` |
| `status` | string | 否 | `active`（默认，在用）/ `disabled`（停用：记录保留、不再参与授权配置） |

`createdAt`/`updatedAt` 是平台维护的只读字段，响应里会返回，不需要你传。**除 id/name/status 外不要传其他信息**（部门等属性平台不收）；将来若需要扩展字段，平台会在本契约上向后兼容地追加。

## 3. 全量同步（首次接入 / 周期对账）

```
POST /subjects/{subject_type}        # subject_type: user | role
```

Body：

```json
{
  "items": [
    {"id": "u001", "name": "张三"},
    {"id": "u002", "name": "李四", "status": "disabled"}
  ]
}
```

你要知道的语义：

- **清单即全量**：不在本次清单里的旧条目会被删除（你方删人，平台跟着删）；
- **重推安全**：同清单重推不产生任何变化，网络超时直接原样重发即可；
- **要么全成功要么全不生效**：单次调用整体一个事务，校验不过一条都不会写入；
- 单次上限 **20000** 条，超出返回 422——更大的量分批（首批全量，其余用 §4 增量补）。

响应（`count`=收到条数，`created/updated/unchanged/removed`=本次实际变化）：

```json
{"subjectType": "user", "count": 2, "created": 2, "updated": 0, "unchanged": 0, "removed": 0}
```

## 4. 增量同步（日常变更推送）

```
POST /subjects/{subject_type}/delta
```

Body（两个数组均可选、可为空）：

```json
{
  "upserts": [
    {"id": "u001", "name": "张三（改名）"},
    {"id": "u003", "name": "王五"},
    {"id": "u002", "status": "disabled"}
  ],
  "deletes": ["u009", "u999"]
}
```

你要知道的语义：

- **upserts 按 id 新建或合并**：本次带了哪个字段就改哪个，没带的字段保持原值（例：u001 只改名，status 不动）；
- **deletes 删不存在的 id 不会报错**——会如实放进响应的 `missing` 数组，重推已消费过的增量是安全的；
- 单次上限：upserts 5000 条 / deletes 5000 条；同批对同一 id 既 upsert 又 delete 时以 delete 为准（建议别这么写）。

响应：

```json
{
  "subjectType": "user",
  "upserts": 3, "inserted": 1, "updated": 2, "unchanged": 0,
  "deletes": 2, "deleted": 1, "missing": ["u999"]
}
```

## 5. 单条接口（核对 / 补录）

| 方法与路径 | 用途 | 说明 |
|---|---|---|
| `GET /subjects/{type}?q=&status=&page=&pageSize=` | 列表 | `q` 模糊搜 id/名称；`status` 过滤；传 `page`（从 1 起）+`pageSize`（1~200，默认 50）才分页，不传返回全部 |
| `GET /subjects/{type}/{id}` | 查单条 | 不存在 404 |
| `PUT /subjects/{type}/{id}` | 新增/改单条 | Body `{name?, status?}`；已存在则只改你带的字段，不存在则新建。重试安全 |
| `DELETE /subjects/{type}/{id}` | 删单条 | 成功 `{"success": true}`；不存在 404 |

## 6. 同步状态（对账用）

```
GET /sync/status
```

```json
{
  "user":  {"total": 128, "active": 126, "disabled": 2,
             "lastSyncAt": "2026-10-29T06:00:12+00:00", "lastMode": "delta"},
  "role":  {"total": 8, "active": 8, "disabled": 0},
  "recent": [
    {"subjectType": "user", "mode": "delta",
     "summary": {"upserts": 3, "inserted": 1, "updated": 2, "deleted": 1},
     "syncedAt": "2026-10-29T06:00:12+00:00"}
  ]
}
```

`lastMode` ∈ `full` / `delta` / `upsert` / `delete`；`recent` 为最近 20 次操作流水。可用于核对"平台这边到底收到多少"。

## 7. 错误码

| 状态码 | 什么时候出现 | 响应体 |
|---|---|---|
| 401 | 鉴权不对：token 缺失/过期、口令不匹配、`X-Api-Key` 与平台密钥不一致 | `{"detail": "..."}` |
| 404 | 查/删的 id 不存在 | `{"detail": "用户不存在: u999"}` |
| 422 | 参数不对：items 非数组、条目缺 id、批内 id 重复、超单次上限、name/status 非法、分页参数越界 | `{"detail": "<具体原因>"}`（含出错位置如 `items[3].id`） |
| 503 | 带了 `X-Api-Key` 但平台尚未配置 MCP 密钥 | `{"detail": "MCP 未启用:管理端未配置 MCP API Key"}` |
| 5xx | 平台侧异常 | `{"detail": "..."}`（失败不会写入部分数据——整批生效或整批不生效） |

## 8. 运行时怎么带身份（必须与同步的 id 一致）

同步只是第一步；你们后续调平台的**对话/检索**接口时，要把同一个用户的 id 和他的角色带上——**用户 id 用同步时的 `user.id`，角色用同步时的 `role.id`**，两边对上号，权限过滤和按用户的数据隔离才会生效。不带身份 = 平台不做权限过滤、会话全部归到公共的 anonymous（不隔离）。

### 你直接调平台 API 时

| 你调的接口 | 用户身份怎么带 | 角色怎么带 |
|---|---|---|
| 对话 `POST /ai-modeler/api/config/chat` | 头 `X-User-Id: u001`（同时决定会话归属：历史列表/载入都只看这个用户的） | body 里 `pack_params: {"knowledge_graph": {"user_roles": ["legal"]}}`（数组或逗号分隔串） |
| 知识库检索 `POST /ai-modeler/api/packs/knowledge_graph/search` | 头 `X-User-Id: u001` | body `roles`（数组或逗号分隔串） |
| 引用文件下载 `GET /ai-modeler/api/packs/knowledge_graph/kbs/{kb}/documents/{doc}/download` | 头 `X-User-Id: u001` | 头 `X-User-Roles: legal,admin`（逗号分隔） |

### 你用浮窗 SDK 嵌入时

```html
<script src="http://<服务器>:13080/ai-modeler/embed.js"></script>
<script>
  const modeler = new LLMFormModeler({
    baseUrl: 'http://<服务器>/ai-modeler',
    userId: 'u001',                    // ← 当前登录用户的 id，与同步的 user.id 一致
    getArtifact: () => myConfig,
    onConfigApply: (c) => render(c),
  })
  modeler.open()
</script>
```

- `userId` 传当前登录用户即可，SDK 会自动给所有请求带上 `X-User-Id`，会话隔离随之生效（每个用户只见自己的历史对话）；
- 当前用户的角色二选一给 SDK：宿主在 `GET_CONTEXT` 的响应里带 `packParams: {"knowledge_graph": {"user_roles": [...]}}`（推荐）；或轻嵌入场景直接在 iframe 地址后拼 `?roles=legal,admin`；
- 用户切换登录时调 `modeler.setContext({ userId: 'u002' })`，并同步更新角色来源。

## 9. curl 端到端示例

```bash
BASE=http://<服务器>/ai-modeler/api/packs/knowledge_graph
AUTH='X-Api-Key: <MCP 密钥>'

# ① 首次全量：用户 + 角色
curl -s -X POST "$BASE/subjects/user" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "items": [
    {"id": "u001", "name": "张三"},
    {"id": "u002", "name": "李四"},
    {"id": "u003", "name": "王五"}
  ]}'
curl -s -X POST "$BASE/subjects/role" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "items": [{"id": "admin", "name": "管理员"}, {"id": "legal", "name": "法务"}]}'

# ② 日常增量：新人入职 + 改名 + 离职停用 + 删除
curl -s -X POST "$BASE/subjects/user/delta" -H "$AUTH" -H 'Content-Type: application/json' -d '{
  "upserts": [
    {"id": "u004", "name": "赵六"},
    {"id": "u002", "name": "李四丰"},
    {"id": "u003", "status": "disabled"}
  ],
  "deletes": ["u009"]}'

# ③ 核对：列表（分页+搜索）/ 单条 / 同步状态
curl -s "$BASE/subjects/user?q=张&status=active&page=1&pageSize=20" -H "$AUTH"
curl -s "$BASE/subjects/user/u001" -H "$AUTH"
curl -s "$BASE/sync/status" -H "$AUTH"

# ④ 补录单条
curl -s -X PUT "$BASE/subjects/role/audit" -H "$AUTH" \
     -H 'Content-Type: application/json' -d '{"name": "审计"}'
```

## 10. 建议的接入节奏

- **首次**：user、role 各推一次全量建立基线；
- **日常**：定时（每小时/每日）推增量；
- **对账**：建议每周推一次全量（全量按差异落库，重推代价低）+ 用 §6 核对计数；
- **重试**：全量与增量 upsert 都幂等，超时原样重发即可；增量 deletes 重推只会得到 `missing` 提示，无副作用；
- **并发**：同一类型（user 或 role）的同步请串行发起；两类之间无要求；
- **大批量**：超单次上限就分批（全量首批 + 增量补齐）。
