<template>
  <div class="kg-page">
    <!-- 顶部:知识库选择 + 刷新(全页共享当前库) -->
    <div class="kg-bar">
      <div class="kg-bar-title">
        <PartitionOutlined class="kg-bar-icon" />
        <span>当前知识库</span>
      </div>
      <a-select
        v-model:value="currentKbId" style="min-width: 260px" placeholder="选择知识库"
        :loading="loadingKbs" @change="onKbChange"
      >
        <a-select-option v-for="k in kbs" :key="k.id" :value="k.id">
          {{ k.name }}
          <span class="kg-bar-meta">{{ k.docCount ?? 0 }} 文档 / {{ k.entityTotal ?? 0 }} 实体</span>
        </a-select-option>
      </a-select>
      <a-button @click="refreshAll"><SyncOutlined :spin="loadingKbs" /> 刷新</a-button>
      <span class="kg-bar-tip">依赖 Neo4j(图谱) + Milvus(向量);连接在插件「设置」里配置</span>
    </div>

    <a-tabs v-model:activeKey="tab" size="small">
      <a-tab-pane key="docs">
        <template #tab><FileTextOutlined /> 文档导入</template>
        <DocManager v-if="currentKbId" :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
        <a-empty v-else description="先在「知识库」页创建或选择一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
      <a-tab-pane key="graph">
        <template #tab><NodeIndexOutlined /> 图谱浏览</template>
        <GraphView v-if="currentKbId" :kb-id="currentKbId" :schema="currentKb?.schema || null" :refresh-tick="refreshTick" />
        <a-empty v-else description="先选择一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
      <a-tab-pane key="schema">
        <template #tab><ApartmentOutlined /> 本体设置</template>
        <SchemaEditor v-if="currentKbId" :kb-id="currentKbId" :refresh-tick="refreshTick" @changed="loadKbs" />
        <a-empty v-else description="先选择一个知识库" style="padding: 60px 0" />
      </a-tab-pane>
      <a-tab-pane key="kbs">
        <template #tab><DatabaseOutlined /> 知识库管理</template>
        <KbManager
          :kbs="kbs" :loading="loadingKbs" :selected="currentKbId"
          @refresh="loadKbs" @select="(id: string) => { currentKbId = id; tab = 'docs' }"
        />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
// 知识图谱管理页(manifest admin.page=knowledge_graph → AdminApp 动态 Tab 挂载)。
// 结构:共享"当前知识库"选择条 + 四个子页(文档导入/图谱浏览/本体设置/库管理)。
import { computed, inject, onMounted, ref } from 'vue'
import {
  ApartmentOutlined, DatabaseOutlined, FileTextOutlined,
  NodeIndexOutlined, PartitionOutlined, SyncOutlined,
} from '@ant-design/icons-vue'
import { KgKnowledgeBase, fetchKgKbs } from '../../api'
import type { LoadSafely } from '../../components/loadSafely'
import KbManager from './KbManager.vue'
import DocManager from './DocManager.vue'
import SchemaEditor from './SchemaEditor.vue'
import GraphView from './GraphView.vue'

const loadSafely = inject<LoadSafely>('loadSafely')!

const kbs = ref<KgKnowledgeBase[]>([])
const currentKbId = ref('')
const loadingKbs = ref(false)
const tab = ref('docs')
// 刷新联动:点「刷新」时自增,子页(文档/图谱/本体)watch 它重载
const refreshTick = ref(0)

const currentKb = computed(() => kbs.value.find((k) => k.id === currentKbId.value))

function refreshAll() {
  refreshTick.value++
  loadKbs()
}

async function loadKbs() {
  loadingKbs.value = true
  await loadSafely(async () => {
    kbs.value = await fetchKgKbs()
    if (currentKbId.value && !kbs.value.some((k) => k.id === currentKbId.value)) {
      currentKbId.value = ''
    }
    if (!currentKbId.value && kbs.value.length === 1) {
      currentKbId.value = kbs.value[0].id
    }
  })
  loadingKbs.value = false
}

function onKbChange() {
  // 切库时子页按 kbId prop 自行重载(响应式),这里无需额外动作
}

onMounted(loadKbs)
</script>

<style scoped>
.kg-page { padding: 6px 2px 0; }
.kg-bar {
  display: flex; align-items: center; gap: 12px; margin-bottom: 14px;
  background: linear-gradient(90deg, #f5f7ff, #fafbff);
  border: 1px solid #e6ebf5; border-radius: 10px; padding: 10px 16px;
}
.kg-bar-title { display: flex; align-items: center; gap: 6px; font-weight: 600; color: #1f2937; }
.kg-bar-icon { color: #2f54eb; font-size: 16px; }
.kg-bar-meta { color: #94a3b8; font-size: 12px; margin-left: 8px; }
.kg-bar-tip { margin-left: auto; color: #9ca3af; font-size: 12px; }
</style>
