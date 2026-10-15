import test from 'node:test'
import assert from 'node:assert/strict'

import {
  canSubmit,
  canToggleEnabled,
  isSwitchInteractive,
} from '../src/api/guards.ts'

test('提交守卫：保存中或结果未确认时禁止再次提交', () => {
  assert.equal(canSubmit({ saving: false, uncertain: false }), true)
  assert.equal(canSubmit({ saving: true, uncertain: false }), false)
  assert.equal(canSubmit({ saving: false, uncertain: true }), false)
  assert.equal(canSubmit({ saving: true, uncertain: true }), false)
})

test('ready 员工可启用与禁用', () => {
  assert.equal(isSwitchInteractive('ready', true), true)
  assert.equal(isSwitchInteractive('ready', false), true)
  assert.equal(canToggleEnabled('ready', true, false), true)
  assert.equal(canToggleEnabled('ready', false, true), true)
})

test('pending 员工仅可对已启用者禁用，不可启用', () => {
  for (const state of ['creating', 'updating', 'incomplete'] as const) {
    assert.equal(isSwitchInteractive(state, true), true)
    assert.equal(isSwitchInteractive(state, false), false)
    assert.equal(canToggleEnabled(state, true, false), true)
    assert.equal(canToggleEnabled(state, false, true), false)
  }
})

test('deleting 员工不可启停', () => {
  assert.equal(isSwitchInteractive('deleting', true), false)
  assert.equal(canToggleEnabled('deleting', true, false), false)
  assert.equal(canToggleEnabled('deleting', false, false), false)
})
