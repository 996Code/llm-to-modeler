/**
 * Embed Bridge — postMessage communication with parent system.
 *
 * Headers forwarding is handled by forwardHeaders.ts (separate module).
 */

import { onMounted, onUnmounted } from 'vue'

export interface EmbedMessage {
  type: string
  payload: Record<string, any>
}

export function useEmbedBridge() {
  const listeners: Array<(e: MessageEvent) => void> = []

  function sendMessage(msg: EmbedMessage) {
    if (window.parent && window.parent !== window) {
      window.parent.postMessage(msg, '*')
    }
  }

  function onMessage(type: string, callback: (payload: any) => void) {
    const handler = (e: MessageEvent) => {
      if (e.data?.type === type) {
        callback(e.data.payload)
      }
    }
    window.addEventListener('message', handler)
    listeners.push(handler)
    return () => window.removeEventListener('message', handler)
  }

  function notifyConfigGenerated(config: any) {
    sendMessage({ type: 'MODELER_CONFIG_GENERATED', payload: { config } })
  }

  function applyConfig(config: any) {
    sendMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config } })
  }

  function closeWindow() {
    sendMessage({ type: 'MODELER_CLOSE', payload: {} })
  }

  function notifyReady() {
    sendMessage({ type: 'MODELER_READY', payload: {} })
  }

  onMounted(() => {
    notifyReady()
  })

  onUnmounted(() => {
    listeners.forEach((l) => window.removeEventListener('message', l))
  })

  return {
    sendMessage,
    onMessage,
    notifyConfigGenerated,
    applyConfig,
    closeWindow,
    notifyReady,
  }
}
