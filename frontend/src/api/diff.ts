// PATCH 只提交实际变更字段。纯函数，便于行为测试。
import type { Employee, EmployeeInput } from './types'

export interface EmployeeFormValues {
  name: string
  domain: string
  responsibilities: string
  instructions: string
  avatar: string
  enabled: boolean
  knowledge: string[]
  skills: string[]
  tools: string[]
}

export function sameStringList(a: readonly string[], b: readonly string[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false
  }
  return true
}

// 与已加载的真实配置比较，只返回变化字段。空数组表示明确清空（会被包含）。
export function computeEmployeeDiff(
  original: Employee,
  values: EmployeeFormValues,
): EmployeeInput {
  const patch: EmployeeInput = {}

  if (values.name !== original.name) patch.name = values.name
  if (values.domain !== original.domain) patch.domain = values.domain
  if (values.responsibilities !== original.responsibilities) {
    patch.responsibilities = values.responsibilities
  }
  if (values.instructions !== original.instructions) {
    patch.instructions = values.instructions
  }
  if (values.avatar !== original.avatar) patch.avatar = values.avatar
  if (values.enabled !== original.enabled) patch.enabled = values.enabled
  if (!sameStringList(values.knowledge, original.knowledge)) {
    patch.knowledge = [...values.knowledge]
  }
  if (!sameStringList(values.skills, original.skills)) {
    patch.skills = [...values.skills]
  }
  if (!sameStringList(values.tools, original.tools)) {
    patch.tools = [...values.tools]
  }

  return patch
}

export function hasChanges(patch: EmployeeInput): boolean {
  return Object.keys(patch).length > 0
}
