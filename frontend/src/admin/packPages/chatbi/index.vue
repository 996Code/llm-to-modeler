<template>
  <div class="chatbi-admin">
    <a-tabs v-model:activeKey="tab">
      <a-tab-pane key="ds">
        <template #tab><DatabaseOutlined /> 数据源</template>
    <!-- ══════════ 数据源管理 ══════════ -->
    <a-card title="数据源" class="section-card">
      <template #extra>
        <a-button type="primary" size="small" @click="showCreate = true">
          <PlusOutlined /> 添加数据源
        </a-button>
      </template>
      <a-table :data-source="datasources" :loading="loadingDs" row-key="id" size="small"
               :pagination="false">
        <a-table-column title="名称" data-index="name" />
        <a-table-column title="类型" data-index="dbType" width="90" />
        <a-table-column title="地址" width="220">
          <template #default="{ record }">{{ record.host }}:{{ record.port }}/{{ record.database }}</template>
        </a-table-column>
        <a-table-column title="状态" width="80">
          <template #default="{ record }">
            <a-tag :color="record.isActive ? 'green' : 'default'">{{ record.isActive ? '启用' : '停用' }}</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="语义层" width="170">
          <template #default="{ record }">
            <template v-if="record.scanStatus === 'done'">
              <a-tag color="success">v 已扫描</a-tag>
              <span class="muted">{{ record.scanStage }}</span>
            </template>
            <a-tag v-else-if="record.scanStatus === 'scanning'" color="processing">
              {{ record.scanProgress }}% {{ record.scanStage }}
            </a-tag>
            <a-tag v-else-if="record.scanStatus === 'failed'" color="error">失败</a-tag>
            <a-tag v-else>未扫描</a-tag>
          </template>
        </a-table-column>
        <a-table-column title="操作" width="300">
          <template #default="{ record }">
            <a-button size="small" type="link" @click="health(record)">健康</a-button>
            <a-button size="small" type="link" :disabled="record.scanStatus === 'scanning'"
                      @click="scan(record)">扫描语义层</a-button>
            <a-button size="small" type="link" @click="viewSemantic(record)"
                      :disabled="record.scanStatus !== 'done'">语义层</a-button>
            <a-popconfirm title="删除数据源及其语义层/向量数据?" @confirm="removeDs(record)">
              <a-button size="small" type="link" danger>删除</a-button>
            </a-popconfirm>
          </template>
        </a-table-column>
      </a-table>
      <div v-if="healthInfo" class="health-line">
        <a-tag :color="healthInfo.healthy ? 'success' : 'error'">
          {{ healthInfo.healthy ? `健康 ${healthInfo.latency_ms}ms` : '不可达' }}
        </a-tag>
        <span class="muted">{{ healthInfo.server_version || healthInfo.error }}</span>
      </div>
    </a-card>

    <!-- ══════════ 语义层查看 ══════════ -->
    <a-card v-if="semantic" :title="`语义层 v${semantic.version}`" class="section-card">
      <template #extra>
        <a-button size="small" @click="semantic = null">关闭</a-button>
      </template>
      <a-collapse>
        <a-collapse-panel v-for="m in semantic.content.models" :key="m.name"
                          :header="`${m.name}（${m.display_name}）— 列 ${m.columns.length} · 关系 ${m.relationships.length} · 指标 ${m.metrics.length}`">
          <div class="model-detail">
            <div class="detail-block">
              <div class="block-title">列</div>
              <a-tag v-for="c in m.columns" :key="c.name"
                     :color="c.semantic_type === 'measure' ? 'blue' : c.semantic_type === 'key' ? 'purple' : 'default'">
                {{ c.name }} · {{ c.display_name }} · {{ c.data_type }}
              </a-tag>
            </div>
            <div v-if="m.metrics.length" class="detail-block">
              <div class="block-title">指标</div>
              <div v-for="metric in m.metrics" :key="metric.name" class="metric-line">
                <b>{{ metric.display_name }}</b>
                <code>{{ metric.formula }}</code>
                <span v-if="metric.condition" class="muted">WHERE {{ metric.condition }}</span>
              </div>
            </div>
            <div v-if="m.relationships.length" class="detail-block">
              <div class="block-title">关系</div>
              <div v-for="rel in m.relationships" :key="rel.name" class="metric-line">
                <code>{{ rel.join_type }} JOIN {{ rel.target_model }} ON {{ rel.on }}</code>
                <span class="muted">({{ rel.type }} · 置信度 {{ rel.confidence }})</span>
              </div>
            </div>
          </div>
        </a-collapse-panel>
      </a-collapse>
      <div v-if="semantic.content.sample_questions?.length" class="detail-block">
        <div class="block-title">示例问题</div>
        <a-tag v-for="q in semantic.content.sample_questions" :key="q" color="cyan">{{ q }}</a-tag>
      </div>
    </a-card>

      </a-tab-pane>
      <a-tab-pane key="m4">
        <template #tab><AppstoreOutlined /> 保存查询与看板</template>
        <M4Page />
      </a-tab-pane>
    </a-tabs>

    <!-- ══════════ 新建数据源 ══════════ -->
    <a-modal v-model:open="showCreate" title="添加数据源" @ok="create">
      <a-form layout="vertical">
        <a-form-item label="名称" required><a-input v-model:value="form.name" /></a-form-item>
        <a-form-item label="类型" required>
          <a-select v-model:value="form.db_type">
            <a-select-option value="postgresql">PostgreSQL</a-select-option>
            <a-select-option value="mysql">MySQL</a-select-option>
          </a-select>
        </a-form-item>
        <a-form-item label="主机" required><a-input v-model:value="form.host" /></a-form-item>
        <a-form-item label="端口" required><a-input-number v-model:value="form.port" style="width:100%" /></a-form-item>
        <a-form-item label="数据库" required><a-input v-model:value="form.database" /></a-form-item>
        <a-form-item label="用户名" required><a-input v-model:value="form.username" /></a-form-item>
        <a-form-item label="密码" required><a-input-password v-model:value="form.password" /></a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// chatbi 管理页 —— 数据源管理/语义层查看(C-3 交付缺口收口)。
// 后端 15 路由已就绪(/api/packs/chatbi/*), 本页消费其核心子集;
// 图谱浏览/语义编辑器为增强项(数据在"查看详情"JSON 可见)。
import { onMounted, reactive, ref } from 'vue'
import { AppstoreOutlined, DatabaseOutlined, PlusOutlined } from '@ant-design/icons-vue'
import { message } from 'ant-design-vue'
import M4Page from './m4.vue'
import { chatbiApi } from '../../api'

const tab = ref('ds')

const datasources = ref<any[]>([])
const loadingDs = ref(false)
const showCreate = ref(false)
const healthInfo = ref<any>(null)
const semantic = ref<any>(null)
const scanning = ref<Set<string>>(new Set())

const form = reactive({ name: '', db_type: 'postgresql', host: '', port: 5432,
                        database: '', username: '', password: '' })

async function loadList() {
  loadingDs.value = true
  try {
    const { data } = await chatbiApi.get('/datasources')
    datasources.value = data.items || []
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  } finally {
    loadingDs.value = false
  }
}

async function create() {
  try {
    await chatbiApi.post('/datasources', form)
    message.success('已创建')
    showCreate.value = false
    await loadList()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '创建失败')
  }
}

async function health(record: any) {
  try {
    const { data } = await chatbiApi.post(`/datasources/${record.id}/health`)
    healthInfo.value = data
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '健康检查失败')
  }
}

async function scan(record: any) {
  try {
    await chatbiApi.post(`/datasources/${record.id}/scan`)
    message.info('扫描任务已提交')
    pollScan(record.id)
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '触发失败')
  }
}

async function pollScan(dsId: string) {
  // 扫描进度轮询(任务在后台跑;数据源行的 scanProgress 实时更新)
  const timer = setInterval(async () => {
    try {
      const { data: st } = await chatbiApi.get(`/datasources/${dsId}/scan`)
      const row = datasources.value.find((d) => d.id === dsId)
      if (row) { row.scanStatus = st.scanStatus; row.scanProgress = st.scanProgress
                 row.scanStage = st.scanStage }
      if (st.scanStatus === 'done' || st.scanStatus === 'failed') {
        clearInterval(timer)
        scanning.value.delete(dsId)
        if (st.scanStatus === 'done') message.success('扫描完成')
        else message.error(st.scanError || '扫描失败')
      }
    } catch {
      clearInterval(timer)
    }
  }, 3000)
}

async function viewSemantic(record: any) {
  try {
    const { data } = await chatbiApi.get(`/datasources/${record.id}/semantic-models`)
    semantic.value = data
  } catch {
    message.error('语义层未就绪')
  }
}

async function removeDs(record: any) {
  try {
    await chatbiApi.delete(`/datasources/${record.id}`)
    message.success('已删除(含语义层与向量数据)')
    await loadList()
  } catch (e: any) {
    message.error(e?.response?.data?.detail || '删除失败')
  }
}

onMounted(loadList)
</script>

<style scoped>
.chatbi-admin { display: flex; flex-direction: column; gap: 16px; }
.section-card { width: 100%; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
.health-line { margin-top: 8px; }
.model-detail { display: flex; flex-direction: column; gap: 10px; }
.detail-block .block-title { font-weight: 600; margin-bottom: 4px; }
.metric-line { margin-bottom: 4px; }
.metric-line code { margin: 0 8px; background: #f5f5f5; padding: 1px 6px; border-radius: 3px; }
</style>
