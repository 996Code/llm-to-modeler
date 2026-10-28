<template>
  <!-- 智能问数管理页:数据源/语义层/保存查询/看板/图谱/记忆/规则 七个平级 Tab(单层,不再嵌套) -->
  <div class="chatbi-admin">
    <a-tabs v-model:activeKey="tab" class="flat-tabs">
      <a-tab-pane key="ds">
        <template #tab><DatabaseOutlined /> 数据源</template>
        <DsManager ref="dsManagerRef" @open-graph="openGraphTab" @open-semantic="openSemanticTab" @loaded="onDsLoaded" />
      </a-tab-pane>
      <a-tab-pane key="semantic">
        <template #tab><ProfileOutlined /> 语义层</template>
        <SemanticLayer :ds-id="semanticDsId" :datasources="datasourceOptions" />
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
        <SchemaGraphTab v-if="tab === 'graph'" :ds-id="graphDsId" :datasources="datasourceOptions" />
      </a-tab-pane>
      <a-tab-pane key="memory">
        <template #tab><BulbOutlined /> 记忆</template>
        <Memories />
      </a-tab-pane>
      <a-tab-pane key="skills">
        <template #tab><FileTextOutlined /> 规则</template>
        <Skills />
      </a-tab-pane>
      <template #rightExtra>
        <a-popover :key="tab" placement="bottomRight" trigger="click">
          <template #content>
            <div class="help-pop help-pop-wide" v-html="currentHelpHtml"></div>
          </template>
          <a-button type="text" size="small" :aria-label="currentHelpTitle" class="help-link">
            <template #icon><QuestionCircleOutlined /></template>说明
          </a-button>
        </a-popover>
      </template>
    </a-tabs>
  </div>
</template>

<script setup lang="ts">
// 智能问数管理页 —— 七个平级 Tab 的编排壳:
//   数据源(DsManager) / 语义层(SemanticLayer) / 保存查询(SavedQueries) /
//   看板(Dashboards) / 图谱(SchemaGraphTab) / 记忆(Memories) / 规则(Skills)
// 职责:Tab 切换编排 + 跨 Tab 联动(数据源→语义层/图谱、保存查询→看板)。
import { computed, onMounted, ref } from 'vue'
import {
  BulbOutlined, DatabaseOutlined, FileTextOutlined, HistoryOutlined,
  LayoutOutlined, PartitionOutlined, ProfileOutlined, QuestionCircleOutlined,
} from '@ant-design/icons-vue'
import { chip, iconBtn, rules } from '../../helpHtml'
import DsManager from './DsManager.vue'
import SemanticLayer from './SemanticLayer.vue'
import SavedQueries from './SavedQueries.vue'
import Dashboards from './Dashboards.vue'
import SchemaGraphTab from './SchemaGraphTab.vue'
import Memories from './Memories.vue'
import Skills from './Skills.vue'

const tab = ref('ds')

// ── 说明(与其他页同标准:编号条目+彩色标签+按钮图标引用) ──
const helpHtmlMap: Record<string, { title: string; html: string }> = {
  ds: {
    title: '数据源说明',
    html: rules([
      '接入数据库后自动解析表结构;语义层、图谱、查询与记忆都基于这里的数据源。',
      `行内按钮(悬停有提示):${iconBtn('heart', '健康探测')} ${iconBtn('experiment', '指标')} ${iconBtn('scan', '扫描(合成语义层)')} ${iconBtn('profile', '语义层')} ${iconBtn('partition', '图谱')} ${iconBtn('delete', '删除', 'red')}。`,
      `扫描完成后语义层/图谱才可用;切换向量后端或检索异常时,用 ${iconBtn('database', '重建索引')} 兜底。`,
    ]),
  },
  semantic: {
    title: '语义层说明',
    html: rules([
      '在扫描合成的草稿上完善指标口径、维度与别名,保存后参与查询理解。',
      '还没扫描的数据源,先回「数据源」页扫描。',
    ]),
  },
  queries: {
    title: '保存查询说明',
    html: rules([
      '保存的查询可一键导出、或加入看板固定跟踪。',
      '列表里的行数是<b>保存时的快照</b>;导出和看板会重新查询实时数据,行数可能不同。',
    ]),
  },
  dash: {
    title: '看板说明',
    html: rules([
      '把常用查询固定成卡片,一屏跟踪核心指标;支持整页刷新全部卡片。',
    ]),
  },
  graph: {
    title: '图谱说明',
    html: rules([
      '可视化表与表之间的关系:点击节点看字段详情,支持检索定位。',
      `${iconBtn('delete', '删除关系', 'red')} 需确认后生效。`,
    ]),
  },
  memory: {
    title: '记忆说明',
    html: rules([
      `${chip('green', '生效中')} 的记忆参与查询理解;已整理的原始记忆已合并,不再参与召回。`,
    ]),
  },
  skills: {
    title: '规则说明',
    html: rules([
      '维护查询改写与口径修正规则,保存后参与查询理解。',
    ]),
  },
}
const currentHelp = computed(() => helpHtmlMap[tab.value] || helpHtmlMap.ds)
const currentHelpHtml = computed(() => currentHelp.value.html)
const currentHelpTitle = computed(() => currentHelp.value.title)

// 跨 Tab 联动状态
const graphDsId = ref('')            // 图谱 Tab 当前数据源
const semanticDsId = ref('')         // 语义层 Tab 当前数据源
const datasourceOptions = ref<{ id: string; name: string }[]>([])
const dsManagerRef = ref<InstanceType<typeof DsManager> | null>(null)
const dashRef = ref<InstanceType<typeof Dashboards> | null>(null)

// 数据源页点「语义层」→ 记住目标数据源并切 Tab
function openSemanticTab(dsId: string) {
  semanticDsId.value = dsId
  tab.value = 'semantic'
}

// 数据源页点「图谱」→ 记住目标数据源并切 Tab
function openGraphTab(dsId: string) {
  graphDsId.value = dsId
  tab.value = 'graph'
}

// 数据源列表就绪后同步给语义层/图谱 Tab 的下拉(DsManager 拉完列表即回调)
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
.chatbi-admin { display: flex; flex-direction: column; min-width: 0; font-size: 13px; }
.chatbi-admin :deep(.flat-tabs > .ant-tabs-nav) { margin-bottom: var(--admin-header-gap, 12px); }
.chatbi-admin :deep(.ant-tabs-tab) { font-size: 13px; }
.chatbi-admin :deep(.ant-table) { font-size: 13px; }
</style>
