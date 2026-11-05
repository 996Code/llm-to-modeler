// 联网搜索回答的 [来源:N] 角标渲染(管理端 WebSearchTester 与对话端
// WebSearchResultCard 共用)。旧写法是对渲染后的 html 字符串做正则替换,
// 会误伤链接 href/title 属性与 code/pre 代码块里的同形文本——审查缺陷。
// 正确做法:先渲染 Markdown,再解析 DOM 只遍历文本节点做替换,最后统一
// 过 DOMPurify。实现只依赖 DOMParser/DOMPurify,node(jsdom 测试)与浏览器
// 两种环境都可用。
import DOMPurify from 'dompurify'

/** 来源 URL 白名单:仅放行 http/https 绝对地址(javascript:/data:/相对路径
 *  一律拒绝)。浏览器解析协议前会忽略 ASCII 控制符与空白("java\nscript:"
 *  这类绕过手法先剔除再判定),通过时返回原字符串,否则 undefined。 */
export function safeWebSourceUrl(url: string): string | undefined {
  if (typeof url !== 'string') return undefined
  const normalized = url.replace(/[\u0000-\u0020]/g, '')
  return /^https?:\/\//i.test(normalized) ? url : undefined
}

export interface WebSearchCitationSource {
  url: string
}

export interface RenderedWebSearchAnswer {
  html: string
  /** 实际被替换成角标的来源编号(非法编号不算),供来源卡高亮 */
  cited: Set<number>
}

// [来源:1] / [来源：1](全角冒号)
const CITATION_RE = /\[来源[:：]\s*(\d+)\]/g
// 这些元素内的文本不参与替换:<a> 的标签文字是锚文本(保持原样),
// <code>/<pre> 是代码,替换会改变代码语义
const SKIP_TAGS = new Set(['A', 'CODE', 'PRE'])
const FILTER_ACCEPT = 1 // NodeFilter.FILTER_ACCEPT
const FILTER_REJECT = 2 // NodeFilter.FILTER_REJECT
const SHOW_TEXT = 4 // NodeFilter.SHOW_TEXT

/** 渲染联网搜索总结回答:
 *  1) mdRender 把 Markdown 渲染成 html;
 *  2) DOMParser 解析后用 TreeWalker 只遍历文本节点,把 [来源:N] 换成
 *     <span class="md-ref-web" data-source-idx="N">N</span>(属性/锚文本/
 *     代码块不动;编号越界或对应来源 URL 不合法时保留原文);
 *  3) DOMPurify.sanitize 收口。
 *  返回 { html, cited },cited 是实际生效的来源编号集合。 */
export function renderWebSearchAnswer(
  text: string,
  sources: WebSearchCitationSource[],
  mdRender: (raw: string) => string,
): RenderedWebSearchAnswer {
  const cited = new Set<number>()
  const doc = new DOMParser().parseFromString(mdRender(text || ''), 'text/html')
  const walker = doc.createTreeWalker(doc.body, SHOW_TEXT, {
    acceptNode: (node: Node) => {
      for (let p = node.parentElement; p; p = p.parentElement) {
        if (SKIP_TAGS.has(p.tagName)) return FILTER_REJECT
        if (p === doc.body) break
      }
      return FILTER_ACCEPT
    },
  })
  const textNodes: Text[] = []
  for (let n = walker.nextNode(); n; n = walker.nextNode()) textNodes.push(n as Text)
  for (const node of textNodes) {
    const value = node.nodeValue || ''
    const frag = doc.createDocumentFragment()
    let copied = 0
    let replaced = false
    for (const m of value.matchAll(CITATION_RE)) {
      const idx = Number(m[1])
      const source = sources[idx - 1]
      if (!source || safeWebSourceUrl(source.url) === undefined) continue
      replaced = true
      cited.add(idx)
      frag.append(value.slice(copied, m.index))
      const badge = doc.createElement('span')
      badge.className = 'md-ref-web'
      badge.setAttribute('data-source-idx', String(idx))
      badge.textContent = String(idx)
      frag.append(badge)
      copied = m.index + m[0].length
    }
    if (!replaced) continue
    frag.append(value.slice(copied))
    node.replaceWith(frag)
  }
  return {
    html: DOMPurify.sanitize(doc.body.innerHTML, {
      ADD_ATTR: ['class', 'data-source-idx'],
    }),
    cited,
  }
}
