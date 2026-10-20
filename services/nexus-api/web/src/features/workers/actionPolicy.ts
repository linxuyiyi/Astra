import type { MaintenanceAction, WorkerBlocking, WorkerHealth } from '../../api/maintenance'

const OCCUPANCY_CODES = new Set(['occupied_task', 'active_session'])
const ACTIVE_MAINTENANCE_STATES = new Set(['QUEUED', 'PREFLIGHT', 'RUNNING', 'VERIFYING'])
const IDLE_ACTIONS = new Set(['deploy', 'update', 'restart'])

export type BlockingHealth = Pick<WorkerHealth, 'blocking'> | null | undefined

export interface ActionPolicyState {
  activeMaintenance: boolean
  reconcileRequired: boolean
}

export function isOccupancyBlocker(item: WorkerBlocking): boolean {
  return OCCUPANCY_CODES.has(item.code)
}

export function occupancyBlockers(health: BlockingHealth): WorkerBlocking[] {
  return (health?.blocking ?? []).filter(isOccupancyBlocker)
}

export function hasOccupancy(health: BlockingHealth): boolean {
  return occupancyBlockers(health).length > 0
}

export function isActiveMaintenanceState(state: string | null | undefined): boolean {
  return ACTIVE_MAINTENANCE_STATES.has(String(state ?? ''))
}

export function isInterruptedMaintenanceState(state: string | null | undefined): boolean {
  return String(state ?? '') === 'INTERRUPTED'
}

export function actionNeedsIdle(action: string | null | undefined): boolean {
  return IDLE_ACTIONS.has(String(action ?? ''))
}

export function actionBlockedByPolicy(
  action: MaintenanceAction | string,
  health: BlockingHealth,
  state: ActionPolicyState,
): boolean {
  if (action === 'check') return false
  if (state.activeMaintenance) return true
  if (state.reconcileRequired) return action !== 'restore'
  if (action === 'restore') return false
  if (actionNeedsIdle(action) && hasOccupancy(health)) return true
  return false
}
