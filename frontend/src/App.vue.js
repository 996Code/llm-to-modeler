/// <reference types="../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { onMounted } from 'vue';
import { useConversationStore } from './stores/conversation';
import StandaloneLayout from './layouts/StandaloneLayout.vue';
import EmbeddedLayout from './layouts/EmbeddedLayout.vue';
const store = useConversationStore();
onMounted(() => {
    if (!store.isEmbedded) {
        store.loadConversations();
    }
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
if (!__VLS_ctx.store.isEmbedded) {
    /** @type {[typeof StandaloneLayout, ]} */ ;
    // @ts-ignore
    const __VLS_0 = __VLS_asFunctionalComponent(StandaloneLayout, new StandaloneLayout({}));
    const __VLS_1 = __VLS_0({}, ...__VLS_functionalComponentArgsRest(__VLS_0));
    var __VLS_3 = {};
    var __VLS_2;
}
else {
    /** @type {[typeof EmbeddedLayout, ]} */ ;
    // @ts-ignore
    const __VLS_4 = __VLS_asFunctionalComponent(EmbeddedLayout, new EmbeddedLayout({}));
    const __VLS_5 = __VLS_4({}, ...__VLS_functionalComponentArgsRest(__VLS_4));
    var __VLS_7 = {};
    var __VLS_6;
}
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            StandaloneLayout: StandaloneLayout,
            EmbeddedLayout: EmbeddedLayout,
            store: store,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
//# sourceMappingURL=App.vue.js.map