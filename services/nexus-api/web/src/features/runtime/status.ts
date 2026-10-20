import taskStatus from '../../../../app/runtime/task_status.json'
export type TaskState = keyof typeof taskStatus
export type RuntimeState = TaskState | 'WORKING' | 'WAIT_HUMAN' | 'IDLE' | 'OFFLINE' | 'PAUSED' | 'ERROR' | 'UNKNOWN'
export type RuntimeTone = 'info' | 'success' | 'warning' | 'danger' | 'neutral'
export type RuntimeStatusFilter = RuntimeState | 'ABNORMAL' | ''
export interface RuntimeStatusProjection { runtime_state: string; runtime_status_label: string; runtime_detail: string }
export interface RuntimeStatusMeta { state: RuntimeState; label: string; detail: string | null; tone: RuntimeTone; pulse: boolean }
const employeeStatus = {
  WORKING: {label: '工作中', tone: 'info'}, WAIT_HUMAN: {label: '等待人工', tone: 'warning'},
  IDLE: {label: '空闲', tone: 'neutral'}, OFFLINE: {label: '离线', tone: 'neutral'},
  PAUSED: {label: '已暂停', tone: 'warning'}, ERROR: {label: '异常', tone: 'danger'}, UNKNOWN: {label: '未知', tone: 'neutral'},
}
const metadata = {...employeeStatus, ...taskStatus}
export const TASK_RUNTIME_FILTER_STATES = Object.keys(taskStatus) as TaskState[]
export const TASK_ATTENTION_STATES = TASK_RUNTIME_FILTER_STATES.filter(state => taskStatus[state].group === 'attention')
export const RUNTIME_ACTIVE_STATES = new Set<RuntimeState>(['RUNNING', 'WAITING'])
export const RUNTIME_TERMINAL_STATES = new Set<RuntimeState>(['SUCCEEDED', 'FAILED', 'CANCELED'])
export const RUNTIME_FAILURE_STATES = new Set<RuntimeState>(['FAILED', 'ERROR'])
export const RUNTIME_ABNORMAL_STATES = new Set<RuntimeState>(['FAILED'])
export const RUNTIME_STATUS_RANK = Object.fromEntries(TASK_RUNTIME_FILTER_STATES.map(state => [state, taskStatus[state].rank]))
export function isRuntimeState(value: string | null | undefined): value is RuntimeState { return !!value && Object.hasOwn(metadata, value) }
export function runtimeState(value: string | null | undefined): RuntimeState { return isRuntimeState(value) ? value : 'UNKNOWN' }
export function runtimeStatusLabel(state: string | null | undefined, backendLabel?: string | null): string { return backendLabel || metadata[runtimeState(state)].label }
export function runtimeStatusTone(state: string | null | undefined): RuntimeTone { return metadata[runtimeState(state)].tone as RuntimeTone }
export function isActiveRuntimeState(state: string | null | undefined) { return RUNTIME_ACTIVE_STATES.has(runtimeState(state)) }
export function isTerminalRuntimeState(state: string | null | undefined) { return RUNTIME_TERMINAL_STATES.has(runtimeState(state)) }
export function isFailureRuntimeState(state: string | null | undefined) { return RUNTIME_FAILURE_STATES.has(runtimeState(state)) }
export function isAbnormalRuntimeState(state: string | null | undefined) { return RUNTIME_ABNORMAL_STATES.has(runtimeState(state)) }
export function runtimeStatusMatchesFilter(state: string | null | undefined, filter: RuntimeStatusFilter) { return !filter || (filter === 'ABNORMAL' ? isAbnormalRuntimeState(state) : state === filter) }
export function runtimeStatusMeta(projection: RuntimeStatusProjection): RuntimeStatusMeta {
  const state = runtimeState(projection.runtime_state)
  return {state, label: runtimeStatusLabel(state, projection.runtime_status_label), detail: projection.runtime_detail || null, tone: runtimeStatusTone(state), pulse: state === 'RUNNING'}
}
