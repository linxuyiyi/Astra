import test from 'node:test'
import assert from 'node:assert/strict'

import {
  computeEmployeeDiff,
  hasChanges,
  sameStringList,
} from '../src/api/diff.ts'
import type { Employee } from '../src/api/types.ts'
import type { EmployeeFormValues } from '../src/api/diff.ts'

function baseEmployee(): Employee {
  return {
    id: 'emp_1',
    agent_id: 'agent_1',
    name: '星河',
    name_source: 'native',
    domain: '测试自动化工程师',
    responsibilities: '负责自动化',
    instructions: '先判断',
    avatar: '👩🏻‍💻',
    enabled: true,
    knowledge: ['kb_1'],
    skills: ['sk_1', 'sk_2'],
    tools: ['mcp_1'],
    native_available: true,
    sync_state: 'ready',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}

function formFrom(employee: Employee): EmployeeFormValues {
  return {
    name: employee.name,
    domain: employee.domain,
    responsibilities: employee.responsibilities,
    instructions: employee.instructions,
    avatar: employee.avatar,
    enabled: employee.enabled,
    knowledge: [...employee.knowledge],
    skills: [...employee.skills],
    tools: [...employee.tools],
  }
}

test('相同表单不产生任何变更字段', () => {
  const employee = baseEmployee()
  const patch = computeEmployeeDiff(employee, formFrom(employee))
  assert.deepEqual(patch, {})
  assert.equal(hasChanges(patch), false)
})

test('只提交实际变化的字段', () => {
  const employee = baseEmployee()
  const values = formFrom(employee)
  values.domain = '新领域'
  values.enabled = false
  const patch = computeEmployeeDiff(employee, values)
  assert.deepEqual(patch, { domain: '新领域', enabled: false })
})

test('清空关联资源提交空数组', () => {
  const employee = baseEmployee()
  const values = formFrom(employee)
  values.skills = []
  const patch = computeEmployeeDiff(employee, values)
  assert.deepEqual(patch, { skills: [] })
})

test('不同顺序的关联数组视为变更', () => {
  assert.equal(sameStringList(['a', 'b'], ['b', 'a']), false)
  assert.equal(sameStringList(['a', 'b'], ['a', 'b']), true)
})
