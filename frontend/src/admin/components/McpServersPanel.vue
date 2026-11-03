<template>
  <section class="mcp-panel">
    <div class="mcp-toolbar">
      <span class="mcp-count">
        共 {{ servers.length }} 个服务
        <a-tag class="mcp-key-tag" :color="keyConfigured ? 'green' : 'red'">
          {{ keyConfigured ? '对外 MCP 已启用' : '对外 MCP 未配置 Key' }}
        </a-tag>
        <template v-if="keyConfigured">
          <a-tooltip title="对外接入端点(外部 AI 工具连本系统)">
            <code class="mcp-endpoint">{{ mcpEndpoint }}</code>
          </a-tooltip>
          <a-input-password :value="apiKey" size="small"
                            style="width: 220px" readonly />
          <a-tooltip title="复制 Key">
            <a-button size="small" type="text" @click="copyKey">
            <template #icon><CopyOutlined /></template>
            </a-button>
          </a-tooltip>
          <a-tooltip title="刷新 Key(旧 Key 约 5 秒内失效)">
            <a-button size="small" type="text" :loading="rotatingKey" @click="rotateKey">
              <template #icon><SyncOutlined /></template>
            </a-button>
          </a-tooltip>
        </template>
        <a-tooltip v-else title="生成对外 MCP 接入 Key">
          <a-button size="small" type="text" :loading="rotatingKey" @click="rotateKey">
            <template #icon><SyncOutlined /></template>
          </a-button>
        </a-tooltip>
      </span>
      <a-button type="primary" size="small" @click="openCreate">
        <template #icon><PlusOutlined /></template>注册外部服务
      </a-button>
    </div>

    <a-table
      :columns="columns"
      :data-source="servers"
      :loading="loading"
      row-key="id"
      size="small"
      :pagination="false"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'name'">
          <div class="mcp-name-cell">
            <span class="mcp-name">{{ record.name }}</span>
            <span v-if="!record.description" class="mcp-name-warn">未填服务说明</span>
          </div>
        </template>
        <template v-else-if="column.key === 'status'">
          <a-tooltip :title="record.status === 'connected' ? '探测成功,工具快照已保存' : '上次探测失败,保持待连接'">
            <a-tag :color="record.status === 'connected' ? 'green' : 'orange'">
              {{ record.status === 'connected' ? '已连接' : '待连接' }}
            </a-tag>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'tools'">
          <a-tooltip v-if="record.tools_snapshot.length"
                     :title="record.tools_snapshot.map((t: McpTool) => t.name).join('、')">
            <a-tag color="purple">{{ record.tools_snapshot.length }} 个工具</a-tag>
          </a-tooltip>
          <a-tag v-else>-</a-tag>
          <a-tooltip v-if="undeclaredCount(record)"
                     :title="`${undeclaredCount(record)} 个工具声明不完整(描述缺失),不进入对话路由——请业务平台补充 MCP 声明`">
            <a-tag color="red">{{ undeclaredCount(record) }} 声明不完整</a-tag>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'ops'">
          <a-switch :checked="record.enabled !== false" size="small"
                    :loading="togglingId === record.id"
                    :disabled="togglingId !== '' && togglingId !== record.id"
                    @change="(v: any) => toggleEnabled(record, v)" />
          <span class="mcp-ops">
            <a-tooltip title="刷新快照(重新探测)">
              <a-button size="small" type="text" :loading="refreshing === record.id" @click="refresh(record)">
                <template #icon><ReloadOutlined /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip title="编辑">
              <a-button size="small" type="text" @click="openEdit(record)">
                <template #icon><EditOutlined /></template>
              </a-button>
            </a-tooltip>
            <a-tooltip title="删除">
              <a-button size="small" type="text" danger @click="remove(record)">
                <template #icon><DeleteOutlined /></template>
              </a-button>
            </a-tooltip>
          </span>
        </template>
      </template>
      <template #expandedRowRender="{ record }">
        <div v-if="record.tools_snapshot.length" class="mcp-tools">
          <div v-for="t in record.tools_snapshot" :key="t.name" class="mcp-tool">
            <span class="mcp-tool-name">{{ t.name }}</span>
            <span class="mcp-tool-desc">{{ t.description || '(无描述)' }}</span>
          </div>
        </div>
        <span v-else class="mcp-tools-empty">（无工具快照——连接成功后自动保存）</span>
      </template>
    </a-table>

    <a-modal
      v-model:open="open"
      :title="editing ? '编辑 MCP 服务' : '注册 MCP 服务'"
      :confirm-loading="saving"
      :mask-closable="false"
      @ok="save"
    >
      <a-form layout="vertical" style="margin-top: 12px">
        <a-form-item label="名称" required>
          <a-input v-model:value="form.name" placeholder="展示名,如 数据服务" />
        </a-form-item>
        <a-form-item label="服务说明（说清该系统提供什么业务，作为能力分组标题）">
          <a-textarea v-model:value="form.description" :rows="2"
                      placeholder="如：计量业务平台，提供委托单进度/证书/报告等查询" />
        </a-form-item>
        <a-form-item v-if="!editing" label="标识（插件引用用，小写字母/数字/-/_）" required>
          <a-input v-model:value="form.code" placeholder="如 ext_data" />
        </a-form-item>
        <a-form-item label="URL" required>
          <a-input v-model:value="form.url" placeholder="http://host:port/mcp" />
        </a-form-item>
        <a-form-item label="认证头（JSON）">
          <a-textarea v-model:value="form.headersText" :rows="3" placeholder='{"Authorization": "Bearer xxx"}' />
        </a-form-item>
        <a-form-item label="连接超时（秒）/ 读取超时（秒）">
          <a-input-number v-model:value="form.timeout" :min="1" style="width: 100px" />
          <a-input-number v-model:value="form.read_timeout" :min="1" style="width: 100px; margin-left: 8px" />
        </a-form-item>
      </a-form>
    </a-modal>
  </section>
</template>

<script setup lang="ts">
// 外部 MCP 服务面板(挂 PacksTab 页底分区):注册表 CRUD + 快照展开/刷新 + 对外接入 key。
// 与 PacksTab 同容器不新开顶级 tab(spec §5);错误统一走 loadSafely(401/4xx detail toast)。
import { inject, onMounted, ref } from 'vue'
import { message, Modal } from 'ant-design-vue'
import { CopyOutlined, SyncOutlined } from '@ant-design/icons-vue'
import {
  ApiOutlined, DeleteOutlined, EditOutlined, PlusOutlined,
  QuestionCircleOutlined, ReloadOutlined,
} from '@ant-design/icons-vue'
import {
  createMcpServer, deleteMcpServer, fetchMcpKeyStatus, listMcpServers,
  refreshMcpKey, refreshMcpServer, updateMcpServer,
  type McpServer, type McpTool,
  toggleMcpServer,
} from '../api'
import { copyText } from '../../utils/clipboard'
import type { LoadSafely } from './loadSafely'

const loadSafely = inject<LoadSafely>('loadSafely')!

const servers = ref<McpServer[]>([])
const emit = defineEmits<{ (e: 'count', s: { total: number; enabled: number }): void }>()
const loading = ref(false)
const togglingId = ref('')
const keyConfigured = ref(false)
// 对外端点带项目前缀(网关剥前缀转后端 /api/mcp;vite base 与生产 nginx 同约定)
const mcpEndpoint = `${window.location.origin}/ai-modeler/api/mcp`
const apiKey = ref('')
const rotatingKey = ref(false)
const open = ref(false)
const saving = ref(false)
const editing = ref<McpServer | null>(null)
const refreshing = ref('')
const form = ref({ name: '', code: '', url: '', description: '', headersText: '{}', timeout: 30, read_timeout: 300 })

const columns = [
  { title: '名称', key: 'name' },
  { title: '标识', dataIndex: 'code' },
  { title: 'URL', dataIndex: 'url', ellipsis: true },
  { title: '状态', key: 'status', width: 90 },
  { title: '工具数', key: 'tools', width: 130 },
  { title: '操作', key: 'ops', width: 130 },
]

/** 快照内声明不完整的工具数(描述缺失/过短)——提示提供方补声明。 */
function undeclaredCount(s: McpServer): number {
  return (s.tools_snapshot || []).filter(
    (t) => !t.description || t.description.trim().length < 6).length
}

async function load() {
  loading.value = true
  await loadSafely(async () => {
    servers.value = await listMcpServers()
    emit('count', { total: servers.value.length,
                   enabled: servers.value.filter((s) => s.enabled !== false).length })
    const st = await fetchMcpKeyStatus()
    keyConfigured.value = st.configured
    apiKey.value = st.apiKey || ''
  })
  loading.value = false
}
onMounted(load)

function openCreate() {
  editing.value = null
  form.value = { name: '', code: '', url: '', description: '', headersText: '{}', timeout: 30, read_timeout: 300 }
  open.value = true
}
function openEdit(s: McpServer) {
  editing.value = s
  form.value = {
    name: s.name, code: s.code, url: s.url, description: s.description || '',
    headersText: JSON.stringify(s.headers ?? {}, null, 2),
    timeout: s.timeout, read_timeout: s.read_timeout,
  }
  open.value = true
}

async function save() {
  let headers: Record<string, string>
  try {
    headers = JSON.parse(form.value.headersText || '{}')
    if (typeof headers !== 'object' || headers === null || Array.isArray(headers)) {
      message.error('认证头必须是 JSON 对象(键值对)')
      return
    }
  } catch {
    message.error('认证头必须是合法 JSON')
    return
  }
  saving.value = true
  let ok = false
  await loadSafely(async () => {
    const body = {
      name: form.value.name.trim(), url: form.value.url.trim(),
      description: form.value.description.trim(),
      headers,
      timeout: form.value.timeout ?? 30, read_timeout: form.value.read_timeout ?? 300,
    }
    const r = editing.value
      ? await updateMcpServer(editing.value.id, body)
      : await createMcpServer({ ...body, code: form.value.code.trim() })
    ok = true
    message.info(r.status === 'connected'
      ? '已连接，工具快照已保存'
      : '已保存，但连接失败（待连接）')
  })
  saving.value = false
  if (ok) {
    open.value = false
    await load()
  }
}

function remove(s: McpServer) {
  Modal.confirm({
    title: `删除「${s.name}」？`,
    content: '删除后插件将无法再引用该 MCP 服务。',
    okText: '删除',
    okType: 'danger',
    onOk: async () => {
      await loadSafely(async () => { await deleteMcpServer(s.id) })
      await load()
    },
  })
}

async function refresh(s: McpServer) {
  refreshing.value = s.id
  await loadSafely(async () => {
    const r = await refreshMcpServer(s.id)
    message.info(r.status === 'connected'
      ? `快照已刷新（${r.tools_snapshot.length} 个工具）`
      : '连接失败，保持待连接')
  })
  refreshing.value = ''
  await load()
}


async function rotateKey() {
  const doRotate = async () => {
    rotatingKey.value = true
    let ok = false
    try {
      await loadSafely(async () => {
        const st = await refreshMcpKey()
        apiKey.value = st.apiKey
        keyConfigured.value = st.configured
        ok = true
      })
      if (ok) message.success('新 Key 已生成(旧 Key 已失效)')
    } finally {
      rotatingKey.value = false
    }
  }
  if (keyConfigured.value) {
    Modal.confirm({
      title: '刷新接入 Key?',
      content: '旧 Key 约 5 秒内失效,已接入的外部工具需改用新 Key(如 self_test 注册的认证头)。',
      onOk: doRotate,
    })
  } else {
    await doRotate()
  }
}

async function copyKey() {
  const ok = await copyText(apiKey.value)
  if (ok) message.success('已复制')
  else message.warning('复制失败,请手动选择')
}

async function toggleEnabled(rec: McpServer, on: boolean) {
  togglingId.value = rec.id
  try {
    await loadSafely(async () => {
      await toggleMcpServer(rec.id, on)
      await load()
    })
  } finally {
    togglingId.value = ''
  }
}

</script>

<style scoped>
/* 精装层(2026-11 重设计):令牌见 shared/design-tokens.css,规格对齐 design-demos/polished-packs.html */
.mcp-panel { display: flex; flex-direction: column; gap: 16px; min-width: 0; }
.mcp-toolbar { display: flex; align-items: center; flex-wrap: wrap; justify-content: space-between; gap: 12px; margin: 0 2px 12px; }
.mcp-count { font-size: 14px; color: var(--ink-2); display: inline-flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.mcp-key-tag { margin: 0 4px; }
.mcp-endpoint {
  background: var(--brand-tint); padding: 3px 8px; border-radius: var(--radius-ctrl);
  font-size: 12px; color: var(--brand-ink);
  font-family: 'SF Mono', Menlo, Consolas, monospace;
}
/* 标题行(独立顶级页形态) */
.pk-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  margin: 2px 2px 14px;
}
.pk-toolbar-title {
  font-size: 14px; font-weight: 600; color: var(--ink);
  display: inline-flex; align-items: center; gap: 8px;
}
.pk-toolbar-count { margin-left: 2px; font-weight: 400; }
.pk-help-link { color: var(--ink-3); font-size: 13px; display: inline-flex; align-items: center; gap: 4px; }
.pk-help-link:hover { color: var(--brand-ink); }
.pk-help-title { font-weight: 600; margin-bottom: 6px; display: flex; align-items: center; gap: 6px; color: var(--brand-ink); }
.pk-help-body { font-size: 12px; color: var(--ink-2); line-height: 1.7; max-width: 380px; }
.pk-help-body code {
  background: var(--brand-tint); padding: 1px 6px; border-radius: 4px; font-size: 12px; color: var(--brand-ink);
}

.mcp-ops { display: inline-flex; gap: 2px; }
.mcp-name-cell { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.mcp-name { font-weight: 600; color: var(--ink); }
.mcp-name-warn { font-size: 12px; color: var(--danger); }
.mcp-tools { display: flex; flex-direction: column; gap: 4px; }
.mcp-tool { display: flex; gap: 10px; font-size: 12px; line-height: 1.6; }
.mcp-tool-name {
  font-family: 'SF Mono', Menlo, Consolas, monospace; color: var(--violet);
  white-space: nowrap;
}
.mcp-tool-desc { color: var(--ink-2); word-break: break-all; }
.mcp-tools-empty { font-size: 12px; color: var(--ink-3); }

/* 状态/标记 a-tag 胶囊化并把 antd 预设色归一到语义令牌(已连接 ok / 待连接 warn / 声明不完整 danger / 工具数 violet) */
.mcp-panel :deep(.ant-tag) { border-radius: 999px; font-size: 12px; line-height: 18px; padding: 0 9px; }
.mcp-panel :deep(.ant-tag-green) { color: var(--ok); background: var(--ok-tint); border-color: var(--ok-tint); }
.mcp-panel :deep(.ant-tag-red) { color: var(--danger); background: var(--danger-tint); border-color: var(--danger-tint); }
.mcp-panel :deep(.ant-tag-orange) { color: var(--warn); background: var(--warn-tint); border-color: var(--warn-tint); }
.mcp-panel :deep(.ant-tag-purple) { color: var(--violet); background: var(--violet-tint); border-color: var(--violet-tint); }
</style>
