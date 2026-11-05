import assert from 'node:assert/strict'
import test from 'node:test'

import { taskDraftSnapshot, type TaskDraftSnapshot } from '../src/components/task/taskDraft.ts'

function blank(): TaskDraftSnapshot {
  return {
    title: '',
    task_type: 'ISSUE',
    category_id: '',

    priority: 'MEDIUM',
    description: '',
    criteria: [''],
    agent_worker_id: '',
    origin_employee_id: '',
    files: 0,
  }
}

test('an edit after the baseline stays dirty', () => {
  const initial = blank()
  const edited = { ...initial, title: '修复登录' }
  assert.notEqual(taskDraftSnapshot(edited), taskDraftSnapshot(initial))
})

test('empty and null employee ids compare as the same untouched value', () => {
  const normalized = blank()
  const cleared = { ...normalized, agent_worker_id: undefined as unknown as string }
  assert.equal(taskDraftSnapshot(normalized), taskDraftSnapshot(cleared))
})
