<template>
  <div>
    <!-- 工具条:搜索 + 计数 -->
    <div class="cv-toolbar">
      <a-input v-model:value="filterUserId" placeholder="按用户过滤" style="width: 200px" allow-clear
        @pressEnter="search">
        <template #prefix><UserOutlined style="color: #bbb" /></template>
      </a-input>
      <a-input v-model:value="filterQ" placeholder="搜标题或对话内容" style="width: 240px" allow-clear
        @pressEnter="search">
        <template #prefix><SearchOutlined style="color: #bbb" /></template>
      </a-input>
      <!-- 插件多选筛选:选项=已发现插件 + 「其他」(无路由记录的会话) -->
      <a-select v-model:value="filterPacks" mode="multiple" allow-clear
        placeholder="按插件筛选" style="min-width: 200px" :options="packOptions"
        @change="search" />
      <a-button type="primary" @click="search">查询</a-button>
      <span class="cv-count">共 {{ total }} 个会话</span>
    </div>

    <a-table
      :columns="columns"
      :data-source="rows"
      :pagination="pagination"
      :loading="loading"
      row-key="id"
      size="middle"
      @change="onTableChange"
    >
      <template #bodyCell="{ column, record }">
        <template v-if="column.key === 'title'">
          <!-- 标题是主信息列:占满剩余宽度,超长省略 + 悬停全文;绑定标记随行 -->
          <div class="cv-title-cell">
            <a-tooltip :title="record.displayTitle || record.title" placement="topLeft">
              <a class="cv-title" @click="openDetail(record)">{{ record.displayTitle || record.title }}</a>
            </a-tooltip>
            <a-tag v-if="record.contextKey" class="cv-ctx" :title="record.contextKey">
              {{ truncate(record.contextKey, 8) }}
            </a-tag>
          </div>
        </template>
        <template v-else-if="column.key === 'pack'">
          <a-tag v-if="record.pack" :color="packColor(record.pack)" class="cv-pack">{{ record.pack }}</a-tag>
          <span v-else class="cv-pack-other">其他</span>
        </template>
        <template v-else-if="column.key === 'userId'">
          <a-tooltip :title="record.userId" placement="topLeft">
            <span class="cv-user">{{ truncate(record.userId, 10) }}</span>
          </a-tooltip>
        </template>
        <template v-else-if="column.key === 'messageCount'">
          <a-tag :color="record.messageCount ? 'blue' : 'default'" class="cv-count-tag">
            {{ record.messageCount }}
          </a-tag>
        </template>
        <template v-else-if="column.key === 'updatedAt'">
          <span class="cv-time">{{ fmtTime(record.updatedAt) }}</span>
        </template>
        <template v-else-if="column.key === 'actions'">
          <a-space :size="12">
            <a @click="openDetail(record)">链路</a>
            <a-popconfirm title="删除该会话(含全部消息与链路记录)?" ok-text="删除" cancel-text="取消"
              @confirm="remove(record)">
              <a class="cv-danger">删除</a>
            </a-popconfirm>
          </a-space>
        </template>
      </template>
    </a-table>

    <!-- 链路追踪抽屉 -->
    <a-drawer v-model:open="detailOpen" width="760" :title="detailTitle" class="cv-drawer">
      <ConversationTrace v-if="detailOpen && detailId" :conv-id="detailId" />
    </a-drawer>
  </div>
</template>

<script setup lang="ts">
// 会话管理:卡片化列表(displayTitle/用户名省略)+ 链路抽屉 + 删除。
import { computed, inject, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import { UserOutlined, SearchOutlined } from '@ant-design/icons-vue'
import { AdminConversation, deleteConversation, fetchConversations, fetchPacks, fmtTime } from '../api'
import type { LoadSafely } from './loadSafely'
import ConversationTrace from './ConversationTrace.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

const rows = ref<AdminConversation[]>([])
const total = ref(0)
const loading = ref(false)
const filterUserId = ref('')
const filterQ = ref('')
const filterPacks = ref<string[]>([])
// 筛选选项:已发现插件 + 「其他」(__other__ 占位,请求时转空串元素)
const packOptions = ref<{ label: string; value: string }[]>([])

// 已知插件配色(与调用日志类型色系一致);未列出的插件给中性色
const PACK_COLORS: Record<string, string> = {
  njmind_form: 'blue',
  leave_application: 'gold',
  knowledge_graph: 'geekblue',
}
function packColor(p: string): string {
  return PACK_COLORS[p] || 'default'
}

async function loadPackOptions() {
  try {
    const data = await fetchPacks()
    const opts = data.items.map((p) => ({ label: p.name, value: p.name }))
    packOptions.value = [...opts, { label: '其他', value: '__other__' }]
  } catch {
    packOptions.value = [{ label: '其他', value: '__other__' }]
  }
}

const page = reactive({ current: 1, pageSize: 20 })
const pagination = computed(() => ({
  total: total.value,
  current: page.current,
  pageSize: page.pageSize,
  showSizeChanger: true,
  pageSizeOptions: ['10', '20', '50', '100'],
  showTotal: (t: number) => `共 ${t} 条`,
}))

const columns = [
  { title: '会话', key: 'title' },  // 不设宽:占满剩余空间(主信息列)
  { title: '插件', key: 'pack', width: 130, ellipsis: true },
  { title: '用户', key: 'userId', width: 120, ellipsis: true },
  { title: '消息', key: 'messageCount', width: 64 },
  { title: '更新时间', key: 'updatedAt', width: 165 },
  { title: '操作', key: 'actions', width: 116 },
]

const detailId = ref('')
const detailTitle = ref('链路追踪')
const detailOpen = ref(false)

function truncate(s: string, n: number): string {
  return s && s.length > n ? `${s.slice(0, n)}…` : (s || '-')
}

async function load() {
  loading.value = true
  await loadSafely(async () => {
    const data = await fetchConversations({
      limit: page.pageSize,
      offset: (page.current - 1) * page.pageSize,
      userId: filterUserId.value.trim() || undefined,
      q: filterQ.value.trim() || undefined,
      // __other__ → 空串元素(后端语义:无路由记录的会话)
      packs: filterPacks.value.length
        ? filterPacks.value.map((p) => (p === '__other__' ? '' : p)).join(',')
        : undefined,
    })
    rows.value = data.items
    total.value = data.total
  })
  loading.value = false
}

function search() {
  page.current = 1
  load()
}

function onTableChange(pag: { current?: number; pageSize?: number }) {
  if (pag.current) page.current = pag.current
  if (pag.pageSize) {
    page.pageSize = pag.pageSize
    page.current = 1
  }
  load()
}

function openDetail(record: AdminConversation) {
  detailId.value = record.id
  detailTitle.value = `链路追踪 · ${record.displayTitle || record.title}`
  detailOpen.value = true
}

async function remove(record: AdminConversation) {
  await loadSafely(async () => {
    await deleteConversation(record.id)
    message.success(`已删除「${record.displayTitle || record.title}」`)
    if (rows.value.length === 1 && page.current > 1) page.current -= 1
    load()
  })
}

onMounted(() => { loadPackOptions(); load() })
</script>

<style scoped>
.cv-toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0 16px; }
.cv-count { margin-left: auto; color: #9ca3af; font-size: 12px; }
/* 标题主列:占满剩余宽度,超长省略,悬停 tooltip 看全文 */
.cv-title-cell { display: flex; align-items: center; gap: 8px; min-width: 0; width: 100%; }
.cv-title {
  flex: 1; min-width: 0;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  display: block;
}
.cv-ctx { flex-shrink: 0; font-size: 11px; }
.cv-user {
  display: inline-block; max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  color: #4b5563; font-size: 13px;
}
.cv-pack { font-size: 11.5px; max-width: 110px; }
.cv-pack :deep(.ant-tag) { overflow: hidden; text-overflow: ellipsis; }
.cv-pack-other { color: #9ca3af; font-size: 12px; }
.cv-count-tag { min-width: 30px; text-align: center; }
.cv-time { font-size: 12.5px; color: #374151; white-space: nowrap; }
.cv-danger { color: #ef4444; }
</style>
