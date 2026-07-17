/// <reference types="../../../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { ref } from 'vue';
import { SendOutlined } from '@ant-design/icons-vue';
const props = defineProps();
const emit = defineEmits();
const text = ref('');
function send() {
    if (!text.value.trim() || props.streaming)
        return;
    emit('send', text.value.trim());
    text.value = '';
}
function onEnter(e) {
    if (e.shiftKey)
        return; // allow newlines with Shift+Enter
    e.preventDefault();
    send();
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "chat-input" },
});
const __VLS_0 = {}.ATextarea;
/** @type {[typeof __VLS_components.ATextarea, typeof __VLS_components.aTextarea, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ 'onPressEnter': {} },
    value: (__VLS_ctx.text),
    placeholder: (__VLS_ctx.hasConfig ? '描述要修改的内容...' : '描述你需要的表单...'),
    autoSize: ({ minRows: 1, maxRows: 4 }),
    disabled: (__VLS_ctx.streaming),
    ...{ class: "input-box" },
}));
const __VLS_2 = __VLS_1({
    ...{ 'onPressEnter': {} },
    value: (__VLS_ctx.text),
    placeholder: (__VLS_ctx.hasConfig ? '描述要修改的内容...' : '描述你需要的表单...'),
    autoSize: ({ minRows: 1, maxRows: 4 }),
    disabled: (__VLS_ctx.streaming),
    ...{ class: "input-box" },
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
let __VLS_4;
let __VLS_5;
let __VLS_6;
const __VLS_7 = {
    onPressEnter: (__VLS_ctx.onEnter)
};
var __VLS_3;
const __VLS_8 = {}.AButton;
/** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(__VLS_8, new __VLS_8({
    ...{ 'onClick': {} },
    type: "primary",
    loading: (__VLS_ctx.streaming),
    disabled: (!__VLS_ctx.text.trim()),
    ...{ class: "send-btn" },
}));
const __VLS_10 = __VLS_9({
    ...{ 'onClick': {} },
    type: "primary",
    loading: (__VLS_ctx.streaming),
    disabled: (!__VLS_ctx.text.trim()),
    ...{ class: "send-btn" },
}, ...__VLS_functionalComponentArgsRest(__VLS_9));
let __VLS_12;
let __VLS_13;
let __VLS_14;
const __VLS_15 = {
    onClick: (__VLS_ctx.send)
};
__VLS_11.slots.default;
if (!__VLS_ctx.streaming) {
    const __VLS_16 = {}.SendOutlined;
    /** @type {[typeof __VLS_components.SendOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_17 = __VLS_asFunctionalComponent(__VLS_16, new __VLS_16({}));
    const __VLS_18 = __VLS_17({}, ...__VLS_functionalComponentArgsRest(__VLS_17));
}
var __VLS_11;
/** @type {__VLS_StyleScopedClasses['chat-input']} */ ;
/** @type {__VLS_StyleScopedClasses['input-box']} */ ;
/** @type {__VLS_StyleScopedClasses['send-btn']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            SendOutlined: SendOutlined,
            text: text,
            send: send,
            onEnter: onEnter,
        };
    },
    __typeEmits: {},
    __typeProps: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeEmits: {},
    __typeProps: {},
});
; /* PartiallyEnd: #4569/main.vue */
//# sourceMappingURL=ChatInput.vue.js.map