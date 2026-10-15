// 知识库层级树映射逻辑单测(契约对齐后端 GET /kbs/tree)。
import { describe, expect, it } from 'vitest'
import type { KgTreeNode } from '../../../api'
import {
  buildFolderOptions, collectFolderKeys, flattenKbs, mapTreeData, subtreeIds,
} from '../kbTree'

function node(p: Partial<KgTreeNode> & { id: string; name: string }): KgTreeNode {
  return {
    kind: 'kb', parentId: null, description: '',
    children: undefined, ...p,
  } as KgTreeNode
}

// 树形:目录A(子目录B(库乙), 库甲) + 顶层散库
const TREE: KgTreeNode[] = [
  node({
    id: 'fa', name: '目录A', kind: 'folder', children: [
      node({ id: 'fb', name: '子目录B', kind: 'folder', parentId: 'fa', children: [
        node({ id: 'kb2', name: '库乙', parentId: 'fb', docCount: 2, entityTotal: 5 }),
      ] }),
      node({ id: 'kb1', name: '库甲', parentId: 'fa', docCount: 1, entityTotal: 3 }),
    ],
  }),
  node({ id: 'kb0', name: '顶层散库', docCount: 0, entityTotal: 0 }),
]

describe('mapTreeData', () => {
  it('folder=可折叠父节点(不可选中),kb=叶子;raw 挂原始节点', () => {
    const data = mapTreeData(TREE)
    const fa = data[0]
    expect(fa.key).toBe('fa')
    expect(fa.selectable).toBe(false)
    expect(fa.isLeaf).toBe(false)
    expect(fa.raw.name).toBe('目录A')
    expect(fa.children).toHaveLength(2)
    const kb1 = fa.children![1]
    expect(kb1.key).toBe('kb1')
    expect(kb1.selectable).toBe(true)
    expect(kb1.isLeaf).toBe(true)
    expect(kb1.children).toBeUndefined()
    expect(data[1].raw.docCount).toBe(0)
  })
})

describe('flattenKbs', () => {
  it('只平铺 kb 节点(深层递归),folder 不进详情列表', () => {
    const kbs = flattenKbs(TREE)
    expect(kbs.map((k) => k.id)).toEqual(['kb2', 'kb1', 'kb0'])
  })
})

describe('buildFolderOptions', () => {
  it('目录带路径前缀(区分同名),深层递归', () => {
    const opts = buildFolderOptions(TREE)
    expect(opts).toEqual([
      { id: 'fa', path: '目录A' },
      { id: 'fb', path: '目录A / 子目录B' },
    ])
  })
})

describe('collectFolderKeys', () => {
  it('收集全部目录键(kb 叶子不需要展开)', () => {
    expect(collectFolderKeys(TREE)).toEqual(['fa', 'fb'])
  })
})

describe('subtreeIds', () => {
  it('自身+全部子孙(移动 folder 排除自身子树防环)', () => {
    const fa = TREE[0]
    expect(subtreeIds(fa)).toEqual(new Set(['fa', 'fb', 'kb2', 'kb1']))
    expect(subtreeIds(TREE[0].children![0])).toEqual(new Set(['fb', 'kb2']))
  })
})
