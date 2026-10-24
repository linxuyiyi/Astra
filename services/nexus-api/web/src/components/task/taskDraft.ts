export interface TaskDraftSnapshot {
  title: string
  task_type: string
  category_id: string
  repository_id: string
  priority: string
  description: string
  criteria: string[]
  employee_id: string
  responsible_employee_id: string
  files: number
}

export function taskDraftSnapshot(draft: TaskDraftSnapshot): string {
  return JSON.stringify({
    title: draft.title,
    task_type: draft.task_type,
    category_id: draft.category_id,
    repository_id: draft.repository_id || '',
    priority: draft.priority,
    description: draft.description,
    criteria: draft.criteria,
    employee_id: draft.employee_id || '',
    responsible_employee_id: draft.responsible_employee_id || '',
    files: draft.files,
  })
}

/**
 * 创建任务不预选分类。分类列表晚于抽屉打开时也不要改用户还没选的值，
 * 否则空表单会被当成已修改。编辑已有任务时分类本来就有值。
 */
export function applyDefaultCategory(
  draft: TaskDraftSnapshot,
  categoryIds: string[],
  baseline: string,
): { draft: TaskDraftSnapshot; baseline: string } {
  void categoryIds
  return { draft, baseline: baseline || taskDraftSnapshot(draft) }
}
