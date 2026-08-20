import { describe, it, expect } from 'vitest'
import { diffJson, summarizeChanges } from '../diff'

const identity = {
  formFieldConfigVos: 'fieldTitleKey',
  'formFieldConfigVos.*.childFormFieldConfigVo': 'fieldTitleKey',
}

const before = {
  formName: '请假表',
  formColumnsNumber: 4,
  formFieldConfigVos: [
    { fieldTitleKey: 'name', fieldTitleText: '姓名', isRequiredField: 0 },
    { fieldTitleKey: 'reason', fieldTitleText: '备注', isRequiredField: 0 },
    { fieldTitleKey: 'gone', fieldTitleText: '旧字段' },
  ],
}

const after = {
  formName: '请假表',
  formColumnsNumber: 4,
  formFieldConfigVos: [
    { fieldTitleKey: 'name', fieldTitleText: '姓名', isRequiredField: 1 },
    { fieldTitleKey: 'reason', fieldTitleText: '备注', isRequiredField: 0 },
    { fieldTitleKey: 'phone', fieldTitleText: '手机号' },
  ],
}

describe('diffJson（按身份键对齐数组）', () => {
  // displayKey 传入后 change.owner 携带所属对象中文名（摘要行用）
  const dk = 'fieldTitleText'

  it('识别 add / remove / modify 三类变更', () => {
    const changes = diffJson(before, after, identity, dk)
    const adds = changes.filter(c => c.kind === 'add')
    const removes = changes.filter(c => c.kind === 'remove')
    const modifies = changes.filter(c => c.kind === 'modify')
    expect(adds.map(c => c.path)).toContain('formFieldConfigVos[phone]')
    expect(removes.map(c => c.path)).toContain('formFieldConfigVos[gone]')
    expect(modifies.map(c => c.path)).toContain('formFieldConfigVos[name].isRequiredField')
  })

  it('change.owner 携带所属对象中文名（modify 行展示用）', () => {
    const changes = diffJson(before, after, identity, dk)
    const mod = changes.find(c => c.path === 'formFieldConfigVos[name].isRequiredField')
    expect(mod?.owner).toBe('姓名')
  })

  it('相同配置零变更', () => {
    expect(diffJson(before, JSON.parse(JSON.stringify(before)), identity, dk)).toHaveLength(0)
  })

  it('无 identity 声明时降级按下标对比（不崩溃）', () => {
    const changes = diffJson({ list: [1, 2] }, { list: [1, 3] }, {}, dk)
    expect(changes.some(c => c.kind === 'modify')).toBe(true)
  })

  it('递归子数组（childFormFieldConfigVo）按身份键对齐', () => {
    const b = {
      formFieldConfigVos: [
        { fieldTitleKey: 'sub', fieldTitleText: '子表', childFormFieldConfigVo: [
          { fieldTitleKey: 'a', fieldTitleText: 'A' },
        ] },
      ],
    }
    const a = {
      formFieldConfigVos: [
        { fieldTitleKey: 'sub', fieldTitleText: '子表', childFormFieldConfigVo: [
          { fieldTitleKey: 'a', fieldTitleText: 'A2' },
          { fieldTitleKey: 'b', fieldTitleText: 'B' },
        ] },
      ],
    }
    const changes = diffJson(b, a, identity, dk)
    expect(changes.some(c => c.kind === 'add' && c.path.includes('childFormFieldConfigVo[b]'))).toBe(true)
    expect(changes.some(c => c.kind === 'modify' && c.path.includes('childFormFieldConfigVo[a].fieldTitleText'))).toBe(true)
  })
})

describe('summarizeChanges（人类可读摘要）', () => {
  it('按对象聚合修改 + 新增/删除各一行', () => {
    const changes = diffJson(before, after, identity, 'fieldTitleText')
    const lines = summarizeChanges(changes, before, { item_label: 'fieldTitleText' })
    expect(lines.some(l => l.includes('姓名') && l.includes('isRequiredField'))).toBe(true)
    expect(lines.some(l => l.includes('新增') && l.includes('手机号'))).toBe(true)
    expect(lines.some(l => l.includes('删除') && l.includes('旧字段'))).toBe(true)
  })
})
