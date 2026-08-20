// =============================================================================
// 模块说明：身份存储（模块级单例）
// -----------------------------------------------------------------------------
// 类比 Java：private static volatile String userId（应用级唯一，可被替换赋值）。
//
// 职责：
//   在嵌入（iframe）模式下，存储宿主通过 INIT/AUTH_UPDATE 下发的用户标识。
//   X-User-Id 请求头只从这里取值，杜绝前端自填 admin/anonymous 的伪造口子
//   （独立模式仍允许 URL/localStorage 兜底，见 api.ts）。
// =============================================================================

let _userId = ''

/** 设置宿主下发的 userId（嵌入模式握手/刷新时调用） */
export function setUserId(userId: string): void {
  _userId = userId
}

/** 获取当前 userId；未设置返回空串 */
export function getUserId(): string {
  return _userId
}

/** 清除（销毁时调用） */
export function clearUserId(): void {
  _userId = ''
}
