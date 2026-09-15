<template>
  <div class="ds-manager">
    <a-card class="section-card">
      <template #title>
        <DatabaseOutlined /> 数据源
        <span class="muted">{{ datasources.length }} 个</span>
      </template>
      <template #extra>
        <a-button size="small" @click="checkAllHealth" :loading="bulkHealth">
          <HeartOutlined /> 全量健康检查
        </a-button>
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
        <a-table-column title="语义层" width="190">
          <template #default="{ record }">
            <template v-if="record.scanStatus === 'done'">
              <a-tag color="success">已扫描</a-tag>
              <span class="muted">{{ record.scanStage }}</span>
            </template>
            <a-progress v-else-if="record.scanStatus === 'scanning'" :percent="record.scanProgress"
                        size="small" status="active" />
            <div v-if="record.scanStatus === 'scanning'" class="scan-stage">{{ record.scanStage }}</div>
            <a-tooltip v-else-if="record.scanStatus === 'failed'" :title="record.scanError || '扫描失败'">
              <a-tag color="error">扫描失败</a-tag>
            </a-tooltip>
            <span v-else class="muted">未扫描</span>
          </template>
        </a-table-column>
        <a-table-column title="操作" width="360">
          <template #default="{ record }">
            <a-button size="small" type="link" :loading="healthCheckingId === record.id"
                      @click="health(record)">健康</a-button>
            <a-button size="small" type="link" :disabled="record.scanStatus === 'scanning'"
                      @click="scan(record)">{{ record.scanStatus === 'done' ? '重新扫描' : '扫描' }}</a-button>
            <a-button size="small" type="link" @click="viewSemantic(record)"
                      :disabled="record.scanStatus !== 'done'">语义层</a-button>
            <a-button size="small" type="link" @click="emit('open-graph', record.id)"
                      :disabled="record.scanStatus !== 'done'">图谱</a-button>            <a-popconfirm title="删除数据源及其语义层/向量数据?" ok-text="删除" ok-type="danger"
                          @confirm="removeDs(record)">
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

    <!-- ══ 新建数据源(双栏排版,不再拉长) ══ -->
    <a-modal v-model:open="showCreate" title="添加数据源" ok-text="创建" cancel-text="取消"
             :confirm-loading="creating" @ok="create">
      <a-form layout="vertical" class="ds-form">
        <div class="form-row">
          <a-form-item label="名称" required class="half">
            <a-input v-model:value="form.name" placeholder="如:电商业务库" />
          </a-form-item>
          <a-form-item label="类型" required class="half">
            <a-select v-model:value="form.db_type">
              <a-select-option value="postgresql">PostgreSQL</a-select-option>
              <a-select-option value="mysql">MySQL</a-select-option>
            </a-select>
          </a-form-item>
        </div>
        <div class="form-row">
          <a-form-item label="主机" required class="half">
            <a-input v-model:value="form.host" placeholder="localhost" />
          </a-form-item>
          <a-form-item label="端口" required class="half">
            <a-input-number v-model:value="form.port" style="width:100%" :min="1" :max="65535" />
          </a-form-item>
        </div>
        <div class="form-row">
          <a-form-item label="数据库" required class="half">
            <a-input v-model:value="form.database" />
          </a-form-item>
          <a-form-item label="用户名" required class="half">
            <a-input v-model:value="form.username" />
          </a-form-item>
        </div>
        <a-form-item label="密码" required>
          <a-input-password v-model:value="form.password" />
        </a-form-item>
      </a-form>
    </a-modal>
  </div>
</template>

<script setup lang="ts">
// 数据源管理子页:列表/新建/健康检查/扫描轮询/语义层抽屉。
// 从原 index.vue 拆出;「图谱」按钮 emit 给父级切到图谱 Tab(跨 Tab 联动)。
import { computed, defineEmits, defineExpose, onMounted, reactive, ref } from 'vue'
import { message } from 'ant-design-vue'
import {
  DatabaseOutlined, HeartOutlined, PlusOutlined,
} from '@ant-design/icons-vue'
import { chatbiApi } from '../../api'

const emit = defineEmits<{
  (e: 'open-graph', dsId: string): void
  (e: 'open-semantic', dsId: string): void
  (e: 'loaded', options: { id: string; name: string }[]): void
}>()

const datasources = ref<any[]>([])
const loadingDs = ref(false)
const showCreate = ref(false)
const creating = ref(false)
const healthInfo = ref<any>(null)
const healthCheckingId = ref('')
const bulkHealth = ref(false)

const form = reactive({ name: '', db_type: 'postgresql', host: '', port: 5432,
                        database: '', username: '', password: '' })

// 图谱 Tab 的数据源下拉选项(父级经 expose 取走)
const options = computed(() => datasources.value.map((d) => ({ id: d.id, name: d.name })))
defineExpose({ options, reload: loadList })

async function loadList() {
  loadingDs.value = true
  try {
    const { data } = await chatbiApi.get('/datasources')
    datasources.value = data.items || []
    emit('loaded', options.value)
  } catch (e: any) {
    message.error(errText(e, '加载失败'))
  } finally {
    loadingDs.value = false
  }
}

function errText(e: any, fallback: string): string {
  const d = e?.response?.data?.detail
  if (typeof d === 'string' && /[\u4e00-\u9fa5]/.test(d)) return d
  return d && typeof d === 'string' ? zhHttpError(e?.response?.status, d) : fallback
}

function zhHttpError(status: number | undefined, detail: string): string {
  if (status === 404) return '资源不存在或已删除'
  if (status === 401) return '登录态已失效,请重新登录'
  if (status === 403) return '没有权限执行此操作'
  if (status === 422) return '请求参数校验失败'
  if (status === 503) return '服务暂不可用'
  return detail
}

async function create() {
  if (!form.name.trim() || !form.host.trim() || !form.database.trim()) {
    message.warning('请填写名称、主机与数据库')
    return
  }
  creating.value = true
  try {
    await chatbiApi.post('/datasources', form)
    message.success('数据源已创建')
    showCreate.value = false
    Object.assign(form, { name: '', host: '', database: '', username: '', password: '' })
    await loadList()
  } catch (e: any) {
    message.error(errText(e, '创建失败'))
  } finally {
    creating.value = false
  }
}

async function health(record: any) {
  healthCheckingId.value = record.id
  try {
    const { data } = await chatbiApi.post(`/datasources/${record.id}/health`)
    healthInfo.value = data
  } catch (e: any) {
    message.error(errText(e, '健康检查失败'))
  } finally {
    healthCheckingId.value = ''
  }
}

async function checkAllHealth() {
  bulkHealth.value = true
  try {
    const { data } = await chatbiApi.post('/datasources/health-check/all')
    const s = data.summary || data
    message.success(`巡检完成:健康 ${s.healthy ?? 0} · 异常 ${s.unhealthy ?? 0} · 恢复 ${s.recovered ?? 0}`)
    await loadList()
  } catch (e: any) {
    message.error(errText(e, '巡检失败'))
  } finally {
    bulkHealth.value = false
  }
}

async function scan(record: any) {
  try {
    await chatbiApi.post(`/datasources/${record.id}/scan`)
    message.info('扫描任务已提交')
    pollScan(record.id)
  } catch (e: any) {
    message.error(errText(e, '触发失败'))
  }
}

async function pollScan(dsId: string) {
  const timer = setInterval(async () => {
    try {
      const { data: st } = await chatbiApi.get(`/datasources/${dsId}/scan`)
      const row = datasources.value.find((d) => d.id === dsId)
      if (row) { row.scanStatus = st.scanStatus; row.scanProgress = st.scanProgress
                 row.scanStage = st.scanStage; row.scanError = st.scanError }
      if (st.scanStatus === 'done' || st.scanStatus === 'failed') {
        clearInterval(timer)
        if (st.scanStatus === 'done') message.success('扫描完成')
        else message.error(st.scanError || '扫描失败')
      }
    } catch {
      clearInterval(timer)
    }
  }, 3000)
}

function viewSemantic(record: any) {
  // 语义层已是独立平级 Tab(双栏编辑视图), 这里只做跨 Tab 联动
  emit('open-semantic', record.id)
}

async function removeDs(record: any) {
  try {
    await chatbiApi.delete(`/datasources/${record.id}`)
    message.success('已删除(含语义层与向量数据)')
    await loadList()
  } catch (e: any) {
    message.error(errText(e, '删除失败'))
  }
}

onMounted(loadList)
</script>

<style scoped>
.ds-manager { display: flex; flex-direction: column; }
.section-card { width: 100%; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
.health-line { margin-top: 8px; }
.scan-stage { color: #999; font-size: 12px; }
/* 新建弹窗双栏:两个字段一行,压缩纵向长度 */
.ds-form .form-row { display: flex; gap: 12px; }
.ds-form .half { flex: 1; }
</style>
