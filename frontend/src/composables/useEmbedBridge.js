/**
 * Embed Bridge — postMessage communication with parent system.
 */
import { onMounted, onUnmounted } from 'vue';
export function useEmbedBridge() {
    const listeners = [];
    function sendMessage(msg) {
        if (window.parent && window.parent !== window) {
            window.parent.postMessage(msg, '*');
        }
    }
    function onMessage(type, callback) {
        const handler = (e) => {
            if (e.data?.type === type) {
                callback(e.data.payload);
            }
        };
        window.addEventListener('message', handler);
        listeners.push(handler);
        return () => window.removeEventListener('message', handler);
    }
    // Tell parent that config was generated
    function notifyConfigGenerated(config) {
        sendMessage({ type: 'MODELER_CONFIG_GENERATED', payload: { config } });
    }
    // Tell parent that user wants to apply config
    function applyConfig(config) {
        sendMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config } });
    }
    // Tell parent to close the window
    function closeWindow() {
        sendMessage({ type: 'MODELER_CLOSE', payload: {} });
    }
    // Notify ready
    function notifyReady() {
        sendMessage({ type: 'MODELER_READY', payload: {} });
    }
    onMounted(() => {
        notifyReady();
    });
    onUnmounted(() => {
        listeners.forEach((l) => window.removeEventListener('message', l));
    });
    return {
        sendMessage,
        onMessage,
        notifyConfigGenerated,
        applyConfig,
        closeWindow,
        notifyReady,
    };
}
//# sourceMappingURL=useEmbedBridge.js.map