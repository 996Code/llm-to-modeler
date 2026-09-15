<template>
  <div class="skills-page">
    <div class="tab-toolbar">
      <span>共 <b>{{ skills.length }}</b> 条</span>
      <span class="muted">规则文件随插件分发, 每次查询按方言注入 SQL 生成 prompt</span>
    </div>

    <a-empty v-if="!loading && !skills.length"
               description="暂无业务规则——规则文件位于插件 skills/ 目录(SKILL.md 格式)" />

      <a-collapse v-else v-model:activeKey="openKeys">
        <a-collapse-panel v-for="s in skills" :key="s.name">
          <template #header>
            <span class="skill-name">{{ s.name }}</span>
            <a-tag v-if="s.version" class="skill-ver">v{{ s.version }}</a-tag>
            <span class="skill-desc">{{ s.description }}</span>
          </template>
          <pre class="skill-content">{{ s.content }}</pre>
          <!-- 方言 reference(postgresql/mysql 各自的细则) -->
          <a-tabs v-if="Object.keys(s.references || {}).length" size="small">
            <a-tab-pane v-for="(text, dbType) in s.references" :key="dbType" :tab="dbType">
              <pre class="skill-content">{{ text }}</pre>
            </a-tab-pane>
          </a-tabs>
        </a-collapse-panel>
      </a-collapse>
  </div>
</template>

<script setup lang="ts">
// Skills 业务规则子页(对标原版 SkillsView 的只读子集):规则清单 + 正文 +
// 方言 reference。规则文件随 pack 分发(skills/<name>/SKILL.md), 热更新
// (mtime 缓存失效), 管理端只读——编辑走部署流程, 与原版"文件即真相"一致。
import { onMounted, ref } from 'vue'
import { message } from 'ant-design-vue'
import { chatbiApi } from '../../api'

const skills = ref<any[]>([])
const loading = ref(false)
const openKeys = ref<string[]>([])

async function loadSkills() {
  loading.value = true
  try {
    const { data } = await chatbiApi.get('/skills')
    skills.value = data.items || []
    if (skills.value.length) openKeys.value = [skills.value[0].name]
  } catch (e: any) {
    message.error(e?.response?.data?.detail || e.message || '加载失败')
  } finally { loading.value = false }
}

onMounted(loadSkills)
</script>

<style scoped>
.skills-page { display: flex; flex-direction: column; }
.tab-toolbar {
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 12px; font-size: 13px; color: #86909c;
}
.tab-toolbar b { color: #1d2129; }
.tab-toolbar .muted { margin-left: 0; }
.muted { color: #999; font-size: 12px; margin-left: 6px; }
.skill-name { font-weight: 600; margin-right: 8px; }
.skill-ver { font-size: 11px; }
.skill-desc { color: #86909c; font-size: 12px; margin-left: 8px; }
.skill-content {
  margin: 0; padding: 12px; background: #f7f8fa; border-radius: 8px;
  font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px;
  line-height: 1.7; color: #4e5969; white-space: pre-wrap; word-break: break-all;
  max-height: 420px; overflow: auto;
}
</style>
