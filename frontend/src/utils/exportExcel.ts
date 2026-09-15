/**
 * Excel 导出工具 — 数据 sheet + 图表 sheet (嵌 PNG)
 *
 * 移植自原版 chat-bi frontend/src/utils/exportExcel.ts(逐行对齐):
 *   - API-05: 查询结果导出 Excel
 *   - CHART-09: 图表导出
 *
 * 使用 exceljs 生成 .xlsx, 两个 sheet:
 *   - "数据" sheet: 查询结果表格(表头加粗+浅蓝底+冻结首行+自动列宽)
 *   - "图表" sheet: ECharts 图表 PNG 快照(可选)
 *
 * 安全: CSV/Excel 注入防护(= + - @ 开头加单引号前缀, OWASP 推荐)。
 */
import ExcelJS from 'exceljs'

export interface ExportData {
  /** 查询问题 (用作文件名) */
  question: string
  /** 列名 */
  columns: string[]
  /** 数据行 (与 columns 顺序对齐) */
  rows: Record<string, any>[] | any[][]
  /** ECharts 图表实例 (可选, 有则嵌 PNG) */
  chart?: any
}

export async function exportQueryToExcel(data: ExportData): Promise<void> {
  const { question, columns, rows, chart } = data
  const workbook = new ExcelJS.Workbook()
  workbook.creator = 'LLM Form Modeler'
  workbook.created = new Date()

  // ── Sheet 1: 数据 ── 冻结首行, 方便滚动查看大量数据
  const ws = workbook.addWorksheet('数据', {
    views: [{ state: 'frozen', ySplit: 1 }],
  })

  const headerRow = ws.addRow(columns)
  headerRow.eachCell((cell) => {
    cell.font = { bold: true }
    cell.fill = {
      type: 'pattern', pattern: 'solid',
      fgColor: { argb: 'FFE8F0FE' },
    }
  })

  // 数据行(CSV 注入防护)
  const isObjRows = rows.length > 0 && !Array.isArray(rows[0])
  for (const row of rows) {
    ws.addRow(columns.map((col) =>
      sanitizeCell(isObjRows ? (row as Record<string, any>)[col] : row)))
  }

  // 自动列宽(按内容长度估算, 中文按 2 字符宽), 限制 10-50
  ws.columns.forEach((col, i) => {
    let maxLen = String(columns[i] || '').length
    for (const row of rows) {
      const val = String(isObjRows ? (row as Record<string, any>)[columns[i]] ?? '' : (row as any[])[i] ?? '')
      const len = [...val].reduce((s, c) => s + (c.charCodeAt(0) > 127 ? 2 : 1), 0)
      if (len > maxLen) maxLen = len
    }
    col.width = Math.min(Math.max(maxLen + 2, 10), 50)
  })

  // ── Sheet 2: 图表 (嵌 PNG) ──
  if (chart?.getDataURL) {
    const pngBase64 = chart.getDataURL({
      type: 'png',
      pixelRatio: 2,           // 2x 高清
      backgroundColor: '#fff',
    })
    if (pngBase64) {
      const wsChart = workbook.addWorksheet('图表')
      const base64Data = pngBase64.split(',')[1] || ''
      const buffer = base64ToBuffer(base64Data)
      const imageId = workbook.addImage({ buffer, extension: 'png' })
      wsChart.addImage(imageId, 'A1:Q31')
    }
  }

  // ── 生成并下载 ──
  const arrayBuffer = await workbook.xlsx.writeBuffer()
  const blob = new Blob([arrayBuffer], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `${(question || '查询结果').slice(0, 20)}-${formatDate()}.xlsx`
  a.click()
  URL.revokeObjectURL(url)
}

/** CSV/Excel 注入防护: 首字符是 = + - @ 时加单引号前缀(OWASP 推荐) */
function sanitizeCell(value: any): string {
  if (value === null || value === undefined) return ''
  const s = String(value)
  if (s && ('=+-@'.includes(s[0]))) {
    return "'" + s
  }
  return s
}

function base64ToBuffer(base64: string): ArrayBuffer {
  const binaryStr = atob(base64)
  const len = binaryStr.length
  const bytes = new Uint8Array(len)
  for (let i = 0; i < len; i++) {
    bytes[i] = binaryStr.charCodeAt(i)
  }
  return bytes.buffer
}

function formatDate(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}_${pad(d.getHours())}${pad(d.getMinutes())}`
}
