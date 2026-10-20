import type { Employee, Task } from '@/api/types'
import { isWorkspaceAreaId, type WorkspaceAreaId } from './sceneLayout'

export interface TaskAreaRule {
  area: WorkspaceAreaId
  categoryIds?: string[]
  keywords?: RegExp
}

/** Business-to-space mapping stays configurable and independent from rendering. */
export const TASK_AREA_RULES: TaskAreaRule[] = [
  { area: 'meeting', keywords: /(会议|评审会|复盘会|汇报会)/u },
  { area: 'collaboration', keywords: /(协作|讨论|联合分析|方案评审)/u },
  { area: 'integration', categoryIds: ['automation', 'integration'], keywords: /(自动化|集成|接口|流水线|部署)/u },
  { area: 'special', categoryIds: ['defense-operation', 'incident'], keywords: /(专项|排查|修复|应急|故障|防线)/u },
]

export function taskWorkspaceArea(task: Task | undefined, employee: Employee): WorkspaceAreaId {
  if (task) {
    const text = `${task.title} ${task.current_progress}`
    const matched = TASK_AREA_RULES.find((rule) =>
      Boolean(task.category_id && rule.categoryIds?.includes(task.category_id)) || Boolean(rule.keywords?.test(text)),
    )
    if (matched) return matched.area
  }
  if (isWorkspaceAreaId(employee.default_work_area)) return employee.default_work_area
  return 'development'
}
