// =============================================================================
// 嵌入数据权限测试页入口 —— 仅测试用途,不进产品导航。
// 产出 /ai-modeler/embed-test.html:选身份(userId+roles,来自 subjects 字典)
// → 预览各库 ACL 放行 → iframe 以 embed 模式加载对话窗(切换即重载)。
// =============================================================================
import { createApp } from 'vue'
import 'ant-design-vue/dist/reset.css'
import { installAdminAntd } from '../plugins/antdAdmin'
import EmbedTestApp from './EmbedTestApp.vue'

const app = createApp(EmbedTestApp)
installAdminAntd(app)
app.mount('#embed-test-app')
