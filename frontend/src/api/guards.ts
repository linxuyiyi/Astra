// 纯规则：启停可操作性、提交守卫。便于行为测试，UI 与 handler 共用同一规则。
import type { SyncState } from './types'

// 提交守卫：保存中或写入结果未确认时一律禁止再次提交。
export interface SubmitState {
  saving: boolean
  uncertain: boolean
}

export function canSubmit(state: SubmitState): boolean {
  return !state.saving && !state.uncertain
}

// 开关是否可交互。
// - ready：可启用/禁用；
// - creating/updating/incomplete：仅当前已启用者可直接禁用（其他编辑需先核对）；
// - deleting：不可启停。
export function isSwitchInteractive(
  state: SyncState,
  currentEnabled: boolean,
): boolean {
  if (state === 'deleting') return false
  if (state === 'ready') return true
  return currentEnabled
}

// 本次切换是否被后端 pending 规则接受（与 isSwitchInteractive 同源）。
export function canToggleEnabled(
  state: SyncState,
  currentEnabled: boolean,
  next: boolean,
): boolean {
  if (state === 'deleting') return false
  if (state === 'ready') return true
  // 未完成的创建/编辑：只允许对当前已启用员工禁用，不允许启用。
  return next === false && currentEnabled
}
