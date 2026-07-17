import { defineStore } from 'pinia';
import { ref, computed } from 'vue';
import * as api from '../services/api';
export const useConversationStore = defineStore('conversation', () => {
    const conversations = ref([]);
    const currentConversation = ref(null);
    const messages = ref([]);
    const currentConfig = ref(null);
    const loading = ref(false);
    const streaming = ref(false);
    const stageMessage = ref('');
    const currentStage = ref(''); // e.g. "fetch_guide", "parse_fields_done"
    const isEmbedded = computed(() => {
        const params = new URLSearchParams(window.location.search);
        return params.get('embed') === 'true' || window.parent !== window;
    });
    async function loadConversations() {
        loading.value = true;
        try {
            conversations.value = await api.listConversations();
        }
        finally {
            loading.value = false;
        }
    }
    async function selectConversation(id) {
        loading.value = true;
        try {
            const conv = await api.getConversation(id);
            currentConversation.value = conv;
            messages.value = conv.messages || [];
            currentConfig.value = conv.currentConfig || null;
        }
        finally {
            loading.value = false;
        }
    }
    async function startNewConversation() {
        const conv = await api.createConversation();
        currentConversation.value = conv;
        messages.value = [];
        currentConfig.value = null;
        await loadConversations();
    }
    async function removeConversation(id) {
        await api.deleteConversation(id);
        if (currentConversation.value?.id === id) {
            currentConversation.value = null;
            messages.value = [];
            currentConfig.value = null;
        }
        await loadConversations();
    }
    async function sendMessage(text) {
        if (!text.trim() || streaming.value)
            return;
        // Auto-create conversation if none selected
        let convId = currentConversation.value?.id;
        if (!convId) {
            const conv = await api.createConversation();
            convId = conv.id;
            currentConversation.value = conv;
            await loadConversations();
        }
        // Add user message
        messages.value.push({ role: 'user', content: text });
        streaming.value = true;
        stageMessage.value = '正在处理...';
        currentStage.value = '';
        try {
            await api.generateConfig(text, convId || null, {
                onStage: (stage, msg) => {
                    stageMessage.value = msg;
                    currentStage.value = stage;
                },
                onResult: (result) => {
                    currentConfig.value = result.config;
                    messages.value.push({
                        role: 'assistant',
                        content: result.summary,
                        configSnapshot: result.config,
                    });
                    stageMessage.value = '';
                    currentStage.value = '';
                    loadConversations();
                },
                onError: (err) => {
                    messages.value.push({ role: 'assistant', content: `错误: ${err}` });
                    stageMessage.value = '';
                    currentStage.value = '';
                },
                onDone: () => {
                    stageMessage.value = '';
                    currentStage.value = '';
                },
            });
        }
        catch (e) {
            messages.value.push({ role: 'assistant', content: `请求失败: ${e.message}` });
        }
        finally {
            streaming.value = false;
            stageMessage.value = '';
            currentStage.value = '';
        }
    }
    async function sendModify(text) {
        if (!text.trim() || streaming.value || !currentConfig.value)
            return;
        const convId = currentConversation.value?.id;
        messages.value.push({ role: 'user', content: text });
        streaming.value = true;
        stageMessage.value = '正在修改...';
        currentStage.value = '';
        try {
            await api.modifyConfig(currentConfig.value, text, convId || null, {
                onStage: (stage, msg) => {
                    stageMessage.value = msg;
                    currentStage.value = stage;
                },
                onResult: (result) => {
                    currentConfig.value = result.config;
                    messages.value.push({
                        role: 'assistant',
                        content: result.summary,
                        configSnapshot: result.config,
                    });
                    stageMessage.value = '';
                    currentStage.value = '';
                    loadConversations();
                },
                onError: (err) => {
                    messages.value.push({ role: 'assistant', content: `错误: ${err}` });
                    stageMessage.value = '';
                    currentStage.value = '';
                },
                onDone: () => {
                    stageMessage.value = '';
                    currentStage.value = '';
                },
            });
        }
        catch (e) {
            messages.value.push({ role: 'assistant', content: `请求失败: ${e.message}` });
        }
        finally {
            streaming.value = false;
            stageMessage.value = '';
            currentStage.value = '';
        }
    }
    return {
        conversations,
        currentConversation,
        messages,
        currentConfig,
        loading,
        streaming,
        stageMessage,
        currentStage,
        isEmbedded,
        loadConversations,
        selectConversation,
        startNewConversation,
        removeConversation,
        sendMessage,
        sendModify,
    };
});
//# sourceMappingURL=conversation.js.map