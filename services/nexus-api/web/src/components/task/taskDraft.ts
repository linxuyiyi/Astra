export interface TaskDraftSnapshot {
  title: string
  task_type: string
  category_id: string
  priority: string
  description: string
  criteria: string[]
  agent_worker_id: string
  origin_employee_id: string
  files: number
}

export function taskDraftSnapshot(draft: TaskDraftSnapshot): string {
  return JSON.stringify({
    title: draft.title,
    task_type: draft.task_type,
    category_id: draft.category_id,
    priority: draft.priority,
    description: draft.description,
    criteria: draft.criteria,
    agent_worker_id: draft.agent_worker_id || '',
    origin_employee_id: draft.origin_employee_id || '',
    files: draft.files,
  })
}
