<!--
  =============================================================================
  组件职责：GitLab 风格 JSON 变更视图（红删绿增 + 双侧行号 + 增删统计）
  -----------------------------------------------------------------------------
  输入两个配置对象（基线 / 新版），内部做 2 缩进序列化 + LCS 行级 diff。
  有差异时默认变更视图、可切完整 JSON；无差异（或无基线）直接显示完整 JSON。
  供 JsonPanel（侧栏面板）与 ChatPanel 的 JSON 查看器弹窗共用。

  设计模式：纯展示组件（props 进、渲染出），不自持业务状态。
  Java 类比：一个只读的自定义 JSP tag / Thymeleaf fragment。
  =============================================================================
-->
<template>
  <div class="json-diff">
    <!-- 工具条：增删统计徽标 + 视图切换（有差异才有意义） -->
    <div v-if="hasDiff" class="diff-toolbar">
      <span class="diff-badge">
        <span class="badge-add">+{{ stats.added }} 行新增</span>
        <span class="badge-del">−{{ stats.removed }} 行删除</span>
      </span>
      <button class="toggle-btn" :class="{ active: mode === 'diff' }" @click="mode = 'diff'">变更</button>
      <button class="toggle-btn" :class="{ active: mode === 'full' }" @click="mode = 'full'">完整</button>
    </div>

    <!-- 变更视图：每行 = 旧行号 | 新行号 | +/- 前缀 | 行内容（行内语法高亮） -->
    <div v-if="hasDiff && mode === 'diff'" class="diff-view">
      <div v-for="(line, idx) in rows" :key="idx" class="dl" :class="line.cls">
        <span class="dl-no">{{ line.oldNo ?? '' }}</span>
        <span class="dl-no">{{ line.newNo ?? '' }}</span>
        <span class="dl-mark">{{ line.mark }}</span>
        <span class="dl-text" v-html="line.html"></span>
      </div>
    </div>
    <!-- 完整视图（无差异时的默认）：整份新版 JSON 高亮 -->
    <pre v-else class="json-view" v-html="highlightedFull"></pre>
  </div>
</template>

<script setup lang="ts">
// computed：派生响应式值；ref：可变状态（视图模式）；watch：差异出现时自动切回变更视图
import { computed, ref, watch } from 'vue'
import { diffLines, countChanges } from '../../utils/lineDiff'

const props = defineProps<{
  oldObj: Record<string, any> | null   // diff 基线（画布/上一版）
  newObj: Record<string, any> | null   // 新版配置
}>()

// 视图模式：diff=变更视图，full=完整 JSON。差异消失时自动回 full。
const mode = ref<'diff' | 'full'>('diff')

// 两份配置用同一序列化口径（2 空格缩进），行级 diff 才有意义
const oldText = computed(() => (props.oldObj ? JSON.stringify(props.oldObj, null, 2) : ''))
const newText = computed(() => (props.newObj ? JSON.stringify(props.newObj, null, 2) : ''))

const hasDiff = computed(() => !!props.oldObj && !!props.newObj && oldText.value !== newText.value)

// 新基线/新版本到来时重置为变更视图（用户上一轮手动切的 full 不跨配置保留）
watch([oldText, newText], () => {
  if (hasDiff.value) mode.value = 'diff'
})

const diffResult = computed(() => (hasDiff.value ? diffLines(oldText.value, newText.value) : []))
const stats = computed(() => countChanges(diffResult.value))

// 行渲染：类名/前缀/行号/行内高亮一次算好（v-for 直接渲染）
const rows = computed(() =>
  diffResult.value.map((l) => ({
    cls: l.type === 'add' ? 'dl-add' : l.type === 'del' ? 'dl-del' : 'dl-ctx',
    mark: l.type === 'add' ? '+' : l.type === 'del' ? '−' : ' ',
    oldNo: l.type === 'add' ? null : l.oldNo,
    newNo: l.type === 'del' ? null : l.newNo,
    html: highlightLine(l.text),
  })),
)

/** 完整视图：整份新版 JSON 语法高亮。 */
const highlightedFull = computed(() => highlightLine(newText.value))

/**
 * 单行 JSON 语法高亮：转义 < > & 后，用正则把 key/字符串/数字/布尔包进
 * 着色 span（与 JsonPanel 原实现同一套规则，抽到共用组件里）。
 * ⚠ v-html 渲染，内容已转义，可安全使用。
 */
function highlightLine(text: string): string {
  if (!text) return ''
  const escaped = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
  return escaped.replace(
    /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|(\b-?\d+\.?\d*\b)|(\btrue\b|\bfalse\b|\bnull\b)/g,
    (match, key, str, num, bool) => {
      if (key) return `<span class="j-key">${key.slice(0, -1).replace(/:$/, '')}</span>:`
      if (str) return `<span class="j-str">${str}</span>`
      if (num) return `<span class="j-num">${num}</span>`
      if (bool) return `<span class="j-bool">${bool}</span>`
      return match
    },
  )
}
</script>

<style scoped>
.json-diff { display: flex; flex-direction: column; height: 100%; min-height: 0; }

/* 工具条 */
.diff-toolbar {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; border-bottom: 1px solid var(--border-color-light);
  position: sticky; top: 0; background: var(--bg-container); z-index: 1;
}
.diff-badge { display: inline-flex; gap: 10px; margin-right: auto; font-size: 12px; font-family: var(--font-mono); }
.badge-add { color: #1a7f37; font-weight: 600; }
.badge-del { color: #cf222e; font-weight: 600; }
.toggle-btn {
  border: 1px solid var(--border-color-light); background: var(--bg-container);
  color: var(--text-regular); font-size: 12px; padding: 2px 10px;
  border-radius: var(--radius-md); cursor: pointer; transition: all 0.2s;
}
.toggle-btn.active { border-color: var(--color-primary); color: var(--color-primary); background: var(--color-primary-bg); }

/* 变更视图行（GitLab 配色：淡底 + 左侧彩条） */
.diff-view { font-family: var(--font-mono); font-size: 12px; line-height: 1.65; }
.dl { display: flex; align-items: stretch; }
.dl:hover { background: var(--bg-hover); }
.dl-no {
  flex: 0 0 42px; text-align: right; padding: 0 6px;
  color: var(--text-placeholder); user-select: none; font-size: 11px; line-height: 1.9;
}
.dl-mark { flex: 0 0 18px; text-align: center; user-select: none; font-weight: 700; line-height: 1.7; }
.dl-text { flex: 1; white-space: pre-wrap; word-break: break-all; padding-right: 12px; }
.dl-del { background: #ffebe9; border-left: 3px solid #cf222e; }
.dl-del .dl-mark { color: #cf222e; }
.dl-add { background: #e6ffec; border-left: 3px solid #1a7f37; }
.dl-add .dl-mark { color: #1a7f37; }
.dl-ctx { border-left: 3px solid transparent; }
/* 增删行内的 token 统一深红/深绿（淡彩底上保证对比度） */
.dl-del .dl-text :deep(.j-key),
.dl-del .dl-text :deep(.j-str),
.dl-del .dl-text :deep(.j-num),
.dl-del .dl-text :deep(.j-bool) { color: #a40e26; }
.dl-add .dl-text :deep(.j-key),
.dl-add .dl-text :deep(.j-str),
.dl-add .dl-text :deep(.j-num),
.dl-add .dl-text :deep(.j-bool) { color: #116329; }

/* 完整视图（与原 JsonPanel 一致的着色） */
.json-view {
  padding: 16px; font-family: var(--font-mono); font-size: 12.5px; line-height: 1.7;
  color: var(--text-regular); white-space: pre-wrap; word-break: break-all; margin: 0;
}
.json-view :deep(.j-key) { color: #1f6feb; }
.json-view :deep(.j-str) { color: #00a870; }
.json-view :deep(.j-num) { color: #d4a72c; }
.json-view :deep(.j-bool) { color: #f54a45; }
</style>
