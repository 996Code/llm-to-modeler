/// <reference types="../../../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { computed } from 'vue';
import { message } from 'ant-design-vue';
import { CopyOutlined, DownloadOutlined, FileTextOutlined } from '@ant-design/icons-vue';
import { useConversationStore } from '../../stores/conversation';
const store = useConversationStore();
const config = computed(() => store.currentConfig);
const formattedJson = computed(() => config.value ? JSON.stringify(config.value, null, 2) : '');
async function copy() {
    if (!config.value)
        return;
    await navigator.clipboard.writeText(JSON.stringify(config.value, null, 2));
    message.success('已复制到剪贴板');
}
function download() {
    if (!config.value)
        return;
    const blob = new Blob([JSON.stringify(config.value, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${config.value.formCode || 'form-config'}-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(url);
}
function applyToParent() {
    if (!config.value)
        return;
    window.parent.postMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config: config.value } }, '*');
    message.success('已发送到主系统');
}
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "json-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "panel-header" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
    ...{ class: "title" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "actions" },
});
const __VLS_0 = {}.AButton;
/** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
// @ts-ignore
const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
    ...{ 'onClick': {} },
    size: "small",
    disabled: (!__VLS_ctx.config),
}));
const __VLS_2 = __VLS_1({
    ...{ 'onClick': {} },
    size: "small",
    disabled: (!__VLS_ctx.config),
}, ...__VLS_functionalComponentArgsRest(__VLS_1));
let __VLS_4;
let __VLS_5;
let __VLS_6;
const __VLS_7 = {
    onClick: (__VLS_ctx.copy)
};
__VLS_3.slots.default;
const __VLS_8 = {}.CopyOutlined;
/** @type {[typeof __VLS_components.CopyOutlined, ]} */ ;
// @ts-ignore
const __VLS_9 = __VLS_asFunctionalComponent(__VLS_8, new __VLS_8({}));
const __VLS_10 = __VLS_9({}, ...__VLS_functionalComponentArgsRest(__VLS_9));
var __VLS_3;
const __VLS_12 = {}.AButton;
/** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
// @ts-ignore
const __VLS_13 = __VLS_asFunctionalComponent(__VLS_12, new __VLS_12({
    ...{ 'onClick': {} },
    size: "small",
    disabled: (!__VLS_ctx.config),
}));
const __VLS_14 = __VLS_13({
    ...{ 'onClick': {} },
    size: "small",
    disabled: (!__VLS_ctx.config),
}, ...__VLS_functionalComponentArgsRest(__VLS_13));
let __VLS_16;
let __VLS_17;
let __VLS_18;
const __VLS_19 = {
    onClick: (__VLS_ctx.download)
};
__VLS_15.slots.default;
const __VLS_20 = {}.DownloadOutlined;
/** @type {[typeof __VLS_components.DownloadOutlined, ]} */ ;
// @ts-ignore
const __VLS_21 = __VLS_asFunctionalComponent(__VLS_20, new __VLS_20({}));
const __VLS_22 = __VLS_21({}, ...__VLS_functionalComponentArgsRest(__VLS_21));
var __VLS_15;
if (__VLS_ctx.store.isEmbedded) {
    const __VLS_24 = {}.AButton;
    /** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
    // @ts-ignore
    const __VLS_25 = __VLS_asFunctionalComponent(__VLS_24, new __VLS_24({
        ...{ 'onClick': {} },
        size: "small",
        type: "primary",
        disabled: (!__VLS_ctx.config),
    }));
    const __VLS_26 = __VLS_25({
        ...{ 'onClick': {} },
        size: "small",
        type: "primary",
        disabled: (!__VLS_ctx.config),
    }, ...__VLS_functionalComponentArgsRest(__VLS_25));
    let __VLS_28;
    let __VLS_29;
    let __VLS_30;
    const __VLS_31 = {
        onClick: (__VLS_ctx.applyToParent)
    };
    __VLS_27.slots.default;
    var __VLS_27;
}
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "editor-container" },
});
if (!__VLS_ctx.config) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "empty" },
    });
    const __VLS_32 = {}.FileTextOutlined;
    /** @type {[typeof __VLS_components.FileTextOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_33 = __VLS_asFunctionalComponent(__VLS_32, new __VLS_32({
        ...{ class: "empty-icon" },
    }));
    const __VLS_34 = __VLS_33({
        ...{ class: "empty-icon" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_33));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({});
}
else {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.pre, __VLS_intrinsicElements.pre)({
        ...{ class: "json-view" },
    });
    (__VLS_ctx.formattedJson);
}
/** @type {__VLS_StyleScopedClasses['json-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['panel-header']} */ ;
/** @type {__VLS_StyleScopedClasses['title']} */ ;
/** @type {__VLS_StyleScopedClasses['actions']} */ ;
/** @type {__VLS_StyleScopedClasses['editor-container']} */ ;
/** @type {__VLS_StyleScopedClasses['empty']} */ ;
/** @type {__VLS_StyleScopedClasses['empty-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['json-view']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            CopyOutlined: CopyOutlined,
            DownloadOutlined: DownloadOutlined,
            FileTextOutlined: FileTextOutlined,
            store: store,
            config: config,
            formattedJson: formattedJson,
            copy: copy,
            download: download,
            applyToParent: applyToParent,
        };
    },
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
});
; /* PartiallyEnd: #4569/main.vue */
//# sourceMappingURL=JsonPanel.vue.js.map