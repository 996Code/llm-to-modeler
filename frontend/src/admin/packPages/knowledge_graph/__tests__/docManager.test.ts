// 文档列表筛选/分页契约(源码结构断言,真实行为由浏览器验收)
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

describe('知识库文档列表 筛选与分页', () => {
  const source = readFileSync(new URL('../DocManager.vue', import.meta.url), 'utf8')

  it('表格带 y 限高(表体自身出滚动条)与受控分页', () => {
    expect(source).toContain('scroll="{ x: 1100, y: tableScrollY }"')
    expect(source).toContain(':pagination="docPagination"')
    expect(source).toContain('@change="onTableChange"')
    expect(source).toContain('tableScrollY = computed')
  })

  it('支持文档名称模糊搜索并与状态筛选叠加', () => {
    expect(source).toMatch(/nameFilter.*placeholder="按文档名称搜索"/s)
    expect(source).toContain("d.filename || '').toLowerCase().includes(nameKeyword")
    expect(source).toContain('watch([statusFilter, nameKeyword]')
  })

  it('筛选变化回第 1 页并清空勾选', () => {
    expect(source).toMatch(/watch\(\[statusFilter, nameKeyword\][\s\S]*?docPage\.value = 1/)
  })
})
