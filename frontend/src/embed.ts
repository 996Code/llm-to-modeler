/**
 * embed.js — SDK entry for host systems to embed the form modeler.
 *
 * Usage in host system:
 *   <script src="https://modeler.example.com/embed.js"></script>
 *   <script>
 *     const modeler = new LLMFormModeler({
 *       baseUrl: 'https://modeler.example.com',
 *       userId: 'user_123',
 *       onConfigGenerated: (config) => { ... },
 *       onConfigApply: (config) => { ... },
 *     })
 *     modeler.open()
 *   </script>
 *
 * This file is built separately (Vite multi-entry) as a standalone JS file.
 */

export interface EmbedOptions {
  baseUrl: string
  userId?: string
  /** 透传到上游 modeler 的请求头（如 Authorization、X-Tenant-Id 等） */
  headers?: Record<string, string>
  position?: 'bottom-right' | 'bottom-left'
  theme?: 'light' | 'dark'
  onConfigGenerated?: (config: any) => void
  onConfigApply?: (config: any) => void
  onClose?: () => void
}

export class LLMFormModeler {
  private options: EmbedOptions
  private container: HTMLDivElement | null = null
  private iframe: HTMLIFrameElement | null = null
  private floatBtn: HTMLDivElement | null = null
  private isOpen = false

  constructor(options: EmbedOptions) {
    this.options = {
      position: 'bottom-right',
      theme: 'light',
      ...options,
    }
    this.init()
  }

  private init() {
    // Create floating button
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
    this.floatBtn.onmouseenter = () => (this.floatBtn!.style.transform = 'scale(1.1)')
    this.floatBtn.onmouseleave = () => (this.floatBtn!.style.transform = 'scale(1)')
    this.floatBtn.onclick = () => this.toggle()
    document.body.appendChild(this.floatBtn)

    // Listen for messages from iframe
    window.addEventListener('message', (e) => this.handleMessage(e))
  }

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

  private handleMessage(e: MessageEvent) {
    const { type, payload } = e.data || {}
    switch (type) {
      case 'MODELER_CONFIG_GENERATED':
        this.options.onConfigGenerated?.(payload.config)
        break
      case 'MODELER_CONFIG_APPLY':
        this.options.onConfigApply?.(payload.config)
        break
      case 'MODELER_CLOSE':
        this.close()
        break
    }
  }

  private createIframe() {
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

    this.iframe = document.createElement('iframe')
    const params = new URLSearchParams({
      embed: 'true',
      ...(this.options.userId ? { userId: this.options.userId } : {}),
    })
    this.iframe.src = `${this.options.baseUrl}/?${params}`
    this.iframe.style.cssText = 'width: 100%; height: 100%; border: none;'
    this.iframe.allow = 'clipboard-write'

    // iframe 加载完成后，通过 postMessage 发送 userId + headers
    this.iframe.onload = () => {
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

  open() {
    if (this.isOpen) return
    // 首次打开时创建 iframe，之后只显示（保留对话历史）
    if (!this.container) {
      this.createIframe()
    }
    this.container!.style.display = 'block'
    this.isOpen = true
    this.updateFloatBtn()
  }

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

  toggle() {
    this.isOpen ? this.close() : this.open()
  }

  destroy() {
    this.close()
    this.floatBtn?.remove()
    this.floatBtn = null
    window.removeEventListener('message', this.handleMessage)
  }

  /** Set context (e.g., current form being edited) */
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
if (typeof window !== 'undefined') {
  ;(window as any).LLMFormModeler = LLMFormModeler
}

export default LLMFormModeler
