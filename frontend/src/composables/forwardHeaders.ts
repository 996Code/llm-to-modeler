// =============================================================================
// 模块说明：透传 headers 存储（模块级单例）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个用 @Component 实现的、持有静态字段的工具 Bean。
// 模块级变量（文件作用域的 let）在整个应用中只有一份，天然是单例。
//
// 职责：
//   在嵌入（iframe）模式下，存储父系统通过 postMessage 传入的 HTTP 请求头
//   （如 Authorization、X-Tenant-Id 等鉴权/租户信息），供所有 API 请求复用。
//
// 为什么单独拆成一个文件而不放进 useEmbedBridge.ts？
//   注释里说明：为了避免 rollup 打包时"Vue Composition API 导入"与"普通工具函数导出"
//   混在一起导致的构建问题。简言之——关注分离 + 规避打包器坑。
// =============================================================================

// 模块级私有变量，存储当前透传的 headers。
// 用 let 而非 const 是因为后续会被整体替换赋值。
// 类比 Java：相当于 private static Map<String,String> _forwardedHeaders
let _forwardedHeaders: Record<string, string> = {}
// Record<string, string> ≈ Map<String, String>，表示"键值都是字符串的对象"

/**
 * 获取当前透传 headers 的【副本】。
 * @returns 返回浅拷贝对象（{ ...obj } 展开运算符），避免外部直接修改内部状态。
 *          类比 Java：返回 Collections.unmodifiableMap(new HashMap<>(map))。
 */
export function getForwardedHeaders(): Record<string, string> {
  return { ..._forwardedHeaders }
}

/**
 * 设置（整体替换）透传 headers。
 * @param headers 新的 headers 字典；同样做浅拷贝后存储，切断与调用方的引用。
 */
export function setForwardedHeaders(headers: Record<string, string>): void {
  _forwardedHeaders = { ...headers }
}

/**
 * 清空透传 headers（登出 / 销毁时调用）。
 */
export function clearForwardedHeaders(): void {
  _forwardedHeaders = {}
}
