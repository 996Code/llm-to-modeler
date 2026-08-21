// =============================================================================
// 模块说明：HostPort —— 平台前端与宿主（Host）之间的通信端口接口 + 两种实现。
// -----------------------------------------------------------------------------
// 类比 Java：HostPort ≈ 一个 SPI 接口（Service Provider Interface），
// UI 层（ChatPanel / EmbeddedLayout）只依赖本接口，不直接碰 postMessage。
//
// 两种实现：
//   - PostMessageHostPort：嵌入态，基于 postMessage 与宿主双向通信
//     （升格自原 useEmbedBridge：补 requestId 关联、请求超时、双向 origin 校验）
//   - NullHostPort：独立态，所有方法返回 null / connected=false，
//     UI 据此显示复制/下载按钮——降级是机械的，不写 if (宿主是谁)。
//
// 消息协议详见 doc/嵌入模式总体设计.md §4。
// =============================================================================

// ref：Vue 响应式引用——capabilities 用 ref 包一层，INIT 到达后更新能触发 UI 重渲染
//（应用按钮的 v-if 依赖 capabilities，非响应式会出现"按钮永远不出现"的隐性 bug）
import { ref } from 'vue'
// 透传头存储：INIT/AUTH_UPDATE 下发的鉴权头写入这里，供所有 API 请求携带
//（鉴权头的存取是端口自身的职责，UI 层不经手）
import { setForwardedHeaders } from './forwardHeaders'

// =============================================================================
// 一、协议类型定义（与宿主共享的 TS 类型，可直接拷贝给宿主侧）
// =============================================================================

/** 宿主能力：INIT 里宿主声明自己实现了哪些钩子，双方按交集工作 */
export type HostCapability = 'context' | 'apply' | 'auth' | 'services'

/** 宿主下发身份 + 凭证 */
export interface HostIdentity {
  userId: string
  headers: Record<string, string>
}

// ── 跨模块契约 key 常量（前端写、后端读——拼错在编译/测试时暴露） ──
/** ChatRequest.context 内的制品 key（后端 config.py 读同名 key） */
export const CONTEXT_KEY_ARTIFACT = 'artifact'

/** 宿主当前上下文（AI 修改前必须拉新，防陈旧基线覆盖手动修改） */
export interface HostContext {
  artifact: unknown | null
  revision: string | null
}

/** APPLY 请求载荷 */
export interface ApplyRequest {
  artifact: unknown
  baseRevision: string | null
  summary?: string
  artifactType?: string
}

/** APPLY 结果 */
export type ApplyResult =
  | { ok: true; artifact: unknown; revision: string; contextKey?: string }
  | {
      ok: false
      code: 'REVISION_CONFLICT' | 'RENDER_FAILED' | 'REJECTED_BY_USER' | 'UNSUPPORTED'
      message: string
    }

/** INIT 握手返回（子应用需要的第一批上下文） */
export interface HostInitResult {
  userId: string
  contextKey?: string
  services?: Record<string, string>
  /** 宿主声明的默认插件集（可选；未声明则由 URL/部署方配置兜底） */
  packs?: string[]
}

/**
 * HostPort 接口：UI 层唯一依赖的宿主通信抽象。
 * 所有方法都可失败返回 null / false，调用方据「connected + capabilities」决定降级。
 */
export interface HostPort {
  readonly connected: boolean
  readonly capabilities: ReadonlySet<HostCapability>
  /** 宿主声明的默认插件集（嵌入态 INIT 下发；独立态为 null） */
  readonly packs: string[] | null
  /** 握手；无宿主返回 null → 调用方切独立模式布局 */
  init(): Promise<HostInitResult | null>
  getContext(): Promise<HostContext | null>
  applyArtifact(req: ApplyRequest): Promise<ApplyResult>
  getIdentity(): Promise<HostIdentity | null>
  onAuthUpdated(cb: (identity: HostIdentity) => void): void
  /** 通知宿主收起窗口（宿主只隐藏 iframe，会话保活）——不拆监听器 */
  notifyClose(): void
  /** 请求宿主调整窗口尺寸（如全屏查看 JSON 时临时撑大）。fire-and-forget：
   *  宿主不支持则静默无效果（弹窗仍受 iframe 视口限制），无需等待回执 */
  notifyResize(mode: 'expanded' | 'normal'): void
  /** 完全拆除端口（移除监听、清空 pending）。宿主隐藏窗口请用 notifyClose */
  close(): void
}

// =============================================================================
// 二、共享：origin 白名单
// =============================================================================

/**
 * 出站 targetOrigin 白名单。
 * 顺序：宿主可能先发 INIT 带 origin → 锁定；否则回退环境变量 VITE_HOST_ORIGINS
 * （逗号分隔）；两者都没有时回退 "*"（仅兼容旧 embed-demo 等无鉴权场景，
 * 有 token 的宿主必须配置白名单）。
 */
function readAllowedHostOrigins(): string[] {
  const fromEnv = (import.meta.env?.VITE_HOST_ORIGINS as string | undefined) || ''
  return fromEnv
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

// =============================================================================
// 三、PostMessageHostPort —— 嵌入态实现
// =============================================================================

interface PendingRequest {
  resolve: (payload: any) => void
  reject: (err: Error) => void
  timer: ReturnType<typeof setTimeout>
}

const PROTOCOL_VERSION = 1

export class PostMessageHostPort implements HostPort {
  readonly connected = true

  /** capabilities 用 ref 包裹：INIT 异步到达后更新，UI（应用按钮 v-if）能响应式刷新 */
  private _capsRef = ref<ReadonlySet<HostCapability>>(new Set())
  private _outOrigin: string | null = null
  private _inOrigins: string[] = readAllowedHostOrigins()
  private _pending = new Map<string, PendingRequest>()
  private _authListeners: Array<(i: HostIdentity) => void> = []
  private _messageHandler: ((e: MessageEvent) => void) | null = null
  /** 宿主 INIT 下发的服务地址表（供 chat 请求携带 services 字段） */
  hostServices: Record<string, string> | null = null
  /** 宿主 INIT 下发的 pack 白名单（上层声明默认插件，供 meta/packs 请求过滤） */
  hostPacks: string[] | null = null

  private _nextId = 0
  private _seq = (): string => `r${++this._nextId}`

  constructor() {
    this._messageHandler = (e: MessageEvent) => this._handle(e)
    window.addEventListener('message', this._messageHandler)
  }

  get capabilities(): ReadonlySet<HostCapability> {
    return this._capsRef.value
  }

  /** 宿主声明的默认插件集（INIT 已到达时；否则 null） */
  get packs(): string[] | null {
    return this.hostPacks
  }

  /** 出站发送：targetOrigin 白名单校验 */
  private _post(msg: Record<string, unknown>) {
    if (!window.parent || window.parent === window) return
    // INIT 到达后锁定宿主 origin；否则用 env 预配的白名单；再退 "*"
    const target = this._outOrigin ?? this._inOrigins[0] ?? '*'
    window.parent.postMessage(msg, target)
  }

  /** 入站接收：origin 白名单校验，不在白名单直接丢弃 */
  private _handle(e: MessageEvent) {
    const origin = e.origin
    // 白名单为空（未配置）时不拦（兼容旧场景）；配置了则必须命中
    if (this._inOrigins.length > 0 && !this._inOrigins.includes(origin)) return
    const data = e.data
    if (!data || typeof data !== 'object' || data.src !== 'host') return

    // 请求响应：按 id 找到 pending，resolve
    // id 按协议在信封顶层；同时兼容宿主把 id 误放 payload 里的实现（容错，
    // 两侧由不同人实现时最常见的错位——匹配不到就是 10s/30s 超时）
    const respId =
      data.id ?? (data.payload && typeof data.payload === 'object' ? data.payload.id : undefined)
    if (respId && this._pending.has(respId)) {
      const p = this._pending.get(respId)!
      this._pending.delete(respId)
      clearTimeout(p.timer)
      p.resolve(data.payload)
      return
    }

    switch (data.type) {
      case 'INIT':
        this._outOrigin = origin
        // 鉴权头落库：INIT 是 token 的主入口，写入 forwardHeaders 供所有 API 请求携带
        if (data.payload?.headers) setForwardedHeaders(data.payload.headers)
        if (data.payload?.capabilities) this._capsRef.value = new Set(data.payload.capabilities)
        if (data.payload?.services) this.hostServices = data.payload.services
        // 宿主声明的默认插件集（与 userId 同通道下发，仅嵌入模式有值）
        if (data.payload?.packs) this.hostPacks = data.payload.packs
        break
      case 'AUTH_UPDATE':
        // token 刷新：新鉴权头整体替换（userId 的同步由 onAuthUpdated 监听方处理）
        if (data.payload?.headers) setForwardedHeaders(data.payload.headers)
        this._authListeners.forEach((cb) => cb(data.payload))
        break
      default:
        // 未知 type 一律忽略（协议演进规则：前向兼容）
        break
    }
  }

  /** 请求-响应：发请求、登记 pending、带超时 */
  private _request<T = any>(type: string, payload: Record<string, unknown>, timeoutMs: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const id = this._seq()
      const timer = setTimeout(() => {
        this._pending.delete(id)
        reject(new Error(`HOST_TIMEOUT:${type}`))
      }, timeoutMs)
      this._pending.set(id, { resolve, reject, timer })
      this._post({ v: PROTOCOL_VERSION, src: 'child', id, type, payload })
    })
  }

  // ── HostPort 实现 ──

  init(): Promise<HostInitResult | null> {
    // READY 是通知原语（子→宿），宿主收到后回 INIT（宿→子，非请求响应，走监听）
    this._post({ v: PROTOCOL_VERSION, src: 'child', type: 'READY', payload: {} })
    // INIT 是宿→子的指令，无 id；这里用「等首个 INIT」而不是请求-响应。
    return new Promise<HostInitResult | null>((resolve) => {
      // 首个 INIT 可能先被 _handle 处理（同样监听 INIT 分支）——为防本监听注册前
      // INIT 已到达的极端时序（理论不发生：postMessage 回包必然是异步宏任务），
      // 若 INIT 已处理过（outOrigin 已锁定），直接用缓存结果解析。
      const timeout = setTimeout(() => {
        this._offOnce?.()
        this._offOnce = null
        resolve(null)
      }, 3000)
      this._offOnce = this.onMessageOnce('INIT', (payload) => {
        clearTimeout(timeout)
        this._offOnce = null
        // 与 _handle 的 INIT 分支一致地保存宿主声明（本函数可能先于 _handle 触发）
        if (payload?.packs) this.hostPacks = payload.packs
        resolve({
          userId: payload.userId,
          contextKey: payload.contextKey,
          services: payload.services,
          packs: payload.packs,
        })
      })
    })
  }

  /** 等待某类型的下一条消息（一次性监听，配合 init 用）；同样做 origin 白名单校验 */
  private onMessageOnce(type: string, cb: (payload: any) => void) {
    const handler = (e: MessageEvent) => {
      // 与 _handle 相同的入站白名单：未配置不拦，配置了必须命中
      if (this._inOrigins.length > 0 && !this._inOrigins.includes(e.origin)) return
      if (e.data?.type === type && e.data?.src === 'host') cb(e.data.payload)
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }

  /** 移除一次性监听（init 超时后调用，防泄漏） */
  private _offOnce: (() => void) | null = null

  getContext(): Promise<HostContext | null> {
    return this._request<HostContext>('GET_CONTEXT', {}, 10_000).catch(() => null)
  }

  applyArtifact(req: ApplyRequest): Promise<ApplyResult> {
    return this._request<ApplyResult>('APPLY', { ...req }, 30_000).catch((err) => {
      // 超时通常是链路问题（宿主没回执/信封错位），说人话让用户能定位
      const msg = String(err).includes('HOST_TIMEOUT')
        ? '宿主响应超时（APPLY 无回执）：嵌入链路异常，请刷新页面后重试'
        : String(err)
      return { ok: false, code: 'UNSUPPORTED' as const, message: msg }
    })
  }

  getIdentity(): Promise<HostIdentity | null> {
    return this._request<HostIdentity>('GET_AUTH', {}, 10_000).catch(() => null)
  }

  onAuthUpdated(cb: (i: HostIdentity) => void): void {
    this._authListeners.push(cb)
  }

  /**
   * 通知宿主收起窗口：只发 CLOSE 消息，**不拆监听器、不清 pending**。
   * 宿主（如 mind-designer 悬浮窗）收到后仅隐藏 iframe（会话保活），
   * 重开后 GET_CONTEXT / APPLY 等请求-响应照常工作。
   */
  notifyClose(): void {
    this._post({ v: PROTOCOL_VERSION, src: 'child', type: 'CLOSE', payload: {} })
  }

  notifyResize(mode: 'expanded' | 'normal'): void {
    this._post({
      v: PROTOCOL_VERSION, src: 'child', type: 'RESIZE',
      payload: { mode },
    })
  }

  close(): void {
    this._post({ v: PROTOCOL_VERSION, src: 'child', type: 'CLOSE', payload: {} })
    if (this._messageHandler) window.removeEventListener('message', this._messageHandler)
    this._pending.forEach((p) => clearTimeout(p.timer))
    this._pending.clear()
  }
}

// =============================================================================
// 四、NullHostPort —— 独立态实现（降级）
// =============================================================================

class NullHostPort implements HostPort {
  readonly connected = false
  readonly capabilities: ReadonlySet<HostCapability> = new Set()
  /** 独立态无宿主声明 → 不参与过滤（packs 交给 URL/env） */
  get packs(): string[] | null {
    return null
  }
  init(): Promise<HostInitResult | null> {
    return Promise.resolve(null)
  }
  getContext(): Promise<HostContext | null> {
    return Promise.resolve(null)
  }
  applyArtifact(): Promise<ApplyResult> {
    return Promise.resolve({ ok: false, code: 'UNSUPPORTED', message: 'no host' })
  }
  getIdentity(): Promise<HostIdentity | null> {
    return Promise.resolve(null)
  }
  onAuthUpdated(): void {}
  notifyClose(): void {}

  notifyResize(_mode: 'expanded' | 'normal'): void {}
  close(): void {}
}

// =============================================================================
// 五、单例 + 检测入口
// =============================================================================

/** 是否为嵌入态：URL 带 embed=true，或当前窗口有父窗口（被 iframe 嵌入） */
export function isEmbedded(): boolean {
  const params = new URLSearchParams(window.location.search)
  return params.get('embed') === 'true' || window.parent !== window
}

let _port: HostPort | null = null

/** 获取全局唯一 HostPort（懒加载单例） */
export function getHostPort(): HostPort {
  if (!_port) {
    _port = isEmbedded() ? new PostMessageHostPort() : new NullHostPort()
  }
  return _port
}
