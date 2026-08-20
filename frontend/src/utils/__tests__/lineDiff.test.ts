// 行级 diff（LCS）单测：增/删/改/移动不变性/护栏退化/统计。
import { describe, it, expect } from 'vitest'
import { diffLines, countChanges } from '../lineDiff'

describe('diffLines', () => {
  it('完全相同 → 全 ctx，行号双侧对齐', () => {
    const out = diffLines('a\nb\nc', 'a\nb\nc')
    expect(out.every((l) => l.type === 'ctx')).toBe(true)
    expect(out.map((l) => [l.oldNo, l.newNo])).toEqual([[1, 1], [2, 2], [3, 3]])
  })

  it('纯新增行 → add 且 newNo 连续', () => {
    const out = diffLines('a\nc', 'a\nb\nc')
    const adds = out.filter((l) => l.type === 'add')
    expect(adds.map((l) => l.text)).toEqual(['b'])
    expect(adds[0].newNo).toBe(2)
    expect(out.filter((l) => l.type === 'del')).toHaveLength(0)
  })

  it('纯删除行 → del 且 oldNo 保留', () => {
    const out = diffLines('a\nb\nc', 'a\nc')
    const dels = out.filter((l) => l.type === 'del')
    expect(dels.map((l) => l.text)).toEqual(['b'])
    expect(dels[0].oldNo).toBe(2)
  })

  it('修改行 → del 块在前 add 块在后（unified diff 惯例）', () => {
    const out = diffLines('a\nold\nc', 'a\nnew\nc')
    const types = out.map((l) => l.type)
    const delIdx = types.indexOf('del')
    const addIdx = types.indexOf('add')
    expect(delIdx).toBeGreaterThan(-1)
    expect(addIdx).toBe(delIdx + 1)
    expect(out.filter((l) => l.type === 'ctx').map((l) => l.text)).toEqual(['a', 'c'])
  })

  it('未变行不受别处修改影响（LCS 最小编辑）', () => {
    const old = JSON.stringify({ a: 1, b: [1, 2, 3], c: 'x' }, null, 2)
    const neu = JSON.stringify({ a: 1, b: [1, 2, 3, 4], c: 'x' }, null, 2)
    const out = diffLines(old, neu)
    // JSON 数组加一项只影响数组附近行，'a': 1 与 'c' 行应保持 ctx
    const ctxTexts = out.filter((l) => l.type === 'ctx').map((l) => l.text.trim())
    expect(ctxTexts).toContain('"a": 1,')
    expect(ctxTexts).toContain('"c": "x"')
  })

  it('空基线 → 内容行全部为 add（split 语义含 1 个空行 del）', () => {
    const out = diffLines('', 'a\nb')
    // ''.split('\n') === ['']：基线有 1 个空行，其余全为新增
    expect(out.filter((l) => l.type === 'add').map((l) => l.text)).toEqual(['a', 'b'])
    expect(out.filter((l) => l.type === 'del').map((l) => l.text)).toEqual([''])
  })

  it('超大输入护栏：退化为整块替换不抛错', () => {
    const big1 = Array.from({ length: 2500 }, (_, i) => `old-${i}`).join('\n')
    const big2 = Array.from({ length: 2500 }, (_, i) => `new-${i}`).join('\n')
    const out = diffLines(big1, big2)
    expect(out).toHaveLength(5000)
    expect(out.filter((l) => l.type === 'del')).toHaveLength(2500)
    expect(out.filter((l) => l.type === 'add')).toHaveLength(2500)
  })
})

describe('countChanges', () => {
  it('统计增删行数（b→x、c→y 是两处修改）', () => {
    const lines = diffLines('a\nb\nc', 'a\nx\ny')
    expect(countChanges(lines)).toEqual({ added: 2, removed: 2 })
  })
})
