// 标准问答 触发问法(固定问答,合并问法框)管理契约(源码结构断言,真实行为由浏览器验收)
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

describe('标准问答 触发问法(固定问答,合并问法框)', () => {
  const source = readFileSync(new URL('../AnnotationManager.vue', import.meta.url), 'utf8')
  const apiSource = readFileSync(new URL('../../../api.ts', import.meta.url), 'utf8')

  it('问法合并为单框:第一行=标准问题,其余=触发问法,规则说明在标签旁帮助图标', () => {
    expect(source).toContain('v-model:value="editor.qaText"')
    expect(source).toContain('第一行是标准问题')
    expect(source).toContain('不经过模型与检索')
  })

  it('保存:问法框按行拆分,第一行=问题,其余=触发问法去重后提交', () => {
    expect(source).toMatch(/function splitTriggers/)
    expect(source).toMatch(/lines = e\.qaText\.split\('\\n'\)\.map\(t => t\.trim\(\)\)\.filter\(Boolean\)/)
    expect(source).toMatch(/const question = lines\[0\] \|\| ''/)
    expect(source).toContain(
      "saveKgAnnotation(props.kbId, e.id || null, question, answer.trim(), triggers)")
  })

  it('编辑回填:question+triggers 合成多行问法文本', () => {
    expect(source).toMatch(/qaText: \[r\.question, \.\.\.\(r\.triggers \|\| \[\]\)\]\.join\('\\n'\)/)
  })

  it('列表展示触发问法列:数量标签 + 悬停明细,未配置标注"仅语义"', () => {
    expect(source).toContain("{ title: '触发问法', key: 'triggers'")
    expect(source).toContain('固定问答 {{ record.triggers.length }} 问')
    expect(source).toContain('仅语义')
  })

  it('答案保持纯文本多行框(富文本/配图不在本仓库范围)', () => {
    expect(source).toContain('v-model:value="editor.answer"')
    expect(source).not.toContain('contenteditable')
    expect(source).not.toContain('domToMarkdown')
  })

  it('api 层:类型带 triggers,保存透传(缺省不传=后端沿用原值)', () => {
    expect(apiSource).toContain('triggers?: string[]')
    expect(apiSource).toMatch(
      /const body = triggers === undefined \? \{ question, answer \} : \{ question, answer, triggers \}/)
  })
})
