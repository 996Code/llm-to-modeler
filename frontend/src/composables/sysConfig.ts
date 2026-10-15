// =============================================================================
// 模块说明：系统配置（聊天界面显示开关，模块级单例 + localStorage 持久化）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个 @Component 的配置 Bean，字段带默认值，变更即落库
// （localStorage 充当持久层），应用内所有组件拿到的是同一份响应式状态。
//
// 配置项（均为用户个人偏好，纯前端行为，不涉及后端）：
//   - showPipeline  对话时是否显示链路/路径/思考信息（pipeline 进度卡、
//                   「本轮链路」按钮）。关闭后流式期间只显示简单的
//                   「正在处理…」文本，结果照常展示。
//   - showRetrieval 首页对话界面是否显示检索结果（KG 检索结果卡）。
//                   关闭后答案文本照常，检索结果卡不渲染。
//
// 入口：聊天页右上角服务状态徽标 hover 弹出的系统配置面板
// （StandaloneLayout / EmbeddedLayout 共用，两布局同源同状态）。
// =============================================================================

import { reactive, watch } from 'vue'

/** 系统配置形状：两个显示开关 */
export interface SysConfig {
  /** 对话时显示链路/路径/思考信息（默认开） */
  showPipeline: boolean
  /** 对话界面显示检索结果（默认开） */
  showRetrieval: boolean
}

/** localStorage 持久化键（全局命名空间前缀防撞） */
const STORAGE_KEY = 'sys_config'

/** 默认值：两个开关都开（需求原文「默认开启 / 默认显示」） */
const DEFAULTS: SysConfig = { showPipeline: true, showRetrieval: true }

/**
 * 安全读 localStorage：非浏览器环境（vitest node 环境）/ JSON 损坏 /
 * 字段类型不对时一律回退默认值——配置读取永远不抛异常（显示开关挂了
 * 不该连累整个聊天页渲染）。
 */
function loadConfig(): SysConfig {
  try {
    const raw = storage()?.getItem(STORAGE_KEY)
    if (!raw) return { ...DEFAULTS }
    const parsed = JSON.parse(raw) as Partial<Record<keyof SysConfig, unknown>>
    return {
      showPipeline:
        typeof parsed.showPipeline === 'boolean' ? parsed.showPipeline : DEFAULTS.showPipeline,
      showRetrieval:
        typeof parsed.showRetrieval === 'boolean' ? parsed.showRetrieval : DEFAULTS.showRetrieval,
    }
  } catch {
    return { ...DEFAULTS }
  }
}

/** localStorage 可能不存在（node 测试环境 / 隐私模式极端场景），统一走此探测 */
function storage(): Pick<Storage, 'getItem' | 'setItem'> | null {
  try {
    if (typeof localStorage === 'undefined') return null
    return localStorage
  } catch {
    return null
  }
}

// 模块级单例：整个应用（含嵌入 iframe 与独立页各自一份）只有这一个实例。
// 类比 Java：private static final SysConfig INSTANCE = load();
const sysConfig = reactive<SysConfig>(loadConfig())

// 持久化：任何字段变更即整体写回（配置只有两个布尔，整体序列化无性能顾虑）。
// 写失败（隐私模式/存储满）静默——本次会话内开关仍然生效，只是不跨会话。
watch(
  sysConfig,
  () => {
    try {
      storage()?.setItem(STORAGE_KEY, JSON.stringify({ ...sysConfig }))
    } catch {
      /* 存储不可写：内存态生效即可 */
    }
  },
  { deep: true },
)

/**
 * 获取系统配置（响应式单例）。
 * 用法：`const sysConfig = useSysConfig()`，模板里 `sysConfig.showPipeline`。
 * 变更直接赋值（`sysConfig.showPipeline = false`），watcher 自动持久化。
 */
export function useSysConfig(): SysConfig {
  return sysConfig
}

/** 恢复默认值（两个开关全开）——预留的重置入口，同样会被持久化 */
export function resetSysConfig(): void {
  sysConfig.showPipeline = DEFAULTS.showPipeline
  sysConfig.showRetrieval = DEFAULTS.showRetrieval
}
