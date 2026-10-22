type WorkerCandidate = {
  enabled: boolean
  task_types: readonly string[]
  digital_employee_id?: string | null
}

export function supportsTask(
  worker: WorkerCandidate,
  taskType: string,
  responsibleEmployeeId?: string | null,
): boolean {
  const owner = responsibleEmployeeId?.trim()
  const bound = worker.digital_employee_id?.trim()
  return worker.enabled && worker.task_types.includes(taskType)
    && (!owner || !bound || owner === bound)
}
