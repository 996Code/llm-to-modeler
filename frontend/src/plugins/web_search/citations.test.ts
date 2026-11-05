// @vitest-environment jsdom
import { describe, expect, it } from "vitest"
import { marked } from "marked"
import { renderWebSearchAnswer, safeWebSourceUrl } from "./citations"

const sources = [{ url: "https://source.test/a" }, { url: "javascript:alert(1)" }]
function render(text: string) {
  const rendered = renderWebSearchAnswer(text, sources, value => marked.parse(value) as string)
  const root = document.createElement("div")
  root.innerHTML = rendered.html
  return { root, ...rendered }
}

describe("web search citations with real Markdown and DOM", () => {
  it("converts only valid body text and preserves Markdown link title, href and label", () => {
    const { root, cited } = render(`[link [来源:1]](https://example.test/[来源:1] "title [来源:1]") 正文 [来源:1] [来源:2] [来源:0] [来源:3]`)
    const link = root.querySelector("a")!
    expect(link.title).toBe("title [来源:1]")
    expect(decodeURI(link.getAttribute("href")!)).toBe("https://example.test/[来源:1]")
    expect(link.textContent).toBe("link [来源:1]")
    expect(link.querySelector(".md-ref-web")).toBeNull()
    expect(root.querySelectorAll(".md-ref-web")).toHaveLength(1)
    expect(root.querySelector(".md-ref-web")?.getAttribute("data-source-idx")).toBe("1")
    expect([...cited]).toEqual([1])
    expect(root.textContent).toContain("[来源:2] [来源:0] [来源:3]")
  })

  it("skips inline code and pre blocks but converts surrounding body text", () => {
    const { root } = render("`[来源:1]`\n\n```text\n[来源:1]\n```\n\n**正文 [来源：1]**")
    expect(root.querySelector("code")?.textContent).toBe("[来源:1]")
    expect(root.querySelector("pre")?.textContent).toContain("[来源:1]")
    expect(root.querySelector("code .md-ref-web, pre .md-ref-web")).toBeNull()
    expect(root.querySelectorAll("strong .md-ref-web")).toHaveLength(1)
  })

  it("sanitizes rendered HTML after citation insertion", () => {
    const { root } = render(`<img src=x onerror="alert(1)"><script>alert(1)</script><a href="javascript:alert(1)">unsafe [来源:1]</a> 正文 [来源:1]`)
    expect(root.querySelector("script, [onerror], a[href]")).toBeNull()
    expect(root.querySelectorAll(".md-ref-web")).toHaveLength(1)
  })

  it.each(["javascript:alert(1)", "data:text/html,evil", "//evil.test", "/relative", "java\nscript:alert(1)", "file:///tmp/x"])("rejects unsafe source URL %s", url => {
    expect(safeWebSourceUrl(url)).toBeUndefined()
  })
  it.each(["https://source.test/x", "http://source.test/x"])("accepts web URL %s", url => {
    expect(safeWebSourceUrl(url)).toBe(url)
  })
})
