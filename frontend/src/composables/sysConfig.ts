// =============================================================================
// 模块说明：系统配置（聊天界面显示开关，模块级只读单例）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个 @Component 的配置 Bean，字段带默认值；与上一版
// 的区别是数据源在服务端——本 composable 只做「读」，不做「写」。
//
// 数据源（高 → 低）：
//   1. 后端平台级显示设置 GET /api/settings/display（用户级端点，管理端
//      PUT /api/admin/settings/display 落库；配置入口只在管理界面，
//      对话界面不支持配置——用户纠正后的需求）
//   2. localStorage 缓存（仅缓存：服务端读取成功后回写，跨会话兜底）
//   3. 默认值（两个开关全开）
//
// 配置项：
//   - showPipeline  对话时是否显示链路/路径/思考信息（pipeline 进度卡、
//                   「本轮链路」按钮）。关闭后流式期间只显示简单的
//                   「正在处理…」文本，结果照常展示。
//   - showRetrieval 首页对话界面是否显示检索结果（KG 检索结果卡）。
//                   关闭后答案文本照常，检索结果卡不渲染。
//
// 消费方：StandaloneLayout / EmbeddedLayout 下的 ChatPanel（正常对话 +
// 嵌入式对话都受这份配置影响）；挂载时调 refresh() 从服务端拉最新值。
// =============================================================================

import { reactive } from 'vue'
import { fetchDisplaySettings } from '../services/api'

/** 系统配置形状：两个显示开关 */
export interface SysConfig {
  /** 对话时显示链路/路径/思考信息（默认开） */
  showPipeline: boolean
  /** 对话界面显示检索结果（默认开） */
  showRetrieval: boolean
}

/** localStorage 缓存键（全局命名空间前缀防撞；仅缓存，不是配置入口） */
const STORAGE_KEY = 'sys_config'

/** 默认值：两个开关都开（需求原文「默认开启 / 默认显示」） */
const DEFAULTS: SysConfig = { showPipeline: true, showRetrieval: true }

/**
 * 安全读 localStorage 缓存：非浏览器环境（vitest node 环境）/ JSON 损坏 /
 * 字段类型不对时一律回退默认值——配置读取永远不抛异常（显示开关挂了
 * 不该连累整个聊天页渲染）。
 */
function loadCached(): SysConfig {
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

/** 服务端值同样逐字段校验：后端契约是两个布尔，异常响应不污染内存态 */
function sanitize(remote: unknown, fallback: SysConfig): SysConfig {
  const r = (remote || {}) as Partial<Record<keyof SysConfig, unknown>>
  return {
    showPipeline: typeof r.showPipeline === 'boolean' ? r.showPipeline : fallback.showPipeline,
    showRetrieval: typeof r.showRetrieval === 'boolean' ? r.showRetrieval : fallback.showRetrieval,
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

/** 回写缓存（服务端读取成功后调用；写失败静默——缓存只是兜底） */
function cacheToStorage(cfg: SysConfig): void {
  try {
    storage()?.setItem(STORAGE_KEY, JSON.stringify(cfg))
  } catch {
    /* 存储不可写：内存态生效即可 */
  }
}

// 模块级单例：整个应用（含嵌入 iframe 与独立页各自一份）只有这一个实例。
// 初始值 = localStorage 缓存（再兜底默认）；挂载时 refresh() 用服务端值覆盖。
// 类比 Java：private static final SysConfig INSTANCE = loadCached();
const sysConfig = reactive<SysConfig>(loadCached())

// 并发去重：多个组件同时挂载（独立页 ChatPanel + 嵌入宿主）只发一次请求
let refreshing: Promise<void> | null = null

/**
 * 从服务端刷新配置（聊天端唯一的数据入口，只读）。
 *
 * 成功 → 服务端值覆盖内存态并回写 localStorage 缓存；
 * 失败 → 保持当前值（初始 = 缓存，再兜底默认），不抛异常——配置拉取
 * 失败不该连累聊天页渲染。
 */
export function refresh(): Promise<void> {
  if (refreshing) return refreshing
  refreshing = (async () => {
    try {
      const remote = await fetchDisplaySettings()
      const next = sanitize(remote, { ...sysConfig })
      sysConfig.showPipeline = next.showPipeline
      sysConfig.showRetrieval = next.showRetrieval
      cacheToStorage({ ...sysConfig })
    } catch {
      /* 服务端不可达：保持缓存/默认值，本次会话内开关照常生效 */
    } finally {
      refreshing = null
    }
  })()
  return refreshing
}

/**
 * 获取系统配置（响应式只读单例）。
 * 用法：`const sysConfig = useSysConfig()`，模板里 `sysConfig.showPipeline`。
 * 聊天端不提供写路径——修改入口在管理界面（PUT /api/admin/settings/display）。
 */
export function useSysConfig(): SysConfig {
  return sysConfig
}
