// =============================================================================
// 模块说明：嵌入通信桥（Composition API 版本）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个封装 postMessage 跨窗口通信的工具类。
// 在 Vue 中，以 use 开头、返回响应式状态/方法的函数称为 "Composable"
// （组合式函数），用法类似 React Hooks —— 在组件里调用一次即可复用逻辑。
//
// 职责：
//   作为【嵌入到父系统的 iframe】应用，通过 window.parent.postMessage 与父系统通信：
//     - 通知父系统：配置已生成 / 用户点了应用 / 应用已就绪 / 请求关闭
//     - 监听父系统：接收指令消息（onMessage）
//   headers 透传逻辑不在这里，见 forwardHeaders.ts（已拆分）。
// =============================================================================

// onMounted / onUnmounted 是 Vue 组件生命周期钩子：
//   onMounted   组件挂载到 DOM 后触发（类比 @PostConstruct）
//   onUnmounted 组件销毁前触发（类比 @PreDestroy，常用于清理监听器避免内存泄漏）
import { onMounted, onUnmounted } from 'vue'

/**
 * 嵌入消息的统一结构（约定协议）。
 * type 为消息类型字符串，payload 为任意数据载荷。
 */
export interface EmbedMessage {
  type: string
  payload: Record<string, any>
}

/**
 * 嵌入通信桥组合式函数。
 * 在组件 setup 中调用，返回一组通信方法。
 *
 * @returns sendMessage/onMessage/notifyConfigGenerated/applyConfig/closeWindow/notifyReady
 */
export function useEmbedBridge() {
  // 维护已注册的消息监听器列表，组件卸载时统一移除（防止内存泄漏）。
  // 类比 Java：相当于 List<Consumer<MessageEvent>> listeners
  const listeners: Array<(e: MessageEvent) => void> = []

  /**
   * 向父窗口发送消息（postMessage）。
   * 仅当当前窗口确实有父窗口（被 iframe 嵌入）时才发送，否则静默跳过。
   * @param msg 要发送的消息对象（含 type + payload）
   */
  function sendMessage(msg: EmbedMessage) {
    // window.parent === window 表示当前就是顶层窗口，没有被嵌入
    if (window.parent && window.parent !== window) {
      // 第二个参数 '*' 表示不校验目标源（生产环境应限定为具体域名以防安全风险）
      window.parent.postMessage(msg, '*')
    }
  }

  /**
   * 注册对某种类型消息的监听。
   * @param type    要监听的消息类型字符串
   * @param callback 收到匹配消息时的回调，参数为消息 payload
   * @returns 返回一个"取消监听"函数，调用它即可移除该监听（闭包模式）。
   *          类比 Java：返回一个 Runnable，run 时执行 removeListener。
   */
  function onMessage(type: string, callback: (payload: any) => void) {
    const handler = (e: MessageEvent) => {
      // 仅处理与目标 type 匹配的消息（按约定协议过滤）
      if (e.data?.type === type) {
        callback(e.data.payload)
      }
    }
    window.addEventListener('message', handler)
    listeners.push(handler)
    // 返回反注册函数（闭包捕获 handler），调用方可在合适时机取消监听
    return () => window.removeEventListener('message', handler)
  }

  /** 通知父系统：已生成一份表单配置 */
  function notifyConfigGenerated(config: any) {
    sendMessage({ type: 'MODELER_CONFIG_GENERATED', payload: { config } })
  }

  /** 请求父系统：应用（写入）一份配置 */
  function applyConfig(config: any) {
    sendMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config } })
  }

  /** 请求父系统：关闭当前嵌入窗口 */
  function closeWindow() {
    sendMessage({ type: 'MODELER_CLOSE', payload: {} })
  }

  /** 通知父系统：嵌入应用已就绪（可接收指令） */
  function notifyReady() {
    sendMessage({ type: 'MODELER_READY', payload: {} })
  }

  // 组件挂载后自动通知父系统"我准备好了"（@PostConstruct 语义）
  onMounted(() => {
    notifyReady()
  })

  // 组件卸载前清理所有监听器（@PreDestroy 语义，防内存泄漏）
  onUnmounted(() => {
    listeners.forEach((l) => window.removeEventListener('message', l))
  })

  // 暴露给组件使用的接口集合（类似 Bean 对外提供的公共方法）
  return {
    sendMessage,
    onMessage,
    notifyConfigGenerated,
    applyConfig,
    closeWindow,
    notifyReady,
  }
}
