import type { App, Plugin } from 'vue'
import {
  Button,
  Drawer,
  Empty,
  Input,
  Layout,
  Modal,
  Segmented,
  Select,
  Spin,
  Tag,
} from 'ant-design-vue'

const components = [
  Button,
  Drawer,
  Empty,
  Input,
  Layout,
  Modal,
  Segmented,
  Select,
  Spin,
  Tag,
] as Plugin[]

export function installMainAntd(app: App): void {
  for (const component of components) {
    app.use(component)
  }
}
