/**
 * 剪贴板写入：安全上下文用原生 API，http 非降级兜底。
 *
 * navigator.clipboard 仅在安全上下文（https 或 localhost）存在——生产
 * 经网关以 http://内网IP 访问时该 API 不存在，直接调用即抛错
 * （真实事故：线上「复制」按钮全部提示复制失败）。
 * 兜底用临时的 textarea + document.execCommand('copy')：老旧但在内网
 * http 场景下是唯一可用路径。
 */
export async function copyText(text: string): Promise<boolean> {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text)
      return true
    } catch {
      // 权限拒绝等异常 → 落到兜底路径
    }
  }
  const ta = document.createElement('textarea')
  ta.value = text
  // 移出视口避免页面滚动/闪烁；readOnly 防止 iOS 弹键盘
  ta.style.position = 'fixed'
  ta.style.top = '-9999px'
  ta.setAttribute('readonly', '')
  document.body.appendChild(ta)
  ta.focus()
  ta.select()
  try {
    return document.execCommand('copy')
  } finally {
    document.body.removeChild(ta)
  }
}
