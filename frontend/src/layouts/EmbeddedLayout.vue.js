/// <reference types="../../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { onMounted } from 'vue';
import { CloseOutlined } from '@ant-design/icons-vue';
import { useConversationStore } from '../stores/conversation';
import { useEmbedBridge } from '../composables/useEmbedBridge';
import ChatPanel from '../components/chat/ChatPanel.vue';
const store = useConversationStore();
const { closeWindow } = useEmbedBridge();
onMounted(() => {
    // In embed mode, create a conversation automatically
    if (!store.currentConversation) {
        store.startNewConversation();
    }
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "embedded-layout" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "embedded-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "title" },
});
const __VLS_0 = {}.AButton;
/** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ 'onClick': {} },
    type: "text",
    size: "small",
}));
const __VLS_2 = __VLS_1({
    ...{ 'onClick': {} },
    type: "text",
    size: "small",
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
let __VLS_4;
let __VLS_5;
let __VLS_6;
const __VLS_7 = {
    onClick: (__VLS_ctx.closeWindow)
};
__VLS_3.slots.default;
const __VLS_8 = {}.CloseOutlined;
/** @type {[typeof __VLS_components.CloseOutlined, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(__VLS_8, new __VLS_8({}));
const __VLS_10 = __VLS_9({}, ...__VLS_functionalComponentArgsRest(__VLS_9));
var __VLS_3;
/** @type {[typeof ChatPanel, ]} */ ;
// @ts-ignore
const __VLS_12 = __VLS_asFunctionalComponent(ChatPanel, new ChatPanel({
    embedded: (true),
}));
const __VLS_13 = __VLS_12({
    embedded: (true),
}, ...__VLS_functionalComponentArgsRest(__VLS_12));
/** @type {__VLS_StyleScopedClasses['embedded-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['embedded-header']} */ ;
/** @type {__VLS_StyleScopedClasses['title']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            CloseOutlined: CloseOutlined,
            ChatPanel: ChatPanel,
            closeWindow: closeWindow,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
//# sourceMappingURL=EmbeddedLayout.vue.js.map