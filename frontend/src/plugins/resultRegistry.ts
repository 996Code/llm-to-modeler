import type { Component } from 'vue'

export interface ResultRenderer {
  component: Component
  canRender: (message: any) => boolean
}
const modules = import.meta.glob<{ default: ResultRenderer }>('./*/result.ts', { eager: true })
const renderers = Object.fromEntries(Object.entries(modules).map(([path, module]) => [path.split('/')[1], module.default]))

export function getResultRenderer(message: any): Component | undefined {
  if (message?.role !== 'assistant' || message.kind === 'error-diag' || message.needsClarification) return undefined
  const renderer = renderers[message?.dataResult?.pack]
  return renderer?.canRender(message) ? renderer.component : undefined
}

export function cardOnlyResult(message: any): boolean {
  return message?.dataResult?.resultDisplay === 'card' && !!getResultRenderer(message)
}
