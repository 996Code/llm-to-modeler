// =============================================================================
// sysConfig 单测：系统配置两开关的默认值 / localStorage 持久化 / 损坏数据回退。
// vitest 默认 node 环境（无 localStorage），用内存 stub 验证持久化链路。
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

// 每个用例前：换新的 stub + 重置模块注册表（单例是模块级状态，必须重新加载）
beforeEach(() => {
  vi.stubGlobal('localStorage', makeStorage())
  vi.resetModules()
})

describe('sysConfig 系统配置单例', () => {
  it('默认值：两个开关都开（需求「默认开启/默认显示」）', async () => {
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
  })

  it('持久化：改开关 → 写入 localStorage；重新加载模块 → 读回上次值', async () => {
    const storage = makeStorage()
    vi.stubGlobal('localStorage', storage)
    const { useSysConfig } = await import('../sysConfig')
    useSysConfig().showPipeline = false
    useSysConfig().showRetrieval = false
    // 模块级 watcher 的持久化走 Vue 调度队列（微任务），flush 后落库
    await flushPromises()
    expect(JSON.parse(storage.getItem('sys_config')!)).toEqual({
      showPipeline: false,
      showRetrieval: false,
    })
    // 重新加载模块：单例从 localStorage 恢复上次的值
    const mod2 = await import('../sysConfig')
    const cfg2 = mod2.useSysConfig()
    expect(cfg2.showPipeline).toBe(false)
    expect(cfg2.showRetrieval).toBe(false)
  })

  it('回退：localStorage 数据损坏（非 JSON）→ 回默认值，不抛异常', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => '{oops not json',
      setItem: () => {},
    })
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
  })

  it('回退：字段类型不对（字符串/缺失）→ 逐字段回默认值', async () => {
    vi.stubGlobal('localStorage', {
      getItem: () => JSON.stringify({ showPipeline: 'yes', foo: 1 }),
      setItem: () => {},
    })
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true) // 非布尔回退
    expect(cfg.showRetrieval).toBe(true) // 缺失回退
  })

  it('resetSysConfig：恢复两个开关为默认开', async () => {
    const { useSysConfig, resetSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    cfg.showPipeline = false
    cfg.showRetrieval = false
    resetSysConfig()
    expect(cfg.showPipeline).toBe(true)
    expect(cfg.showRetrieval).toBe(true)
  })

  it('无 localStorage 环境（node/隐私模式）：内存态照常可用，不抛异常', async () => {
    vi.unstubAllGlobals()
    const { useSysConfig } = await import('../sysConfig')
    const cfg = useSysConfig()
    expect(cfg.showPipeline).toBe(true)
    cfg.showPipeline = false // 写入不持久化但不报错
    expect(cfg.showPipeline).toBe(false)
  })
})
