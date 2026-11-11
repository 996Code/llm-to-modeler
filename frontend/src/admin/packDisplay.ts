// 插件显示名共享工具:key ↔ 中文名(manifest admin.title)统一映射。
// /api/admin/packs 的 items 自带 adminTitle;各页 fetch 后调 rememberPacks
// 注册,下拉/表格用 packLabel 渲染「中文名（key）」,未注册的 key 原样显示。
import { reactive } from 'vue'
import type { AdminPack } from './api'

// 响应式 Map:注册(rememberPacks)晚于表格首渲染时——线上网络时序下会话列表
// 可能先到——读它的组件能跟着重渲染补上中文名;非响应式时先渲染的单元格
// 会永远停在英文 key(线上实测坑,本地后端快一直没露)
const titles = reactive(new Map<string, string>())

/** 已知插件配色(与调用日志类型色系一致);未列出的插件给中性色 */
const PACK_COLORS: Record<string, string> = {
  njmind_form: 'blue',
  knowledge_graph: 'geekblue',
}

/** 插件徽章/标签配色:会话列表与链路总览共用 */
export function packColor(p: string): string {
  return PACK_COLORS[p] || 'default'
}

export function rememberPacks(items: AdminPack[]): void {
  for (const p of items || []) {
    if (p?.name && p.adminTitle) titles.set(p.name, p.adminTitle)
  }
}

/** 中文名(无则空串) */
export function packTitle(name?: string | null): string {
  return (name && titles.get(name)) || ''
}

/** 显示名:中文名（key);无中文名时退回 key 本身 */
export function packLabel(name?: string | null): string {
  if (!name) return ''
  const t = titles.get(name)
  return t && t !== name ? `${t}（${name}）` : name
}

/** 下拉 options:key 为 value,显示名为 label;可带额外项(如「其他」) */
export function packOptionsFrom(items: AdminPack[], extra: { label: string; value: string }[] = []) {
  rememberPacks(items)
  return [
    ...(items || []).map((p) => ({ label: packLabel(p.name), value: p.name })),
    ...extra,
  ]
}
