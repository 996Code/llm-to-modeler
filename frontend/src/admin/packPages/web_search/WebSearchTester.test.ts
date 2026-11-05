// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { mount, flushPromises } from "@vue/test-utils"
import { defineComponent } from "vue"
import WebSearchTester from "./WebSearchTester.vue"
import { wsApi } from "../../api"

const saved = { id: "saved", query: "历史问题", results: [{ title: "已保存来源", url: "https://source.test", content: "摘要" }], answer: "已保存答案 [来源:1]", cached: false, resultCount: 1, operator: "", createdAt: "now" }
let posts: string[]
let wrapper: ReturnType<typeof mount>
const InputSearch = defineComponent({ emits: ["search", "update:value"], template: `<button class="search" @click="$emit(\u0027search\u0027)">搜索</button>` })
beforeEach(() => {
  posts = []
})
afterEach(() => { wrapper?.unmount(); vi.restoreAllMocks(); document.body.innerHTML = "" })

async function open(logError?: string) {
  const previous = wsApi.defaults.adapter
  wsApi.defaults.adapter = async config => {
    if (config.method === "post") posts.push(config.url!)
    return { data: config.method === "get" ? [saved] : { ...saved, logId: undefined, logError }, status: 200, statusText: "OK", headers: {}, config }
  }
  wrapper = mount(WebSearchTester, { attachTo: document.body, global: {
    provide: { loadSafely: async (fn: () => Promise<void>) => fn() },
    stubs: { "a-input-search": InputSearch, "a-select": true, "a-select-option": true, "a-tag": true, "a-empty": true },
  } })
  await flushPromises()
  return () => { wsApi.defaults.adapter = previous }
}

describe("saved web search history", () => {
  it("restores saved results and answer without posting a new search", async () => {
    const restore = await open()
    try {
      await wrapper.get(".ws-logs-chip").trigger("click")
      await flushPromises()
      expect(wrapper.text()).toContain("已保存答案")
      expect(wrapper.text()).toContain("已保存来源")
      expect(posts).toEqual([])
      const source = wrapper.get(".ws-item").element
      const scroll = vi.fn()
      Object.defineProperty(source, "scrollIntoView", { value: scroll })
      await wrapper.get(".md-ref-web").trigger("click")
      expect(scroll).toHaveBeenCalledOnce()
    } finally { restore() }
  })
  it("explicit search remains available and shows unsaved-log warning", async () => {
    const restore = await open("调研记录未保存")
    try {
      await wrapper.get(".ws-logs-chip").trigger("click")
      await wrapper.get(".search").trigger("click")
      await flushPromises()
      expect(posts).toEqual(["/web-search"])
      expect(document.body.textContent).toContain("调研记录未保存")
    } finally { restore() }
  })
})
