import { requestJson } from './client'
import type {
  Employee,
  EmployeeInput,
  EmployeeListResponse,
  ReconciledReleased,
} from './types'

export function listEmployees(query?: string): Promise<EmployeeListResponse> {
  const q = query?.trim()
  const suffix = q ? `?q=${encodeURIComponent(q)}` : ''
  return requestJson<EmployeeListResponse>(`/api/employees${suffix}`)
}

export function getEmployee(employeeId: string): Promise<Employee> {
  return requestJson<Employee>(`/api/employees/${encodeURIComponent(employeeId)}`)
}

export function createEmployee(payload: EmployeeInput): Promise<Employee> {
  return requestJson<Employee>('/api/employees', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
}

export function updateEmployee(
  employeeId: string,
  payload: EmployeeInput,
): Promise<Employee> {
  return requestJson<Employee>(
    `/api/employees/${encodeURIComponent(employeeId)}`,
    { method: 'PATCH', body: JSON.stringify(payload) },
  )
}

// 结果未知的写入后手动核对；普通前端不发送 confirm_not_created，也不重启服务。
export function reconcileEmployee(
  employeeId: string,
): Promise<Employee | ReconciledReleased> {
  return requestJson<Employee | ReconciledReleased>(
    `/api/employees/${encodeURIComponent(employeeId)}/reconcile`,
    { method: 'POST', body: JSON.stringify({}) },
  )
}
