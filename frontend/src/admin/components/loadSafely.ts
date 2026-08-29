// 各 Tab 共享的"安全加载"函数类型:AdminApp provide('loadSafely'),
// 统一处理 401(回登录页)与错误 toast,业务组件只写成功路径。
export interface LoadSafely {
  (fn: () => Promise<void>): Promise<void>
}
