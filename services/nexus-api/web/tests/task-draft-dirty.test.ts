import assert from 'node:assert/strict'
import test from 'node:test'

import { applyDefaultCategory, taskDraftSnapshot, type TaskDraftSnapshot } from '../src/components/task/taskDraft.ts'

function blank(): TaskDraftSnapshot {
  return {
    title: '',
    task_type: 'ISSUE',
    category_id: '',
    repository_id: '',
    priority: 'MEDIUM',
    description: '',
    criteria: [''],
    employee_id: '',
    responsible_employee_id: '',
    files: 0,
  }
}

test('opening the create drawer stays uncategorized and clean', () => {
  const opened = applyDefaultCategory(blank(), [], '')
  assert.equal(opened.draft.category_id, '')
  assert.equal(taskDraftSnapshot(opened.draft), opened.baseline)

  const settled = applyDefaultCategory(opened.draft, ['testing-platform'], opened.baseline)
  assert.equal(settled.draft.category_id, '')
  assert.equal(taskDraftSnapshot(settled.draft), settled.baseline)
})

test('an edit after the baseline stays dirty', () => {
  const settled = applyDefaultCategory(blank(), ['testing-platform'], '')
  const edited = { ...settled.draft, title: '修复登录' }
  assert.notEqual(taskDraftSnapshot(edited), settled.baseline)
})

test('empty and null employee ids compare as the same untouched value', () => {
  const settled = applyDefaultCategory(blank(), ['testing-platform'], '')
  const normalized = { ...settled.draft, employee_id: '' }
  const cleared = { ...settled.draft, employee_id: undefined as unknown as string }
  assert.equal(taskDraftSnapshot(normalized), taskDraftSnapshot(cleared))
  assert.equal(taskDraftSnapshot(cleared), settled.baseline)
})
