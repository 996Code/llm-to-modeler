import pluginVue from 'eslint-plugin-vue'
import {
  defineConfigWithVueTs,
  vueTsConfigs,
} from '@vue/eslint-config-typescript'

export default defineConfigWithVueTs(
  {
    ignores: [
      'dist/**',
      'node_modules/**',
      // vue-tsc 当前会在 src 旁生成 JS；这些文件已被 Git 忽略，不是源码。
      'src/**/*.js',
    ],
  },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
  {
    rules: {
      // 后端 JSON、图表 option 和 pack 动态协议尚未收敛成静态 schema。
      '@typescript-eslint/no-explicit-any': 'off',
      // pack 页面按路由职责命名，单词名与目录结构共同表达完整语义。
      'vue/multi-word-component-names': 'off',
    },
  },
)
