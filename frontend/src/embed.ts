// =============================================================================
// 模块说明：嵌入 SDK 入口（供宿主系统集成的脚本）
// -----------------------------------------------------------------------------
// 类比 Java：相当于一个对外发布的 SDK 客户端类（如 SDK 中的 LLMFormModelerClient）。
// 宿主系统（业务方）通过 <script src="embed.js"> 引入，再 new LLMFormModeler(options)
// 即可在自己的页面里嵌入一个浮窗式表单建模器。
//
// 用法（宿主系统侧）：
//   <script src="https://modeler.example.com/embed.js"></script>
//   <script>
//     const modeler = new LLMFormModeler({
 *       baseUrl: 'https://modeler.example.com',
 *       userId: 'user_123',
 *       onConfigGenerated: (config) => { ... },
 *       onConfigApply: (config) => { ... },
 *     })
 *     modeler.open()
 *   </script>
 *
 * 这个文件会被 Vite 单独打包成一个独立的 JS 文件（多入口构建），
 * 不会与主应用打在一起。
 * =============================================================================
 */

/**
 * SDK 配置选项（宿主系统构造时传入）。
 * 大部分字段可选（?），有默认值或非必填。
 */
export interface EmbedOptions {
  baseUrl: string                                  // 建模器服务的基础 URL（iframe 加载来源）
  userId?: string                                  // 当前用户 ID（透传给 iframe）
  /** 透传到上游 modeler 的请求头（如 Authorization、X-Tenant-Id 等） */
  headers?: Record<string, string>                 // 要透传给 iframe 的请求头
  position?: 'bottom-right' | 'bottom-left'        // 浮窗位置（右下/左下）
  theme?: 'light' | 'dark'                         // 主题
  onConfigGenerated?: (config: any) => void        // 回调：配置已生成
  onConfigApply?: (config: any) => void            // 回调：用户点了"应用配置"
  onClose?: () => void                             // 回调：浮窗被关闭
}

/**
 * SDK 主类 —— 宿主系统通过 new LLMFormModeler(options) 使用。
 *
 * 设计模式：组合了"浮动按钮 + iframe 容器 + 消息桥"三部分，
 *          对外只暴露 open/close/toggle/destroy/setContext 等方法（外观模式 Facade）。
 *
 * 类比 Java：这是一个持有状态的客户端类，字段私有、方法公有，
 *           构造时自动初始化（init），生命周期由宿主控制。
 */
export class LLMFormModeler {
  // private 字段（TS 访问修饰符，与 Java 一致）
  private options: EmbedOptions
  private container: HTMLDivElement | null = null   // iframe 外层容器 DOM
  private iframe: HTMLIFrameElement | null = null   // 实际加载应用的 iframe
  private floatBtn: HTMLDivElement | null = null    // 右下角浮动按钮 DOM
  private isOpen = false                            // 当前是否展开

  /**
   * 构造函数：合并默认值后保存配置，并立即初始化浮窗。
   * @param options 宿主传入的配置
   */
  constructor(options: EmbedOptions) {
    // 展开运算符合并：后面的 options 覆盖前面的默认值
    this.options = {
      position: 'bottom-right',
      theme: 'light',
      ...options,
    }
    this.init()
  }

  /**
   * 初始化：创建浮动按钮并注册消息监听（私有方法）。
   */
  private init() {
    // Create floating button
    // 用原生 DOM API 创建浮动按钮（设置大量内联样式）
    this.floatBtn = document.createElement('div')
    this.floatBtn.style.cssText = `
      position: fixed;
      ${this.options.position === 'bottom-left' ? 'left' : 'right'}: 24px;
      bottom: 24px;
      width: 56px; height: 56px;
      border-radius: 50%;
      background: #3370ff;
      color: #fff;
      display: flex; align-items: center; justify-content: center;
      font-size: 24px; cursor: pointer;
      box-shadow: 0 4px 12px rgba(51,112,255,0.4);
      z-index: 99999;
      transition: transform 0.2s;
    `
    this.floatBtn.innerHTML = '💬'
    // 悬停放大效果（箭头函数保留 this 指向）
    this.floatBtn.onmouseenter = () => (this.floatBtn!.style.transform = 'scale(1.1)')
    this.floatBtn.onmouseleave = () => (this.floatBtn!.style.transform = 'scale(1)')
    // 点击切换展开/收起
    this.floatBtn.onclick = () => this.toggle()
    document.body.appendChild(this.floatBtn)

    // Listen for messages from iframe
    // 监听 iframe（子窗口）通过 postMessage 发回来的消息
    window.addEventListener('message', (e) => this.handleMessage(e))
  }

  /**
   * 根据展开状态更新浮动按钮外观（私有）。
   */
  private updateFloatBtn() {
    if (!this.floatBtn) return
    // 打开时切换成关闭图标（✕），关闭时恢复聊天气泡（💬）
    if (this.isOpen) {
      this.floatBtn.innerHTML = '✕'
      this.floatBtn.style.background = '#86909c'
      this.floatBtn.style.boxShadow = '0 4px 12px rgba(134,144,156,0.4)'
    } else {
      this.floatBtn.innerHTML = '💬'
      this.floatBtn.style.background = '#3370ff'
      this.floatBtn.style.boxShadow = '0 4px 12px rgba(51,112,255,0.4)'
    }
  }

  /**
   * 处理来自 iframe 的消息（私有）。
   * 根据消息类型分发到宿主注册的回调。
   * @param e 消息事件
   */
  private handleMessage(e: MessageEvent) {
    // 解构出 type 和 payload（e.data 可能为空，给个兜底 {}）
    const { type, payload } = e.data || {}
    switch (type) {
      // iframe 内生成了配置 → 通知宿主
      case 'MODELER_CONFIG_GENERATED':
        // 可选链 ?. ：仅当 onConfigGenerated 存在时才调用
        this.options.onConfigGenerated?.(payload.config)
        break
      // 用户在 iframe 内点了"应用配置" → 通知宿主
      case 'MODELER_CONFIG_APPLY':
        this.options.onConfigApply?.(payload.config)
        break
      // iframe 请求关闭
      case 'MODELER_CLOSE':
        this.close()
        break
    }
  }

  /**
   * 创建 iframe 容器并加载应用（私有，懒创建）。
   */
  private createIframe() {
    // 外层容器：定位 + 尺寸 + 阴影
    this.container = document.createElement('div')
    this.container.style.cssText = `
      position: fixed;
      ${this.options.position === 'bottom-left' ? 'left' : 'right'}: 24px;
      bottom: 90px;
      width: 400px; height: 600px;
      max-width: calc(100vw - 48px); max-height: calc(100vh - 120px);
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0,0,0,0.15);
      overflow: hidden;
      z-index: 99999;
      background: #fff;
    `

    // 创建 iframe，把 userId 作为 URL 参数传给应用
    this.iframe = document.createElement('iframe')
    const params = new URLSearchParams({
      embed: 'true',
      // 条件展开：仅当有 userId 时才加入
      ...(this.options.userId ? { userId: this.options.userId } : {}),
    })
    // 拼接最终 URL：baseUrl/?embed=true&userId=xxx
    this.iframe.src = `${this.options.baseUrl}/?${params}`
    this.iframe.style.cssText = 'width: 100%; height: 100%; border: none;'
    // 授予 iframe 剪贴板写入权限（应用配置时可能需要复制）
    this.iframe.allow = 'clipboard-write'

    // iframe 加载完成后，通过 postMessage 发送 userId + headers
    this.iframe.onload = () => {
      // contentWindow 是 iframe 内部的 window 对象，向它 postMessage 即可跨窗口通信
      this.iframe?.contentWindow?.postMessage({
        type: 'MODELER_INIT',
        payload: {
          userId: this.options.userId,
          headers: this.options.headers || {},
        },
      }, '*')
    }

    this.container.appendChild(this.iframe)
    document.body.appendChild(this.container)
  }

  /** 打开浮窗（首次会创建 iframe，之后仅显示，保留对话历史） */
  open() {
    if (this.isOpen) return
    // 首次打开时创建 iframe，之后只显示（保留对话历史）
    if (!this.container) {
      this.createIframe()
    }
    this.container!.style.display = 'block'  // ! 非空断言：明确告诉编译器此处 container 一定存在
    this.isOpen = true
    this.updateFloatBtn()
  }

  /** 关闭浮窗（仅隐藏不销毁，下次打开继续之前的对话） */
  close() {
    if (!this.isOpen) return
    // 隐藏而非销毁，下次打开能继续之前的对话
    if (this.container) {
      this.container.style.display = 'none'
    }
    this.isOpen = false
    this.updateFloatBtn()
    this.options.onClose?.()
  }

  /** 切换展开/收起 */
  toggle() {
    this.isOpen ? this.close() : this.open()
  }

  /** 彻底销毁：关闭 + 移除 DOM + 解绑监听（宿主登出时调用） */
  destroy() {
    this.close()
    this.floatBtn?.remove()
    this.floatBtn = null
    // 注意：这里 removeEventListener 传的是同一个箭头函数引用才能正确解绑
    window.removeEventListener('message', this.handleMessage)
  }

  /**
   * 设置上下文（如宿主当前正在编辑的表单）。
   * @param context 包含 formCode / userId 的上下文
   */
  setContext(context: { formCode?: string; userId?: string }) {
    if (context.userId) this.options.userId = context.userId
    if (this.iframe) {
      this.iframe.contentWindow?.postMessage(
        { type: 'MODELER_INIT', payload: context },
        '*',
      )
    }
  }
}

// Auto-register on window
// 自动挂到全局 window 上，让宿主通过 <script> 引入后能直接用 window.LLMFormModeler
if (typeof window !== 'undefined') {
  // (window as any) —— 强制类型转换（TS 的 as 类似 Java 的强制转换 (Type)），
  // 让编译器接受给 window 添加任意属性。
  ;(window as any).LLMFormModeler = LLMFormModeler
}

// 默认导出（default export）：import 时可用任意名字，如 import Foo from './embed'
export default LLMFormModeler
