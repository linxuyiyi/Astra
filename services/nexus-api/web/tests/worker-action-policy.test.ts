import assert from 'node:assert/strict'
import test from 'node:test'

import {
  actionBlockedByPolicy,
  hasOccupancy,
  isOccupancyBlocker,
} from '../src/features/workers/actionPolicy.ts'

function blocker(code: string, extra: Record<string, string> = {}) {
  return { code, message: code, ...extra }
}

function health(blocking: Array<ReturnType<typeof blocker>>) {
  return { blocking }
}

function policy(activeMaintenance = false, reconcileRequired = false) {
  return { activeMaintenance, reconcileRequired }
}

test('initial offline, unknown health and pending equipment do not block deploy', () => {
  const value = health([
    blocker('worker_offline'),
    blocker('runtime_unknown'),
    blocker('capability_not_applied'),
  ])
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), false)
  assert.equal(actionBlockedByPolicy('update', value, policy()), false)
  assert.equal(actionBlockedByPolicy('restart', value, policy()), false)
})

test('real occupancy blocks restart/deploy but not disable', () => {
  const value = health([blocker('occupied_task', { task_id: 'task-1' })])
  assert.equal(actionBlockedByPolicy('restart', value, policy()), true)
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), true)
  assert.equal(actionBlockedByPolicy('disable', value, policy()), false)
})

test('health abnormalities are not treated as occupancy', () => {
  assert.equal(isOccupancyBlocker(blocker('runtime_unavailable')), false)
  assert.equal(isOccupancyBlocker(blocker('capability_not_applied')), false)
  const value = health([blocker('runtime_unavailable'), blocker('worker_offline')])
  assert.equal(hasOccupancy(value), false)
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), false)
})

test('active maintenance blocks actions while interrupted keeps restore/check available', () => {
  const value = health([blocker('maintenance:running')])
  assert.equal(actionBlockedByPolicy('restart', value, policy(true, false)), true)
  assert.equal(actionBlockedByPolicy('check', value, policy(true, false)), false)
  assert.equal(actionBlockedByPolicy('restore', value, policy(false, true)), false)
  assert.equal(actionBlockedByPolicy('deploy', value, policy(false, true)), true)
})
