/// <reference types="../../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { PlusOutlined, MessageOutlined, DeleteOutlined } from '@ant-design/icons-vue';
import { useConversationStore } from '../stores/conversation';
import ChatPanel from '../components/chat/ChatPanel.vue';
import JsonPanel from '../components/json/JsonPanel.vue';
const store = useConversationStore();
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['conv-item']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-item']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-item']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-del']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-del']} */ ;
/** @type {__VLS_StyleScopedClasses['standalone-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['json-area']} */ ;
/** @type {__VLS_StyleScopedClasses['sider']} */ ;
// CSS variable injection 
// CSS variable injection end 
const __VLS_0 = {}.ALayout;
/** @type {[typeof __VLS_components.ALayout, typeof __VLS_components.aLayout, typeof __VLS_components.ALayout, typeof __VLS_components.aLayout, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ class: "standalone-layout" },
}));
const __VLS_2 = __VLS_1({
    ...{ class: "standalone-layout" },
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
var __VLS_4 = {};
__VLS_3.slots.default;
const __VLS_5 = {}.ALayoutSider;
/** @type {[typeof __VLS_components.ALayoutSider, typeof __VLS_components.aLayoutSider, typeof __VLS_components.ALayoutSider, typeof __VLS_components.aLayoutSider, ]} */ ;
// @ts-ignore
const __VLS_6 = __VLS_asFunctionalComponent(__VLS_5, new __VLS_5({
    width: (220),
    theme: "light",
    ...{ class: "sider" },
}));
const __VLS_7 = __VLS_6({
    width: (220),
    theme: "light",
    ...{ class: "sider" },
}, ...__VLS_functionalComponentArgsRest(__VLS_6));
__VLS_8.slots.default;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "sider-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "logo" },
});
const __VLS_9 = {}.AButton;
/** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
// @ts-ignore
const __VLS_10 = __VLS_asFunctionalComponent(__VLS_9, new __VLS_9({
    ...{ 'onClick': {} },
    type: "primary",
    size: "small",
}));
const __VLS_11 = __VLS_10({
    ...{ 'onClick': {} },
    type: "primary",
    size: "small",
}, ...__VLS_functionalComponentArgsRest(__VLS_10));
let __VLS_13;
let __VLS_14;
let __VLS_15;
const __VLS_16 = {
    onClick: (__VLS_ctx.store.startNewConversation)
};
__VLS_12.slots.default;
{
    const { icon: __VLS_thisSlot } = __VLS_12.slots;
    const __VLS_17 = {}.PlusOutlined;
    /** @type {[typeof __VLS_components.PlusOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_18 = __VLS_asFunctionalComponent(__VLS_17, new __VLS_17({}));
    const __VLS_19 = __VLS_18({}, ...__VLS_functionalComponentArgsRest(__VLS_18));
}
var __VLS_12;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "conv-list" },
});
for (const [conv] of __VLS_getVForSourceType((__VLS_ctx.store.conversations))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ onClick: (...[$event]) => {
                __VLS_ctx.store.selectConversation(conv.id);
            } },
        key: (conv.id),
        ...{ class: "conv-item" },
        ...{ class: ({ active: conv.id === __VLS_ctx.store.currentConversation?.id }) },
    });
    const __VLS_21 = {}.MessageOutlined;
    /** @type {[typeof __VLS_components.MessageOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_22 = __VLS_asFunctionalComponent(__VLS_21, new __VLS_21({
        ...{ class: "conv-icon" },
    }));
    const __VLS_23 = __VLS_22({
        ...{ class: "conv-icon" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_22));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "conv-title" },
    });
    (conv.title);
    const __VLS_25 = {}.DeleteOutlined;
    /** @type {[typeof __VLS_components.DeleteOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_26 = __VLS_asFunctionalComponent(__VLS_25, new __VLS_25({
        ...{ 'onClick': {} },
        ...{ class: "conv-del" },
    }));
    const __VLS_27 = __VLS_26({
        ...{ 'onClick': {} },
        ...{ class: "conv-del" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_26));
    let __VLS_29;
    let __VLS_30;
    let __VLS_31;
    const __VLS_32 = {
        onClick: (...[$event]) => {
            __VLS_ctx.store.removeConversation(conv.id);
        }
    };
    var __VLS_28;
}
if (!__VLS_ctx.store.conversations.length) {
    const __VLS_33 = {}.AEmpty;
    /** @type {[typeof __VLS_components.AEmpty, typeof __VLS_components.aEmpty, ]} */ ;
    // @ts-ignore
    const __VLS_34 = __VLS_asFunctionalComponent(__VLS_33, new __VLS_33({
        description: "暂无会话",
    }));
    const __VLS_35 = __VLS_34({
        description: "暂无会话",
    }, ...__VLS_functionalComponentArgsRest(__VLS_34));
}
var __VLS_8;
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "main-area" },
});
/** @type {[typeof ChatPanel, ]} */ ;
// @ts-ignore
const __VLS_37 = __VLS_asFunctionalComponent(ChatPanel, new ChatPanel({}));
const __VLS_38 = __VLS_37({}, ...__VLS_functionalComponentArgsRest(__VLS_37));
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "json-area" },
});
/** @type {[typeof JsonPanel, ]} */ ;
// @ts-ignore
const __VLS_40 = __VLS_asFunctionalComponent(JsonPanel, new JsonPanel({}));
const __VLS_41 = __VLS_40({}, ...__VLS_functionalComponentArgsRest(__VLS_40));
var __VLS_3;
/** @type {__VLS_StyleScopedClasses['standalone-layout']} */ ;
/** @type {__VLS_StyleScopedClasses['sider']} */ ;
/** @type {__VLS_StyleScopedClasses['sider-header']} */ ;
/** @type {__VLS_StyleScopedClasses['logo']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-list']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-item']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-title']} */ ;
/** @type {__VLS_StyleScopedClasses['conv-del']} */ ;
/** @type {__VLS_StyleScopedClasses['main-area']} */ ;
/** @type {__VLS_StyleScopedClasses['json-area']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            PlusOutlined: PlusOutlined,
            MessageOutlined: MessageOutlined,
            DeleteOutlined: DeleteOutlined,
            ChatPanel: ChatPanel,
            JsonPanel: JsonPanel,
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
//# sourceMappingURL=StandaloneLayout.vue.js.map