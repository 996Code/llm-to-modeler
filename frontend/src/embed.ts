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
//       baseUrl: 'https://modeler.example.com',
//       userId: 'user_123',
//       onConfigGenerated: (config) => { ... },
//       onConfigApply: (config) => { ... },
//     })
//     modeler.open()
//   </script>
//
// 这个文件会被 Vite 单独打包成一个独立的 JS 文件（多入口构建），
// 不会与主应用打在一起。
//
// 嵌入契约 v1 信封协议（READY/INIT/GET_CONTEXT/APPLY/GET_AUTH/CLOSE/RESIZE），
// 与 mind-designer 适配器同标准，见 src/composables/hostPort.ts。
// =============================================================================

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
  /** 宿主"设计器"当前配置的取数钩子（GET_CONTEXT/APPLY 的 artifact 来源）。
   *  返回 null 表示当前无配置。与 mind-designer 的 currentVo() 同角色。 */
  getArtifact?: () => any
  onConfigGenerated?: (config: any) => void        // 回调：配置已生成（仅诊断用，交互在 iframe 内自足）
  onConfigApply?: (config: any) => void            // 回调：iframe 内点了"应用"（宿主渲染到自己的设计器）
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
   * 根据展开状态更新浮动按钮（私有）。
   * 打开悬浮窗时隐藏悬浮球（子应用内自带标题栏与关闭按钮，悬浮球再变 ✕ 会
   * 与窗口边缘重叠）；关闭后恢复显示，可再次打开。
   */
  private updateFloatBtn() {
    if (!this.floatBtn) return
    this.floatBtn.innerHTML = '💬'
    this.floatBtn.style.background = '#3370ff'
    this.floatBtn.style.boxShadow = '0 4px 12px rgba(51,112,255,0.4)'
    this.floatBtn.style.display = this.isOpen ? 'none' : 'flex'
  }

  /**
   * 发送 INIT（幂等：只发一次，READY 重发不重复）。
   * payload 对齐 mind-designer 的握手标准：capabilities 通告齐全，iframe 内
   * 子应用的应用按钮/上下文拉取才会启用（capabilities 缺 apply 时子应用退化）。
   */
  private _initSent = false
  private trySendInit() {
    if (this._initSent || !this.iframe) return
    this._initSent = true
    this.postToChild('INIT', {
      userId: this.options.userId,
      headers: this.options.headers || {},
      capabilities: ['context', 'apply', 'auth', 'resize'],
      // 宿主"设计器"的当前配置（无则为 null，子应用首次对话走 create）
      artifact: this.currentArtifact(),
      revision: this.currentRevision(),
    })
  }

  /** 宿主→子消息统一出口：嵌入契约信封 {v, src:'host', id?, type, payload}。
   *  targetOrigin 用 iframe 的精确 origin（防消息泄露给被重定向的窗口）；
   *  baseUrl 解析失败时退回 '*'（demo 宽松场景） */
  private postToChild(type: string, payload: Record<string, unknown>, id?: string) {
    this.iframe?.contentWindow?.postMessage(
      { v: 1, src: 'host', ...(id !== undefined ? { id } : {}), type, payload },
      this.iframeOrigin(),
    )
  }

  /** iframe 的 origin（从 baseUrl 推导；同源相对地址即宿主自身） */
  private iframeOrigin(): string {
    try {
      return new URL(this.options.baseUrl, window.location.origin).origin
    } catch {
      return '*'
    }
  }

  /** 当前"设计器"配置（宿主注册的取数钩子；无钩子视为无配置） */
  private currentArtifact(): any {
    try {
      return this.options.getArtifact?.() ?? null
    } catch {
      return null
    }
  }

  /** 简易 revision（变更检测用）：配置 JSON 的指纹。无配置时为 null */
  private currentRevision(): string | null {
    const a = this.currentArtifact()
    if (a == null) return null
    const s = JSON.stringify(a)
    let h = 0
    for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0
    return `r${s.length}_${Math.abs(h)}`
  }

  /**
   * 处理来自 iframe 的消息（私有）。
   * 根据消息类型分发到宿主注册的回调。
   * @param e 消息事件
   */
  private handleMessage(e: MessageEvent) {
    const { type, payload, src, id } = e.data || {}
    // 嵌入契约：子→宿消息带 src:'child'（双向身份标识）
    if (src !== 'child') return
    switch (type) {
      // 子应用就绪 → 发 INIT（握手时序：READY 前 hostPort 监听未挂，早发会丢）
      case 'READY':
        this.trySendInit()
        break
      // 拉最新"设计器"配置（子应用每次发消息前都会拉，作为 AI 的修改基线）
      case 'GET_CONTEXT':
        this.postToChild('GET_CONTEXT_RESULT', {
          artifact: this.currentArtifact(),
          revision: this.currentRevision(),
        }, id)
        break
      // iframe 内点了"应用"：渲染回调交给宿主 + 回执（无回执子应用 30s 超时报链路异常）
      case 'APPLY': {
        const artifact = payload?.artifact
        try {
          this.options.onConfigApply?.(artifact)
          this.postToChild('APPLY_RESULT', {
            ok: true,
            artifact,
            revision: this.currentRevision(),
          }, id)
        } catch (err) {
          this.postToChild('APPLY_RESULT', {
            ok: false, code: 'HOST_ERROR', message: String(err),
          }, id)
        }
        break
      }
      // 鉴权查询（token 刷新场景）
      case 'GET_AUTH':
        this.postToChild('GET_AUTH_RESULT', {
          userId: this.options.userId,
          headers: this.options.headers || {},
        }, id)
        break
      // iframe 请求关闭（新契约消息名）
      case 'CLOSE':
        this.close()
        break
      // iframe 请求调整窗口尺寸（如全屏查看 JSON 时撑大，关闭后恢复）
      case 'RESIZE': {
        const expanded = payload?.mode === 'expanded'
        if (this.container) {
          this.container.style.width = expanded ? 'min(1100px, calc(100vw - 64px))' : '400px'
          this.container.style.height = expanded ? 'min(820px, calc(100vh - 120px))' : '600px'
          this.container.style.transition = 'width 0.25s ease, height 0.25s ease'
        }
        break
      }
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
      bottom: 24px;
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

    // iframe 加载完成后发送 INIT（嵌入契约信封：{v, src:'host', type, payload}）。
    // 注意：必须等子应用的 READY 再发——onload 时子应用的 hostPort 监听可能
    // 还没挂上，早发会丢。READY 由 handleMessage 收到后触发 sendInit。
    this.iframe.onload = () => {
      this.trySendInit()
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
   * 设置上下文（更新 userId 并通知子应用刷新鉴权）。
   * 走契约的 AUTH_UPDATE 通知（INIT 只在握手时发一次，后续身份变化增量通知）。
   */
  setContext(context: { userId?: string }) {
    if (context.userId) this.options.userId = context.userId
    if (this.iframe) {
      this.iframe.contentWindow?.postMessage(
        {
          v: 1,
          src: 'host',
          type: 'AUTH_UPDATE',
          payload: { userId: context.userId || this.options.userId },
        },
        this.iframeOrigin(),
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
