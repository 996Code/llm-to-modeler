// 知识库层级树的纯映射逻辑(自 index.vue 抽出,便于单测)。
// 数据契约(后端 GET /kbs/tree):顶层列表,folder 节点带 children 递归;
// 排序 folder 先于 kb、同级按名称;kb 节点带 docCount/entityTotal/relationTotal。
import type { TreeDataItem } from 'ant-design-vue/es/tree'
import type { KgKnowledgeBase, KgTreeNode } from '../../api'

export interface KbTreeDataItem extends TreeDataItem {
  /** 挂原始节点(title 插槽渲染用) */
  raw: KgTreeNode
}

/** a-tree 数据:folder=可折叠父节点(不可选中详情),kb=叶子 */
export function mapTreeData(nodes: KgTreeNode[]): KbTreeDataItem[] {
  return nodes.map((n) => ({
    key: n.id,
    title: n.name,
    selectable: n.kind === 'kb',
    isLeaf: n.kind !== 'folder',
    children: n.kind === 'folder' ? mapTreeData(n.children || []) : undefined,
    raw: n,
  }))
}

/** kb-only 平铺(右侧详情数据源;folder 不进详情) */
export function flattenKbs(nodes: KgTreeNode[]): KgKnowledgeBase[] {
  const out: KgKnowledgeBase[] = []
  const walk = (ns: KgTreeNode[]) => {
    for (const n of ns) {
      if (n.kind === 'kb') out.push(n)
      if (n.children) walk(n.children)
    }
  }
  walk(nodes)
  return out
}

export interface FolderOption { id: string; path: string }

/** 目录选项(建库父目录/移动目标):带路径前缀便于区分同名 */
export function buildFolderOptions(nodes: KgTreeNode[]): FolderOption[] {
  const out: FolderOption[] = []
  const walk = (ns: KgTreeNode[], prefix: string) => {
    for (const n of ns) {
      if (n.kind !== 'folder') continue
      const path = prefix ? `${prefix} / ${n.name}` : n.name
      out.push({ id: n.id, path })
      walk(n.children || [], path)
    }
  }
  walk(nodes, '')
  return out
}

/** 全部目录键(默认展开全部用;kb 叶子无需展开) */
export function collectFolderKeys(nodes: KgTreeNode[]): string[] {
  const out: string[] = []
  const walk = (ns: KgTreeNode[]) => {
    for (const n of ns) {
      if (n.kind === 'folder') {
        out.push(n.id)
        walk(n.children || [])
      }
    }
  }
  walk(nodes)
  return out
}

/** 节点自身+全部子孙 id(folder 移动时排除自身子树,防环) */
export function subtreeIds(node: KgTreeNode): Set<string> {
  const ids = new Set<string>([node.id])
  for (const c of node.children || []) {
    for (const id of subtreeIds(c)) ids.add(id)
  }
  return ids
}
