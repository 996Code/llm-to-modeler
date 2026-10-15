// =============================================================================
// sysConfig 单测：只读单例的服务端数据源 / localStorage 缓存回退 / 默认值。
// vitest 默认 node 环境（无 localStorage），用内存 stub 验证缓存链路；
// services/api 模块整体 mock——本测只关心 sysConfig 的回退链，不关心 HTTP。
// =============================================================================
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { flushPromises } from '@vue/test-utils'

// 内存版 localStorage stub（getItem/setItem 与真实 Storage 同契约）
function makeStorage() {
  const map = new Map<string, string>()
  return {
    getItem: (k: string) => (map.has(k) ? map.get(k)! : null),
    setItem: (k: string, v: string) => void map.set(k, v),
  }
}

// mock services/api：fetchDisplaySettings 的行为由各用例注入
const fetchDisplaySettings = vi.fn()
vi.mock('../../services/api', () => ({
  fetchDisplaySettings: (...args: unknown[]) => fetchDisplaySettings(...args),
}))

// 每个用例前：换新的 stub + 重置模块注册表（单例是模块级状态，必须重新加载）
beforeEach(() => {
  vi.stubGlobal('localStorage', makeStorage())
  vi.resetModules()
  fetchDisplaySettings.mockReset()
})

describe('sysConfig 系统配置只读单例（数据源=后端 API）', () => {
  it('初始默认值：两个开关都开（需求「默认开启/默认显示」），不发请求', async () => {
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
    expect(fetchDisplaySettings).not.toHaveBeenCalled()
  })

  it('refresh 成功：服务端值覆盖内存态并回写 localStorage 缓存', async () => {
    const storage = makeStorage()
    vi.stubGlobal('localStorage', storage)
    fetchDisplaySettings.mockResolvedValue({ showPipeline: false, showRetrieval: false })
    const { useSysConfig, refresh } = await import('../sysConfig')
    await refresh()
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(false)
    expect(cfg.showRetrieval).toBe(false)
    // 成功后回写缓存（仅缓存，不是配置入口）
    expect(JSON.parse(storage.getItem('sys_config')!)).toEqual({
      showPipeline: false,
      showRetrieval: false,
    })
  })

  it('refresh 失败：回退 localStorage 缓存值，不抛异常', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => JSON.stringify({ showPipeline: false, showRetrieval: true }),
      setItem: () => {},
    })
    fetchDisplaySettings.mockRejectedValue(new Error('network down'))
    const { useSysConfig, refresh } = await import('../sysConfig')
    await expect(refresh()).resolves.toBeUndefined()
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(false) // 缓存值
    expect(cfg.showRetrieval).toBe(true)
  })

  it('refresh 失败且无缓存：回默认值', async () => {
    fetchDisplaySettings.mockRejectedValue(new Error('network down'))
    const { useSysConfig, refresh } = await import('../sysConfig')
    await refresh()
    expect(useSysConfig().showPipeline).toBe(true)
    expect(useSysConfig().showRetrieval).toBe(true)
  })

  it('缓存损坏（非 JSON）→ 初始回默认值，refresh 失败仍保持默认', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => '{oops not json',
      setItem: () => {},
    })
    fetchDisplaySettings.mockRejectedValue(new Error('network down'))
    const { useSysConfig, refresh } = await import('../sysConfig')
    await refresh()
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
  })

  it('缓存字段类型不对（字符串/缺失）→ 逐字段回默认值', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => JSON.stringify({ showPipeline: 'yes', foo: 1 }),
      setItem: () => {},
    })
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true) // 非布尔回退
    expect(cfg.showRetrieval).toBe(true) // 缺失回退
  })

  it('服务端返回异常形状（非布尔）→ 逐字段保持当前值，不污染内存态', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => JSON.stringify({ showPipeline: false, showRetrieval: false }),
      setItem: () => {},
    })
    fetchDisplaySettings.mockResolvedValue({ showPipeline: 'broken' })
    const { useSysConfig, refresh } = await import('../sysConfig')
    await refresh()
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(false) // 非布尔 → 保持缓存值
    expect(cfg.showRetrieval).toBe(false) // 缺失 → 保持缓存值
  })

  it('并发 refresh 去重：多个组件同时挂载只发一次请求', async () => {
    let resolveFn!: (v: unknown) => void
    fetchDisplaySettings.mockReturnValue(new Promise((r) => { resolveFn = r }))
    const { refresh } = await import('../sysConfig')
    const p1 = refresh()
    const p2 = refresh()
    resolveFn({ showPipeline: true, showRetrieval: true })
    await Promise.all([p1, p2])
    expect(fetchDisplaySettings).toHaveBeenCalledTimes(1)
  })

  it('无 localStorage 环境（node/隐私模式）：内存态照常可用，不抛异常', async () => {
    vi.unstubAllGlobals()
    fetchDisplaySettings.mockRejectedValue(new Error('network down'))
    const { useSysConfig, refresh } = await import('../sysConfig')
    await refresh()
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
  })
})
