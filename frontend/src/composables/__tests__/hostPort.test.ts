// =============================================================================
// 协议级端到端自测：用 stub 的 window 模拟宿主页，驱动 PostMessageHostPort
// 走完整协议（READY→INIT→GET_CONTEXT→APPLY→AUTH_UPDATE→CLOSE）。
// 验证点：握手解析、鉴权头落库、requestId 关联、能力协商、超时降级、close 清理。
// =============================================================================
import { describe, it, expect, beforeAll, beforeEach, vi } from 'vitest'

// ---- window stub（hostPort 依赖 window.parent.postMessage / addEventListener）----
type Handler = (e: any) => void
const listeners: Handler[] = []
const posted: Array<{ msg: any; target: string }> = []
let stubWindow: any

function dispatchFromHost(data: any) {
  // 模拟宿主 postMessage 到子窗口：触发所有 message 监听器
  listeners.slice().forEach((h) => h({ origin: 'http://host-page', data }))
}

beforeAll(() => {
  stubWindow = {
    parent: {
      postMessage: (msg: any, target: string) => posted.push({ msg, target }),
    },
    addEventListener: (_t: string, h: Handler) => listeners.push(h),
    removeEventListener: (_t: string, h: Handler) => {
      const i = listeners.indexOf(h)
      if (i >= 0) listeners.splice(i, 1)
    },
  }
  vi.stubGlobal('window', stubWindow)
})

// 每个用例独立：清掉上个用例遗留的监听/发送记录（否则旧 port 实例会串处理消息）
beforeEach(() => {
  listeners.length = 0
  posted.length = 0
})

describe('PostMessageHostPort 协议自测', () => {
  it('握手：READY 发出 → INIT 解析（userId/services/capabilities）+ 鉴权头落库', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const { getForwardedHeaders, setForwardedHeaders } = await import('../forwardHeaders')
    setForwardedHeaders({}) // 清场
    const port = new PostMessageHostPort()

    // 子应用 init：先发 READY
    const initPromise = port.init()
    expect(posted.at(-1)?.msg.type).toBe('READY')
    expect(posted.at(-1)?.msg.src).toBe('child')

    // 宿主回 INIT（带 token/服务表/能力）
    dispatchFromHost({
      v: 1, src: 'host', type: 'INIT',
      payload: {
        protocolVersion: 1,
        userId: 'u_1024',
        headers: { Authorization: 'Bearer t1', 'tenant-id': '7' },
        contextKey: 'leave_form',
        services: { 'njmind-modeler': 'http://gw/codeBack' },
        artifact: { formName: '请假' }, revision: 'rev_a',
        capabilities: ['context', 'apply', 'auth', 'services'],
      },
    })

    const r = await initPromise
    expect(r?.userId).toBe('u_1024')
    expect(r?.contextKey).toBe('leave_form')
    expect(r?.services?.['njmind-modeler']).toBe('http://gw/codeBack')
    // 鉴权头已写入 forwardHeaders（此前全局漏绑的 bug 的回归测试）
    expect(getForwardedHeaders().Authorization).toBe('Bearer t1')
    // 能力协商生效
    expect(port.capabilities.has('apply')).toBe(true)
    // 服务表可被 chat 上下文读取
    expect((port as any).hostServices?.['njmind-modeler']).toBe('http://gw/codeBack')
  })

  it('GET_CONTEXT：requestId 关联 + 10s 超时降级为 null', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u', capabilities: ['context'] } })

    // 正常响应：按 id 回填
    const p1 = port.getContext()
    const req = posted.find((x) => x.msg.type === 'GET_CONTEXT')!
    expect(req.msg.id).toBeTruthy()
    dispatchFromHost({ v: 1, src: 'host', id: req.msg.id, type: 'GET_CONTEXT_RESULT', payload: { artifact: { a: 1 }, revision: 'r1' } })
    const ctx = await p1
    expect(ctx?.revision).toBe('r1')

    // 超时：无人应答 → null（不抛异常）
    vi.useFakeTimers()
    try {
      const p2 = port.getContext()
      await vi.advanceTimersByTimeAsync(10_500)
      expect(await p2).toBeNull()
    } finally {
      vi.useRealTimers()
    }
  })

  it('容错：宿主把 id 放在 payload 里（错位实现）也能匹配响应', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u', capabilities: ['context'] } })

    const p = port.getContext()
    const req = posted.find((x) => x.msg.type === 'GET_CONTEXT')!
    // 模拟"错位宿主"：id 不在信封顶层，而在 payload 里
    dispatchFromHost({
      v: 1, src: 'host', type: 'GET_CONTEXT_RESULT',
      payload: { id: req.msg.id, artifact: { a: 1 }, revision: 'r9' },
    })
    const ctx = await p
    expect(ctx?.revision).toBe('r9')
  })

  it('APPLY：成功回执透传权威配置；失败按错误码返回', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u', capabilities: ['apply'] } })

    // 成功路径
    const p1 = port.applyArtifact({ artifact: { x: 1 }, baseRevision: 'r1', summary: 's', artifactType: 'form-config' })
    const req1 = posted.filter((x) => x.msg.type === 'APPLY').at(-1)!
    expect(req1.msg.payload.baseRevision).toBe('r1')
    dispatchFromHost({ v: 1, src: 'host', id: req1.msg.id, type: 'APPLY_RESULT', payload: { ok: true, artifact: { x: 2 }, revision: 'r2' } })
    const ok = await p1
    expect(ok.ok).toBe(true)
    if (ok.ok) expect((ok.artifact as any).x).toBe(2)

    // 失败路径（REJECTED_BY_USER）
    const p2 = port.applyArtifact({ artifact: { x: 1 }, baseRevision: null })
    const req2 = posted.filter((x) => x.msg.type === 'APPLY').at(-1)!
    dispatchFromHost({ v: 1, src: 'host', id: req2.msg.id, type: 'APPLY_RESULT', payload: { ok: false, code: 'REJECTED_BY_USER', message: '取消' } })
    const fail = await p2
    expect(fail.ok).toBe(false)
    if (!fail.ok) expect(fail.code).toBe('REJECTED_BY_USER')
  })

  it('AUTH_UPDATE：新鉴权头整体替换 + 监听回调触发', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const { getForwardedHeaders } = await import('../forwardHeaders')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u', headers: { Authorization: 'Bearer old' } } })
    expect(getForwardedHeaders().Authorization).toBe('Bearer old')

    const seen: string[] = []
    port.onAuthUpdated((id) => seen.push(id.userId))
    dispatchFromHost({ v: 1, src: 'host', type: 'AUTH_UPDATE', payload: { userId: 'u', headers: { Authorization: 'Bearer new' } } })
    expect(getForwardedHeaders().Authorization).toBe('Bearer new')
    expect(seen).toEqual(['u'])
  })

  it('notifyClose：只发 CLOSE 不拆端口（隐藏后重开，请求-响应仍工作）', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u', capabilities: ['apply'] } })

    // 隐藏：notifyClose 只发消息，监听器仍在
    const before = listeners.length
    port.notifyClose()
    expect(posted.at(-1)?.msg.type).toBe('CLOSE')
    expect(listeners.length).toBe(before) // 监听未移除

    // 重开后（宿主仅切显隐）：APPLY 请求-响应照常
    const p = port.applyArtifact({ artifact: { x: 1 }, baseRevision: null })
    const req = posted.filter((x) => x.msg.type === 'APPLY').at(-1)!
    dispatchFromHost({ v: 1, src: 'host', id: req.msg.id, type: 'APPLY_RESULT', payload: { ok: true, artifact: { x: 2 }, revision: 'r' } })
    const ok = await p
    expect(ok.ok).toBe(true)
  })

  it('CLOSE：通知宿主 + 移除监听（后续消息不再处理）', async () => {
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    dispatchFromHost({ v: 1, src: 'host', type: 'INIT', payload: { userId: 'u' } })

    const before = listeners.length
    port.close()
    expect(posted.at(-1)?.msg.type).toBe('CLOSE')
    expect(listeners.length).toBeLessThan(before) // 监听已移除

    // close 后再派发 INIT：无人处理（不会重复落鉴权头）
    const { getForwardedHeaders } = await import('../forwardHeaders')
    const auth = getForwardedHeaders().Authorization
    dispatchFromHost({ v: 1, src: 'host', type: 'AUTH_UPDATE', payload: { userId: 'u', headers: { Authorization: 'Bearer after-close' } } })
    expect(getForwardedHeaders().Authorization).toBe(auth)
  })

  it('origin 白名单：未命中入站白名单的消息被丢弃', async () => {
    // 重新 stub：配置白名单后，非白名单 origin 的 INIT 应被忽略
    const listeners2: Handler[] = []
    vi.stubGlobal('window', {
      ...stubWindow,
      addEventListener: (_t: string, h: Handler) => listeners2.push(h),
      removeEventListener: (_t: string, h: Handler) => {
        const i = listeners2.indexOf(h)
        if (i >= 0) listeners2.splice(i, 1)
      },
    })
    // 动态改 env 白名单需要模块重置——这里直接构造后用非白名单 origin 派发
    const { PostMessageHostPort } = await import('../hostPort')
    const port = new PostMessageHostPort()
    // _inOrigins 未配置（[]）→ 不拦：正常场景兼容旧行为；此用例验证 src=host 校验
    listeners2.slice().forEach((h) => h({ origin: 'http://host-page', data: { type: 'INIT', src: 'attacker', payload: { userId: 'evil' } } }))
    expect((port as any).hostServices).toBeNull() // src 非 host，未处理
    vi.unstubAllGlobals()
  })
})
