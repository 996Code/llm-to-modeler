<template>
  <div class="jv">
    <!-- 头部整行点击展开/收起;复制按钮次要操作,阻止冒泡不触发折叠 -->
    <div class="jv-head" @click="collapsed = !collapsed">
      <span class="jv-caret" :class="{ open: !collapsed }">▸</span>
      <span class="jv-label">{{ label }}</span>
      <span class="jv-meta">{{ lineCount }} 行 · {{ prettyText.length }} 字符</span>
      <button class="jv-btn" @click.stop="copy" title="复制全文">{{ copied ? '已复制' : '复制' }}</button>
    </div>
    <!-- 滚动限制放在滚动容器自身:max-height + overflow:auto 才能真正滚动
         (此前 max-height 套在外层且 overflow:hidden,内容被裁剪无法滚) -->
    <div v-show="!collapsed" class="jv-scroll" :style="{ maxHeight: height + 'px' }">
      <table class="jv-table">
        <tbody>
          <tr v-for="(line, i) in lines" :key="i">
            <td class="jv-no">{{ i + 1 }}</td>
            <td class="jv-code" v-html="line"></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
// 轻量 JSON 查看器:头部点击展开/收起 + 语法高亮 + 行号 + 复制。
// 不引入 Monaco(CDN 加载在内网部署不可达,且项目未用过);高亮实现与
// 聊天端 JsonDiffView 的 highlightLine 同路线。
import { computed, ref, watch } from 'vue'
import { message } from 'ant-design-vue'

const props = withDefaults(defineProps<{
  label?: string
  data: unknown
  height?: number
  defaultCollapsed?: boolean
}>(), {
  label: 'JSON',
  height: 280,
  defaultCollapsed: false,
})

const collapsed = ref(props.defaultCollapsed)
const copied = ref(false)

const prettyText = computed(() => {
  if (props.data == null) return '(空)'
  if (typeof props.data === 'string') return props.data
  try {
    return JSON.stringify(props.data, null, 2)
  } catch {
    return String(props.data)
  }
})

function esc(s: string): string {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
}

function highlight(src: string): string {
  const escaped = esc(src)
  return escaped.replace(
    /("(?:\\u[0-9a-fA-F]{4}|\\[^u]|[^\\"])*"(?:\s*:)?)|\b(true|false)\b|\bnull\b|-?\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b/g,
    (m) => {
      let cls = 'num'
      if (m.startsWith('"')) cls = m.trimEnd().endsWith(':') ? 'key' : 'str'
      else if (m === 'true' || m === 'false') cls = 'bool'
      else if (m === 'null') cls = 'null'
      return `<span class="jv-${cls}">${m}</span>`
    },
  )
}

const lines = computed(() => prettyText.value.split('\n').map(highlight))
const lineCount = computed(() => lines.value.length)

watch(() => props.data, () => { collapsed.value = props.defaultCollapsed })

async function copy() {
  try {
    await navigator.clipboard.writeText(prettyText.value)
    copied.value = true
    setTimeout(() => (copied.value = false), 1500)
  } catch {
    message.error('复制失败')
  }
}
</script>

<style scoped>
.jv {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  overflow: hidden;
  background: #fbfbfd;
  margin-top: 6px;
}
.jv-head {
  display: flex; align-items: center; gap: 8px;
  padding: 6px 10px; cursor: pointer; user-select: none;
  background: #f3f4f6; border-bottom: 1px solid #e5e7eb;
  font-size: 12px;
}
.jv-head:hover { background: #eef1f6; }
.jv-caret { color: #9ca3af; transition: transform 0.15s; display: inline-block; }
.jv-caret.open { transform: rotate(90deg); }
.jv-label { font-weight: 600; color: #374151; }
.jv-meta { color: #9ca3af; flex: 1; }
.jv-btn {
  border: none; background: transparent; color: #2563eb; cursor: pointer;
  font-size: 12px; padding: 1px 6px; border-radius: 4px;
}
.jv-btn:hover { background: #dbeafe; }
.jv-scroll { overflow: auto; }
.jv-table { border-collapse: collapse; width: 100%; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 12px; }
.jv-no {
  text-align: right; color: #c4c8d0; padding: 0 10px; user-select: none;
  border-right: 1px solid #eee; white-space: nowrap; vertical-align: top; line-height: 1.65;
  position: sticky; left: 0; background: #fbfbfd;
}
.jv-code {
  white-space: pre-wrap; word-break: break-all; padding-left: 10px; line-height: 1.65;
  color: #1f2937; vertical-align: top;
}
:deep(.jv-key) { color: #0550ae; }
:deep(.jv-str) { color: #0a3069; }
:deep(.jv-num) { color: #0560a4; }
:deep(.jv-bool) { color: #cf222e; }
:deep(.jv-null) { color: #6e7781; font-style: italic; }
</style>
