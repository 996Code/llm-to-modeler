// =============================================================================
// pack 自定义管理页注册表 —— 目录约定自动扫描(与后端 pack 同构的插件化)。
// -----------------------------------------------------------------------------
// 约定:frontend/src/admin/packPages/<pack_name>/index.vue 存在 = 该插件有
// 管理页组件;manifest 的 admin.page 声明 <pack_name> → AdminApp 动态挂 Tab。
//
// 与后端 domains/<pack>/ 的对齐:
//   后端:domains/<pack>/pack.py 存在即插件(目录扫描 + importlib)
//   前端:packPages/<pack>/index.vue 存在即页面(import.meta.glob 自动注册)
// 新插件的管理页 = 建目录写 index.vue,零中心文件改动;
// manifest 声明即挂载、随插件启停联动。每个插件页自动独立异步 chunk。
//
// 取舍:单仓库单构建(类型检查/vue-tsc 全覆盖);运行时远程加载独立前端包
// (Module Federation 等)不在本期范围。
// =============================================================================
import type { Component } from 'vue'
import { defineAsyncComponent } from 'vue'

const modules = import.meta.glob('./*/index.vue')

export const packPageRegistry: Record<string, Component> = Object.fromEntries(
  Object.entries(modules).map(([path, loader]) => {
    const key = path.match(/^\.\/([^/]+)\/index\.vue$/)?.[1]
    if (!key) throw new Error(`packPages 路径不符合插件约定: ${path}`)
    return [key, defineAsyncComponent(loader as () => Promise<Component>)]
  }),
)

/** 该 key 是否注册了管理页组件(未注册 → 不渲染 Tab,AdminApp 优雅降级)。 */
export function hasPackPage(key: string): boolean {
  return !!packPageRegistry[key]
}
