// =============================================================================
// 管理端入口 —— 与主聊天应用(src/main.ts)平行的独立入口。
// 经 vite 多页打包(build.rollupOptions.input.admin)产出 dist/admin.html,
// 访问路径 /ai-modeler/admin.html;数据走 /ai-modeler/api/admin/*(同源)。
// =============================================================================
import { createApp } from 'vue'
import Antd from 'ant-design-vue'
import 'ant-design-vue/dist/reset.css'
import AdminApp from './AdminApp.vue'

createApp(AdminApp).use(Antd).mount('#admin-app')
