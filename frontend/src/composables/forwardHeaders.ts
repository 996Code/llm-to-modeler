/**
 * Forwarded headers store — module-level singleton.
 *
 * Separated from useEmbedBridge.ts to avoid rollup build issues with
 * mixing Vue composition API imports and plain utility exports.
 */

let _forwardedHeaders: Record<string, string> = {}

export function getForwardedHeaders(): Record<string, string> {
  return { ..._forwardedHeaders }
}

export function setForwardedHeaders(headers: Record<string, string>): void {
  _forwardedHeaders = { ...headers }
}

export function clearForwardedHeaders(): void {
  _forwardedHeaders = {}
}
