import type { App, Plugin } from 'vue'
import {
  Badge,
  Button,
  Drawer,
  Empty,
  Input,
  Layout,
  Modal,
  Popover,
  Segmented,
  Select,
  Spin,
  Switch,
  Tag,
} from 'ant-design-vue'

const components = [
  Badge,
  Button,
  Drawer,
  Empty,
  Input,
  Layout,
  Modal,
  Popover,
  Segmented,
  Select,
  Spin,
  Switch,
  Tag,
] as Plugin[]

export function installMainAntd(app: App): void {
  for (const component of components) {
    app.use(component)
  }
}
