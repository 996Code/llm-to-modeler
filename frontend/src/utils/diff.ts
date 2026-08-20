// =============================================================================
// 模块说明：通用 diff —— 任意 JSON 制品的深比对（pack 无关）。
// -----------------------------------------------------------------------------
// 设计要点（对应 doc/嵌入模式总体设计.md §5.5）：
//   - 核心是纯 JSON 深比对，输出 add / remove / modify 三类变更，携带 JSON path；
//   - 数组对齐：按 identity 键（来自 pack manifest 声明，如 formFieldConfigVos
//     按 fieldTitleKey 对齐）建索引，无 identity 声明的数组退化为整体对比；
//   - 递归：childFormFieldConfigVo / labelPages / 任意嵌套对象自动深入；
//   - 通用性：本模块不认识任何领域词（form/field 等），身份键由调用方传入
//     （manifest 的 identity 映射），因此换一个 pack 无需改这里。
// =============================================================================

/** 单条变更 */
export interface DiffChange {
  kind: 'add' | 'remove' | 'modify'
  /** 变更位置的可读路径，如 "formFieldConfigVos[phone].isRequiredField" */
  path: string
  /** add: 新值；remove: 旧值；modify: {old, new} */
  oldValue?: unknown
  newValue?: unknown
  /** 所属对象的展示名（数组项对齐时取 displayKey 字段值，如字段中文名）；无则为 undefined */
  owner?: string
}

/** identity 映射：JSON path 片段 → 数组内对象的身份键名（来自 pack manifest） */
export type IdentityHints = Record<string, string>

/** 展示名映射：path → 用于 UI 展示的字段名（如 item_label: fieldTitleText） */
export type DisplayHints = Record<string, string>

/**
 * 计算 before → after 的差异。
 *
 * @param before  改前制品（宿主下发的基线）
 * @param after   改后制品（AI 生成的候选）
 * @param hints.identity 数组身份键声明；键匹配规则：
 *        规范化后匹配——把路径中所有数组下标段（[数字] 或 [身份键]）统一折算为
 *        `*`，因此 "formFieldConfigVos" 与 "formFieldConfigVos.*.childFormFieldConfigVo"
 *        都能命中任意深度的对齐数组。
 * @param displayKey 可选；数组项的展示名字段（如 fieldTitleText），
 *        命中时填充 change.owner 供摘要行显示中文名。
 */
export function diffJson(
  before: unknown,
  after: unknown,
  identity: IdentityHints = {},
  displayKey?: string,
): DiffChange[] {
  const changes: DiffChange[] = []
  walk(before, after, '', identity, changes, undefined, displayKey)
  return changes
}

/** 把实际 path 规范化为可匹配 hint 的形式：数组下标段（[数字]/[身份键]）→ `*` 段 */
function normalizePath(path: string): string {
  return path
    .split('.')
    .map((seg) => {
      // 段内含数组标签（如 "formFieldConfigVos[sub]"）→ 剥离标签并追加 * 段
      if (seg.includes('[')) {
        const stripped = seg.replace(/\[[^\]]*\]/g, '')
        return stripped ? `${stripped}.*` : '*'
      }
      return /^\d+$/.test(seg) ? '*' : seg
    })
    .filter((s) => s !== '')
    .join('.')
}

/** 按身份键对齐两个对象数组，返回配对结果（未配对的进 added/removed） */
function alignArrays(
  beforeArr: unknown[],
  afterArr: unknown[],
  identityKey: string | undefined,
): {
  pairs: Array<{ before: unknown; after: unknown; label: string }>
  added: Array<{ item: unknown; label: string }>
  removed: Array<{ item: unknown; label: string }>
} {
  // 无身份键声明：退化为按下标整体对比（调用方对输出做"整体替换"展示）
  if (!identityKey) {
    return {
      pairs: beforeArr.map((b, i) => ({ before: b, after: afterArr[i], label: `[${i}]` })),
      added: afterArr.slice(beforeArr.length).map((item, i) => ({
        item,
        label: `[${beforeArr.length + i}]`,
      })),
      removed: [],
    }
  }

  const keyOf = (item: unknown): string | undefined =>
    item && typeof item === 'object' && identityKey in (item as any)
      ? String((item as any)[identityKey])
      : undefined

  const afterMap = new Map<string, unknown>()
  for (const a of afterArr) {
    const k = keyOf(a)
    if (k !== undefined) afterMap.set(k, a)
  }
  const beforeKeys = new Set<string>()
  const pairs: Array<{ before: unknown; after: unknown; label: string }> = []
  const removed: Array<{ item: unknown; label: string }> = []
  for (const b of beforeArr) {
    const k = keyOf(b)
    if (k === undefined) continue
    beforeKeys.add(k)
    const a = afterMap.get(k)
    if (a !== undefined) pairs.push({ before: b, after: a, label: k })
    else removed.push({ item: b, label: k })
  }
  const added: Array<{ item: unknown; label: string }> = []
  for (const [k, a] of afterMap) {
    if (!beforeKeys.has(k)) added.push({ item: a, label: k })
  }
  return { pairs, added, removed }
}

function walk(
  before: unknown,
  after: unknown,
  path: string,
  identity: IdentityHints,
  out: DiffChange[],
  owner: string | undefined,
  displayKey?: string,
): void {
  // 从对象里取展示名（displayKey 声明时）
  const pickLabel = (v: unknown): string | undefined =>
    displayKey && v && typeof v === 'object' && displayKey in (v as any)
      ? String((v as any)[displayKey])
      : undefined

  // 类型不同：整体视为 modify
  if (typeof before !== typeof after || before === null || after === null) {
    if (before !== after) {
      out.push({ kind: 'modify', path: path || '$', oldValue: before, newValue: after, owner })
    }
    return
  }

  // 数组：按身份键对齐
  if (Array.isArray(before) && Array.isArray(after)) {
    const key = identity[normalizePath(path)] ?? identity[path]
    const { pairs, added, removed } = alignArrays(before, after, key)
    const pushPath = (label: string) => (path ? `${path}[${label}]` : `[${label}]`)
    for (const r of removed) {
      out.push({ kind: 'remove', path: pushPath(r.label), oldValue: r.item, owner: pickLabel(r.item) ?? owner })
    }
    for (const a of added) {
      out.push({ kind: 'add', path: pushPath(a.label), newValue: a.item, owner: pickLabel(a.item) ?? owner })
    }
    for (const p of pairs) {
      // 进入对齐的数组项：更新 owner（优先展示名，退化为身份键值）
      walk(p.before, p.after, pushPath(p.label), identity, out, pickLabel(p.before) ?? pickLabel(p.after) ?? p.label, displayKey)
    }
    return
  }

  // 对象：并集键逐个深入
  if (typeof before === 'object' && typeof after === 'object') {
    const keys = new Set([...Object.keys(before as object), ...Object.keys(after as object)])
    for (const k of keys) {
      const bv = (before as any)[k]
      const av = (after as any)[k]
      const childPath = path ? `${path}.${k}` : k
      if (!(k in (before as object))) {
        out.push({ kind: 'add', path: childPath, newValue: av, owner })
      } else if (!(k in (after as object))) {
        out.push({ kind: 'remove', path: childPath, oldValue: bv, owner })
      } else {
        walk(bv, av, childPath, identity, out, owner, displayKey)
      }
    }
    return
  }

  // 基本类型：直接比较
  if (before !== after) {
    out.push({ kind: 'modify', path: path || '$', oldValue: before, newValue: after, owner })
  }
}

/**
 * 把 diff 结果渲染为人类可读的摘要行（应用前确认弹窗用）。
 * 展示名优先级：change.owner（diff 时解析的所属对象中文名）>
 * 新增/删除项自身的 item_label 字段 > path 首段。
 */
export function summarizeChanges(
  changes: DiffChange[],
  before: unknown,
  display: DisplayHints = {},
): string[] {
  const itemLabel = display.item_label || 'name'
  const labelOf = (change: DiffChange): string => {
    // 1) diff 阶段已解析的所属对象展示名
    if (change.owner) return change.owner
    // 2) 新增/删除项本身是对象：取其展示字段
    const candidate = change.kind === 'add' ? change.newValue : change.oldValue
    if (candidate && typeof candidate === 'object' && itemLabel in (candidate as any)) {
      return String((candidate as any)[itemLabel])
    }
    // 3) 兜底：path 首段
    const seg = change.path.replace(/^\[|\]$/g, '').split(/[.[]/)[0]
    return seg || change.path
  }
  const propOf = (change: DiffChange): string => change.path.split('.').pop() || ''

  const lines: string[] = []
  const added = changes.filter((c) => c.kind === 'add')
  const removed = changes.filter((c) => c.kind === 'remove')
  const modified = changes.filter((c) => c.kind === 'modify')

  // 同一对象的多个 modify 属性合并成一行："备注（isRequiredField: 否→是, …）"
  const byLabel = new Map<string, string[]>()
  for (const c of modified) {
    const label = labelOf(c)
    const desc =
      c.oldValue !== undefined && c.newValue !== undefined
        ? `${propOf(c)}: ${formatVal(c.oldValue)} → ${formatVal(c.newValue)}`
        : `${propOf(c)} 变更`
    const list = byLabel.get(label) || []
    list.push(desc)
    byLabel.set(label, list)
  }

  for (const [label, props] of byLabel) {
    lines.push(`修改「${label}」：${props.slice(0, 3).join('；')}${props.length > 3 ? ' 等' : ''}`)
  }
  for (const c of added) {
    lines.push(`新增「${labelOf(c)}」`)
  }
  for (const c of removed) {
    lines.push(`删除「${labelOf(c)}」`)
  }
  return lines
}

function formatVal(v: unknown): string {
  if (v === null || v === undefined) return '空'
  if (typeof v === 'object') return '{…}'
  return String(v)
}
