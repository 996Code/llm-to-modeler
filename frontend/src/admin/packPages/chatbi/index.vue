<template>
  <!-- 智能问数管理页:数据源/保存查询/看板/图谱/记忆 五个平级 Tab(单层,不再嵌套) -->
  <div class="chatbi-admin">
    <a-tabs v-model:activeKey="tab" size="large" class="flat-tabs">
      <a-tab-pane key="ds">
        <template #tab><DatabaseOutlined /> 数据源</template>
        <DsManager ref="dsManagerRef" @open-graph="openGraphTab" @loaded="onDsLoaded" />
      </a-tab-pane>
      <a-tab-pane key="queries">
        <template #tab><HistoryOutlined /> 保存查询</template>
        <SavedQueries @add-to-dash="onAddToDash" />
      </a-tab-pane>
      <a-tab-pane key="dash">
        <template #tab><LayoutOutlined /> 看板</template>
        <Dashboards ref="dashRef" />
      </a-tab-pane>
      <a-tab-pane key="graph">
        <template #tab><PartitionOutlined /> 图谱</template>
        <SchemaGraphTab :ds-id="graphDsId" :datasources="datasourceOptions" />
      </a-tab-pane>
      <a-tab-pane key="memory">
        <template #tab><BulbOutlined /> 记忆</template>
        <Memories />
      </a-tab-pane>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
// 智能问数管理页 —— 五个平级 Tab 的编排壳:
//   数据源(DsManager) / 保存查询(SavedQueries) / 看板(Dashboards) /
//   图谱(SchemaGraphTab) / 记忆(Memories)
// 职责:Tab 切换编排 + 跨 Tab 联动(数据源→图谱、保存查询→看板)。
import { onMounted, ref } from 'vue'
import {
  BulbOutlined, DatabaseOutlined, HistoryOutlined, LayoutOutlined, PartitionOutlined,
} from '@ant-design/icons-vue'
import DsManager from './DsManager.vue'
import SavedQueries from './SavedQueries.vue'
import Dashboards from './Dashboards.vue'
import SchemaGraphTab from './SchemaGraphTab.vue'
import Memories from './Memories.vue'

const tab = ref('ds')

// 跨 Tab 联动状态
const graphDsId = ref('')            // 图谱 Tab 当前数据源
const datasourceOptions = ref<{ id: string; name: string }[]>([])
const dsManagerRef = ref<InstanceType<typeof DsManager> | null>(null)
const dashRef = ref<InstanceType<typeof Dashboards> | null>(null)

// 数据源页点「图谱」→ 记住目标数据源并切 Tab
function openGraphTab(dsId: string) {
  graphDsId.value = dsId
  tab.value = 'graph'
}

// 数据源列表就绪后同步给图谱 Tab 的下拉(DsManager onMounted 拉完列表即回调)
function onDsLoaded(options: { id: string; name: string }[]) {
  datasourceOptions.value = options
}

// 保存查询页「加到看板」→ 切到看板 Tab 并带入目标看板
function onAddToDash(payload: { dashboardId?: string }) {
  tab.value = 'dash'
  if (payload.dashboardId && dashRef.value) {
    dashRef.value.openDashboard(payload.dashboardId)
  }
}

onMounted(() => { /* Tab 均懒渲染(切到才挂载), 无需全局初始化 */ })
</script>

<style scoped>
.chatbi-admin { display: flex; flex-direction: column; }
</style>
