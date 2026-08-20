/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 入站宿主 origin 白名单（逗号分隔），hostPort 双向 origin 校验用 */
  readonly VITE_HOST_ORIGINS?: string
  /** 其它 Vite 注入的环境变量可继续在此声明 */
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
// vite/client 已内置 import.meta.env.BASE_URL 类型，此处无需扩展
