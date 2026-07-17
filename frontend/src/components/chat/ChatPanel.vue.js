/// <reference types="../../../node_modules/.vue-global-types/vue_3.5_0_0_0.d.ts" />
import { ref, computed, watch, nextTick } from 'vue';
import { UserOutlined, RobotOutlined, LoadingOutlined, TableOutlined, CheckOutlined, } from '@ant-design/icons-vue';
import { useConversationStore } from '../../stores/conversation';
import ChatInput from './ChatInput.vue';
const __VLS_props = defineProps();
const store = useConversationStore();
const msgListRef = ref();
const examples = [
    '创建一个请假申请表，包含申请人、请假类型、日期',
    '创建一个员工信息表，包含姓名、部门、手机号',
    '创建一个联系人表单，包含姓名、邮箱、地址',
];
// Pipeline step tracking
const STAGES = [
    { key: 'fetch_guide', label: '获取指南' },
    { key: 'list_assets', label: '获取模板列表' },
    { key: 'parse_fields', label: '解析字段' },
    { key: 'fetch_templates', label: '获取模板' },
    { key: 'generate', label: '生成配置' },
    { key: 'validate', label: '校验' },
];
const pipelineSteps = computed(() => {
    const currentStage = store.currentStage;
    return STAGES.map((s, i) => {
        let status = 'pending';
        if (currentStage) {
            const currentIdx = STAGES.findIndex(x => currentStage.startsWith(x.key) || x.key.startsWith(currentStage));
            if (currentStage.startsWith(s.key) && !currentStage.includes('_done')) {
                status = 'active';
            }
            else if (currentStage.includes('_done') && currentStage.startsWith(s.key)) {
                status = 'done';
            }
            else if (currentIdx > i) {
                status = 'done';
            }
            else if (currentIdx === i && currentStage.includes('_done')) {
                status = 'done';
            }
        }
        // If streaming is done, mark validate as done
        if (!store.streaming && store.stageMessage === '' && store.currentConfig) {
            status = 'done';
        }
        return { ...s, index: i + 1, status };
    });
});
function quickFill(text) {
    store.sendMessage(text);
}
function handleSend(text) {
    if (store.currentConfig) {
        store.sendModify(text);
    }
    else {
        store.sendMessage(text);
    }
}
function selectConfig(config) {
    store.currentConfig = config;
}
function applyConfig(config) {
    window.parent.postMessage({ type: 'MODELER_CONFIG_APPLY', payload: { config } }, '*');
}
watch(() => store.messages.length, () => {
    nextTick(() => {
        if (msgListRef.value)
            msgListRef.value.scrollTop = msgListRef.value.scrollHeight;
    });
});
debugger; /* PartiallyEnd: #3632/scriptSetup.vue */
const __VLS_ctx = {};
let __VLS_components;
let __VLS_directives;
/** @type {__VLS_StyleScopedClasses['message-row']} */ ;
/** @type {__VLS_StyleScopedClasses['msg-avatar']} */ ;
/** @type {__VLS_StyleScopedClasses['message-row']} */ ;
/** @type {__VLS_StyleScopedClasses['msg-avatar']} */ ;
/** @type {__VLS_StyleScopedClasses['config-card']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['step-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['step-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['step-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['done']} */ ;
/** @type {__VLS_StyleScopedClasses['step-label']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['active']} */ ;
/** @type {__VLS_StyleScopedClasses['step-label']} */ ;
// CSS variable injection 
// CSS variable injection end 
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "chat-panel" },
});
__VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
    ...{ class: "message-list" },
    ref: "msgListRef",
});
/** @type {typeof __VLS_ctx.msgListRef} */ ;
if (!__VLS_ctx.store.messages.length && !__VLS_ctx.store.streaming) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "welcome" },
    });
    const __VLS_0 = {}.RobotOutlined;
    /** @type {[typeof __VLS_components.RobotOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_1 = __VLS_asFunctionalComponent(__VLS_0, new __VLS_0({
        ...{ class: "welcome-icon" },
    }));
    const __VLS_2 = __VLS_1({
        ...{ class: "welcome-icon" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_1));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.p, __VLS_intrinsicElements.p)({
        ...{ class: "welcome-text" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "examples" },
    });
    for (const [ex] of __VLS_getVForSourceType((__VLS_ctx.examples))) {
        const __VLS_4 = {}.ATag;
        /** @type {[typeof __VLS_components.ATag, typeof __VLS_components.aTag, typeof __VLS_components.ATag, typeof __VLS_components.aTag, ]} */ ;
        // @ts-ignore
        const __VLS_5 = __VLS_asFunctionalComponent(__VLS_4, new __VLS_4({
            ...{ 'onClick': {} },
            key: (ex),
            ...{ class: "example-tag" },
        }));
        const __VLS_6 = __VLS_5({
            ...{ 'onClick': {} },
            key: (ex),
            ...{ class: "example-tag" },
        }, ...__VLS_functionalComponentArgsRest(__VLS_5));
        let __VLS_8;
        let __VLS_9;
        let __VLS_10;
        const __VLS_11 = {
            onClick: (...[$event]) => {
                if (!(!__VLS_ctx.store.messages.length && !__VLS_ctx.store.streaming))
                    return;
                __VLS_ctx.quickFill(ex);
            }
        };
        __VLS_7.slots.default;
        (ex);
        var __VLS_7;
    }
}
for (const [msg, i] of __VLS_getVForSourceType((__VLS_ctx.store.messages))) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        key: (i),
        ...{ class: "message-row" },
        ...{ class: (msg.role) },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "msg-avatar" },
    });
    if (msg.role === 'user') {
        const __VLS_12 = {}.UserOutlined;
        /** @type {[typeof __VLS_components.UserOutlined, ]} */ ;
        // @ts-ignore
        const __VLS_13 = __VLS_asFunctionalComponent(__VLS_12, new __VLS_12({}));
        const __VLS_14 = __VLS_13({}, ...__VLS_functionalComponentArgsRest(__VLS_13));
    }
    else {
        const __VLS_16 = {}.RobotOutlined;
        /** @type {[typeof __VLS_components.RobotOutlined, ]} */ ;
        // @ts-ignore
        const __VLS_17 = __VLS_asFunctionalComponent(__VLS_16, new __VLS_16({}));
        const __VLS_18 = __VLS_17({}, ...__VLS_functionalComponentArgsRest(__VLS_17));
    }
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "msg-content" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "msg-text" },
    });
    (msg.content);
    if (msg.configSnapshot) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ onClick: (...[$event]) => {
                    if (!(msg.configSnapshot))
                        return;
                    __VLS_ctx.selectConfig(msg.configSnapshot);
                } },
            ...{ class: "config-card" },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "config-card-header" },
        });
        const __VLS_20 = {}.TableOutlined;
        /** @type {[typeof __VLS_components.TableOutlined, ]} */ ;
        // @ts-ignore
        const __VLS_21 = __VLS_asFunctionalComponent(__VLS_20, new __VLS_20({}));
        const __VLS_22 = __VLS_21({}, ...__VLS_functionalComponentArgsRest(__VLS_21));
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({});
        (msg.configSnapshot.formName || '表单配置');
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "config-card-body" },
        });
        (msg.configSnapshot.formFieldConfigVos?.length || 0);
        (msg.configSnapshot.formColumnsNumber);
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "config-card-actions" },
        });
        const __VLS_24 = {}.AButton;
        /** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
        // @ts-ignore
        const __VLS_25 = __VLS_asFunctionalComponent(__VLS_24, new __VLS_24({
            ...{ 'onClick': {} },
            size: "small",
        }));
        const __VLS_26 = __VLS_25({
            ...{ 'onClick': {} },
            size: "small",
        }, ...__VLS_functionalComponentArgsRest(__VLS_25));
        let __VLS_28;
        let __VLS_29;
        let __VLS_30;
        const __VLS_31 = {
            onClick: (...[$event]) => {
                if (!(msg.configSnapshot))
                    return;
                __VLS_ctx.selectConfig(msg.configSnapshot);
            }
        };
        __VLS_27.slots.default;
        var __VLS_27;
        if (__VLS_ctx.embedded) {
            const __VLS_32 = {}.AButton;
            /** @type {[typeof __VLS_components.AButton, typeof __VLS_components.aButton, typeof __VLS_components.AButton, typeof __VLS_components.aButton, ]} */ ;
            // @ts-ignore
            const __VLS_33 = __VLS_asFunctionalComponent(__VLS_32, new __VLS_32({
                ...{ 'onClick': {} },
                size: "small",
                type: "primary",
            }));
            const __VLS_34 = __VLS_33({
                ...{ 'onClick': {} },
                size: "small",
                type: "primary",
            }, ...__VLS_functionalComponentArgsRest(__VLS_33));
            let __VLS_36;
            let __VLS_37;
            let __VLS_38;
            const __VLS_39 = {
                onClick: (...[$event]) => {
                    if (!(msg.configSnapshot))
                        return;
                    if (!(__VLS_ctx.embedded))
                        return;
                    __VLS_ctx.applyConfig(msg.configSnapshot);
                }
            };
            __VLS_35.slots.default;
            var __VLS_35;
        }
    }
}
if (__VLS_ctx.store.streaming) {
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pipeline-progress" },
    });
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pipeline-header" },
    });
    const __VLS_40 = {}.LoadingOutlined;
    /** @type {[typeof __VLS_components.LoadingOutlined, ]} */ ;
    // @ts-ignore
    const __VLS_41 = __VLS_asFunctionalComponent(__VLS_40, new __VLS_40({
        ...{ class: "spin-icon" },
    }));
    const __VLS_42 = __VLS_41({
        ...{ class: "spin-icon" },
    }, ...__VLS_functionalComponentArgsRest(__VLS_41));
    __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
        ...{ class: "pipeline-title" },
    });
    (__VLS_ctx.store.stageMessage || '处理中...');
    __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
        ...{ class: "pipeline-steps" },
    });
    for (const [step] of __VLS_getVForSourceType((__VLS_ctx.pipelineSteps))) {
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            key: (step.key),
            ...{ class: "pipeline-step" },
            ...{ class: (step.status) },
        });
        __VLS_asFunctionalElement(__VLS_intrinsicElements.div, __VLS_intrinsicElements.div)({
            ...{ class: "step-icon" },
        });
        if (step.status === 'done') {
            const __VLS_44 = {}.CheckOutlined;
            /** @type {[typeof __VLS_components.CheckOutlined, ]} */ ;
            // @ts-ignore
            const __VLS_45 = __VLS_asFunctionalComponent(__VLS_44, new __VLS_44({}));
            const __VLS_46 = __VLS_45({}, ...__VLS_functionalComponentArgsRest(__VLS_45));
        }
        else if (step.status === 'active') {
            const __VLS_48 = {}.LoadingOutlined;
            /** @type {[typeof __VLS_components.LoadingOutlined, ]} */ ;
            // @ts-ignore
            const __VLS_49 = __VLS_asFunctionalComponent(__VLS_48, new __VLS_48({}));
            const __VLS_50 = __VLS_49({}, ...__VLS_functionalComponentArgsRest(__VLS_49));
        }
        else {
            __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
                ...{ class: "step-num" },
            });
            (step.index);
        }
        __VLS_asFunctionalElement(__VLS_intrinsicElements.span, __VLS_intrinsicElements.span)({
            ...{ class: "step-label" },
        });
        (step.label);
    }
}
/** @type {[typeof ChatInput, ]} */ ;
// @ts-ignore
const __VLS_52 = __VLS_asFunctionalComponent(ChatInput, new ChatInput({
    ...{ 'onSend': {} },
    streaming: (__VLS_ctx.store.streaming),
    hasConfig: (!!__VLS_ctx.store.currentConfig),
}));
const __VLS_53 = __VLS_52({
    ...{ 'onSend': {} },
    streaming: (__VLS_ctx.store.streaming),
    hasConfig: (!!__VLS_ctx.store.currentConfig),
}, ...__VLS_functionalComponentArgsRest(__VLS_52));
let __VLS_55;
let __VLS_56;
let __VLS_57;
const __VLS_58 = {
    onSend: (__VLS_ctx.handleSend)
};
var __VLS_54;
/** @type {__VLS_StyleScopedClasses['chat-panel']} */ ;
/** @type {__VLS_StyleScopedClasses['message-list']} */ ;
/** @type {__VLS_StyleScopedClasses['welcome']} */ ;
/** @type {__VLS_StyleScopedClasses['welcome-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['welcome-text']} */ ;
/** @type {__VLS_StyleScopedClasses['examples']} */ ;
/** @type {__VLS_StyleScopedClasses['example-tag']} */ ;
/** @type {__VLS_StyleScopedClasses['message-row']} */ ;
/** @type {__VLS_StyleScopedClasses['msg-avatar']} */ ;
/** @type {__VLS_StyleScopedClasses['msg-content']} */ ;
/** @type {__VLS_StyleScopedClasses['msg-text']} */ ;
/** @type {__VLS_StyleScopedClasses['config-card']} */ ;
/** @type {__VLS_StyleScopedClasses['config-card-header']} */ ;
/** @type {__VLS_StyleScopedClasses['config-card-body']} */ ;
/** @type {__VLS_StyleScopedClasses['config-card-actions']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-progress']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-header']} */ ;
/** @type {__VLS_StyleScopedClasses['spin-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-title']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-steps']} */ ;
/** @type {__VLS_StyleScopedClasses['pipeline-step']} */ ;
/** @type {__VLS_StyleScopedClasses['step-icon']} */ ;
/** @type {__VLS_StyleScopedClasses['step-num']} */ ;
/** @type {__VLS_StyleScopedClasses['step-label']} */ ;
var __VLS_dollars;
const __VLS_self = (await import('vue')).defineComponent({
    setup() {
        return {
            UserOutlined: UserOutlined,
            RobotOutlined: RobotOutlined,
            LoadingOutlined: LoadingOutlined,
            TableOutlined: TableOutlined,
            CheckOutlined: CheckOutlined,
            ChatInput: ChatInput,
            store: store,
            msgListRef: msgListRef,
            examples: examples,
            pipelineSteps: pipelineSteps,
            quickFill: quickFill,
            handleSend: handleSend,
            selectConfig: selectConfig,
            applyConfig: applyConfig,
        };
    },
    __typeProps: {},
});
export default (await import('vue')).defineComponent({
    setup() {
        return {};
    },
    __typeProps: {},
});
; /* PartiallyEnd: #4569/main.vue */
//# sourceMappingURL=ChatPanel.vue.js.map