// =============================================================================
// 模块说明：应用入口文件（bootstrap）
// -----------------------------------------------------------------------------
// 类比 Java：这个文件相当于 Spring Boot 的 main 方法所在类（@SpringBootApplication
// 启动类），负责创建并启动整个前端应用。
//
// 职责：
//   1. 创建 Vue 应用实例（相当于 Spring ApplicationContext 的初始化）
//   2. 注册全局插件（Pinia 状态管理、Ant Design 组件库）
//   3. 将应用挂载到 HTML 中的 #app DOM 节点（相当于把 Bean 容器与外界连通）
// =============================================================================

// 从 vue 包导入 createApp 工厂方法（类似 Spring 的 SpringApplication.run()）
import { createApp } from 'vue'
// 从 pinia 导入状态管理插件工厂（Pinia ≈ 全局单例 Bean 容器 / Redux Store）
import { createPinia } from 'pinia'
// 导入 Ant Design Vue 组件库（类似 Java 的 UI 组件包，提供 Button/Table/Form 等）
import Antd from 'ant-design-vue'
// 导入 Ant Design 的全局样式重置表
import 'ant-design-vue/dist/reset.css'
// 导入根组件 App.vue（单文件组件 SFC，相当于 Java 的主控制器/根 Bean）
import App from './App.vue'

// 创建 Vue 应用实例，传入根组件。
// 类比：SpringApplication.run(App.class, args) —— 创建应用上下文
const app = createApp(App)

// 注册 Pinia 插件。注册后，所有组件都能用 useXxxStore() 访问同一份全局状态。
// 类比：启用 @EnableXxx 注解，让 @Service/@Repository 注解的 Bean 生效。
app.use(createPinia())

// 全量注册 Ant Design 组件（app.use(Antd) 会注册所有组件）。
// 生产环境通常按需引入以减小体积，这里为了开发便利全量引入。
app.use(Antd)

// 将应用挂载到 index.html 中 id="app" 的 DOM 元素。
// 这是前端应用真正"启动"的瞬间，类似 Spring 上下文 refresh 完成、Tomcat 开始监听端口。
app.mount('#app')
