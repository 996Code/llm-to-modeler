// =============================================================================
// 模块说明：行级 diff（LCS 最长公共子序列）—— GitLab 风格变更视图的核心算法
// -----------------------------------------------------------------------------
// 用途：把「基线配置 vs AI 最新配置」各自 JSON.stringify(,2) 后按行 diff，
// 产出 add/del/ctx 行流，供 JsonPanel 渲染红删绿增的变更视图。
//
// 类比 Java：经典的 Edit Distance / LCS DP，dp[i][j] 表示 a[i:] 与 b[j:]
// 的最长公共子序列长度；回溯路径即为最小编辑脚本（删除在前、新增在后）。
//
// 性能护栏：JSON 行数 n×m 超过 400 万（约 2000×2000）时退化为
// 「全删 + 全增」—— O(n·m) 内存此时已不可接受，且超大 diff 本身不可读。
// =============================================================================

/** 单行 diff 结果。type: del=基线有新版无（红），add=新增（绿），ctx=未变。 */
export interface DiffLine {
  type: 'add' | 'del' | 'ctx'
  oldNo: number | null   // 基线侧行号（add 行为 null）
  newNo: number | null   // 新版侧行号（del 行为 null）
  text: string
}

/**
 * 行级 diff：产出最小编辑脚本（LCS 回溯）。
 * 修改语义由相邻的 del 块 + add 块自然表达（同 GitLab unified diff）。
 */
export function diffLines(oldText: string, newText: string): DiffLine[] {
  const a = oldText.split('\n')
  const b = newText.split('\n')
  const n = a.length
  const m = b.length

  // 护栏：超大输入退化为整块替换，避免 O(n·m) DP 撑爆内存
  if (n * m > 4_000_000) {
    return [
      ...a.map((t, i) => ({ type: 'del', oldNo: i + 1, newNo: null, text: t }) as DiffLine),
      ...b.map((t, i) => ({ type: 'add', oldNo: null, newNo: i + 1, text: t }) as DiffLine),
    ]
  }

  // dp[i][j]：a[i:] 与 b[j:] 的 LCS 长度（逆序填表，倒推用）
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  // 回溯：相等走 ctx；否则先吐 del（保证 del 块在 add 块前，符合 unified diff 惯例）
  const out: DiffLine[] = []
  let i = 0
  let j = 0
  let oldNo = 0
  let newNo = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      oldNo++
      newNo++
      out.push({ type: 'ctx', oldNo, newNo, text: a[i] })
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      oldNo++
      out.push({ type: 'del', oldNo, newNo: null, text: a[i] })
      i++
    } else {
      newNo++
      out.push({ type: 'add', oldNo: null, newNo, text: b[j] })
      j++
    }
  }
  while (i < n) {
    oldNo++
    out.push({ type: 'del', oldNo, newNo: null, text: a[i] })
    i++
  }
  while (j < m) {
    newNo++
    out.push({ type: 'add', oldNo: null, newNo, text: b[j] })
    j++
  }
  return out
}

/** 统计增删行数（面板顶部 "+N −M" 徽标用）。 */
export function countChanges(lines: DiffLine[]): { added: number; removed: number } {
  let added = 0
  let removed = 0
  for (const l of lines) {
    if (l.type === 'add') added++
    else if (l.type === 'del') removed++
  }
  return { added, removed }
}
