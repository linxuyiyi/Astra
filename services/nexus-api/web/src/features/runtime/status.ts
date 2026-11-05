import taskStatus from '../../../../app/runtime/task_status.json'
export type TaskState = keyof typeof taskStatus
export type RuntimeState =
  | TaskState
  | 'WORKING'
  | 'IDLE'
  | 'OFFLINE'
  | 'PAUSED'
  | 'UNKNOWN'
export type RuntimeTone = 'info' | 'success' | 'warning' | 'danger' | 'neutral'
const workerStatus = {
  WORKING: { label: '工作中', tone: 'info' },
  IDLE: { label: '空闲', tone: 'neutral' },
  OFFLINE: { label: '离线', tone: 'neutral' },
  PAUSED: { label: '已暂停', tone: 'warning' },
  UNKNOWN: { label: '未知', tone: 'neutral' },
}
const metadata = { ...workerStatus, ...taskStatus }
export function isRuntimeState(
  value: string | null | undefined,
): value is RuntimeState {
  return !!value && Object.hasOwn(metadata, value)
}
export function runtimeState(value: string | null | undefined): RuntimeState {
  return isRuntimeState(value) ? value : 'UNKNOWN'
}
export function runtimeStatusLabel(
  state: string | null | undefined,
  backendLabel?: string | null,
): string {
  return backendLabel || metadata[runtimeState(state)].label
}
export function runtimeStatusTone(
  state: string | null | undefined,
): RuntimeTone {
  return metadata[runtimeState(state)].tone as RuntimeTone
}
