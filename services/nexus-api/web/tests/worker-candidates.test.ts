import assert from 'node:assert/strict'
import test from 'node:test'
import { supportsTask } from '../src/components/task/workerCandidates.ts'

test('task type and employee scope determine candidates, without repository or category', () => {
  const bound = { enabled: true, task_types: ['ISSUE'], digital_employee_id: 'emp-a', category_ids: [], repository_id: 'other' }
  assert.equal(supportsTask(bound, 'ISSUE', 'emp-a'), true)
  assert.equal(supportsTask(bound, 'ISSUE', 'emp-b'), false)
  assert.equal(supportsTask(bound, 'ISSUE', null), true)
  assert.equal(supportsTask({ ...bound, digital_employee_id: null }, 'ISSUE', 'emp-b'), true)
  assert.equal(supportsTask(bound, 'REQUIREMENT', 'emp-a'), false)
  assert.equal(supportsTask({ ...bound, enabled: false }, 'ISSUE', 'emp-a'), false)
})
