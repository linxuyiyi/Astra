import type { MaintenanceAction, MaintenanceState } from '@/api/maintenance'

export const maintenanceActions: Record<MaintenanceAction, string> = {
  check: '检查',
  deploy: '一键部署',
  restart: '重启',
  update: '更新仓库',
}

export const maintenanceStates: Record<MaintenanceState, string> = {
  QUEUED: '排队中',
  RUNNING: '执行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELED: '已取消',
}

export function maintenanceActive(state: MaintenanceState) {
  return state === 'QUEUED' || state === 'RUNNING'
}
