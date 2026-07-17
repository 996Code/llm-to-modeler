import axios from 'axios';
const api = axios.create({
    baseURL: '/api',
    headers: { 'Content-Type': 'application/json' },
});
// Set user ID header from URL or localStorage
function getUserId() {
    const params = new URLSearchParams(window.location.search);
    return params.get('userId') || localStorage.getItem('userId') || 'anonymous';
}
api.interceptors.request.use((config) => {
    config.headers['X-User-Id'] = getUserId();
    return config;
});
// ── Conversations ──────────────────────────────────────────
export async function listConversations() {
    const { data } = await api.get('/conversations');
    return data;
}
export async function createConversation(title = '') {
    const { data } = await api.post('/conversations', { title });
    return data;
}
export async function getConversation(id) {
    const { data } = await api.get(`/conversations/${id}`);
    return data;
}
export async function deleteConversation(id) {
    await api.delete(`/conversations/${id}`);
}
/**
 * Generate form config via SSE stream.
 * Uses fetch + ReadableStream (not EventSource, because we need POST).
 */
export async function generateConfig(description, conversationId, callbacks) {
    await streamSSE('/api/config/generate', { description, conversation_id: conversationId }, callbacks);
}
export async function modifyConfig(currentConfig, instruction, conversationId, callbacks) {
    await streamSSE('/api/config/modify', {
        current_config: currentConfig,
        instruction,
        conversation_id: conversationId,
    }, callbacks);
}
async function streamSSE(url, body, callbacks) {
    const resp = await fetch(url, {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json',
            'X-User-Id': getUserId(),
        },
        body: JSON.stringify(body),
    });
    if (!resp.ok || !resp.body) {
        throw new Error(`HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done)
            break;
        buffer += decoder.decode(value, { stream: true });
        // SSE events are separated by \n\n
        const events = buffer.split('\n\n');
        buffer = events.pop() || ''; // keep incomplete chunk
        for (const eventStr of events) {
            if (!eventStr.trim())
                continue;
            const event = parseSSEEvent(eventStr);
            if (!event)
                continue;
            switch (event.type) {
                case 'stage':
                    callbacks.onStage?.(event.data.stage, event.data.message);
                    break;
                case 'result':
                    callbacks.onResult?.(event.data);
                    break;
                case 'error':
                    callbacks.onError?.(event.data.error);
                    break;
                case 'done':
                    callbacks.onDone?.();
                    break;
            }
        }
    }
}
function parseSSEEvent(raw) {
    let type = '';
    let dataStr = '';
    for (const line of raw.split('\n')) {
        if (line.startsWith('event:'))
            type = line.slice(6).trim();
        else if (line.startsWith('data:'))
            dataStr += line.slice(5).trim();
    }
    if (!type)
        return null;
    try {
        return { type, data: JSON.parse(dataStr) };
    }
    catch {
        return { type, data: {} };
    }
}
// ── Validate ───────────────────────────────────────────────
export async function validateConfig(config) {
    const { data } = await api.post('/config/validate', { config });
    return data;
}
//# sourceMappingURL=api.js.map