import { describe, expect, it } from 'vitest'
import { classifyError, errorCategoryLabel } from '../errorDiag'

describe('classifyError', () => {
  it('LLM 上游类:502/timeout/connection/embed 归 llm-upstream', () => {
    expect(classifyError('Error code: 502')).toBe('llm-upstream')
    expect(classifyError('LLM chat failed: timeout')).toBe('llm-upstream')
    expect(classifyError('Connection refused')).toBe('llm-upstream')
    expect(classifyError('embed down')).toBe('llm-upstream')
  })

  it('检索类:neo4j/vector/graph 归 retrieval', () => {
    expect(classifyError('neo4j 服务不可用')).toBe('retrieval')
    expect(classifyError('vector search failed')).toBe('retrieval')
  })

  it('执行类:sql/数据库 归 execution', () => {
    expect(classifyError('SQL 校验失败')).toBe('execution')
    expect(classifyError('数据库连接超时')).toBe('execution')
  })

  it('未知错误归 unknown;空串安全', () => {
    expect(classifyError('什么都不是')).toBe('unknown')
    expect(classifyError('')).toBe('unknown')
  })

  it('大小写不敏感', () => {
    expect(classifyError('NEO4J DOWN')).toBe('retrieval')
    expect(classifyError('Sql Error')).toBe('execution')
  })
})

describe('errorCategoryLabel', () => {
  it('四类文案非空且不抛', () => {
    for (const cat of ['llm-upstream', 'retrieval', 'execution', 'unknown', undefined]) {
      expect(errorCategoryLabel(cat).length).toBeGreaterThan(0)
    }
  })
})
