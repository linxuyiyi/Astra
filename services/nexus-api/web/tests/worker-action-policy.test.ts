import assert from 'node:assert/strict'
import test from 'node:test'

import {
  actionBlockedByPolicy,
  hasOccupancy,
  isOccupancyBlocker,
  occupiedTaskIds,
} from '../src/features/workers/actionPolicy.ts'

function blocker(code: string, extra: Record<string, string> = {}) {
  return { code, message: code, ...extra }
}

function health(blocking: Array<ReturnType<typeof blocker>>) {
  return { blocking }
}

function policy(activeMaintenance = false) {
  return { activeMaintenance }
}

test('current tasks follow authoritative occupancy references, including released-state edge cases', () => {
  assert.deepEqual(occupiedTaskIds(health([
    blocker('occupied_task', { task_id: 'failed-but-occupied' }),
    blocker('occupied_task', { task_id: 'canceled-but-unreleased' }),
    blocker('occupied_task', { task_id: 'failed-but-occupied' }),
    blocker('active_session', { session_id: 'session-1' }),
    blocker('equipment_pending'),
  ])), ['failed-but-occupied', 'canceled-but-unreleased'])
  assert.deepEqual(occupiedTaskIds(null), [])
  assert.deepEqual(occupiedTaskIds(health([])), [])
})

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

test('real occupancy blocks restart/deploy/update but permits checking', () => {
  const value = health([blocker('occupied_task', { task_id: 'task-1' })])
  assert.equal(actionBlockedByPolicy('restart', value, policy()), true)
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), true)
  assert.equal(actionBlockedByPolicy('check', value, policy()), false)
})

test('health abnormalities are not treated as occupancy', () => {
  assert.equal(isOccupancyBlocker(blocker('runtime_unavailable')), false)
  assert.equal(isOccupancyBlocker(blocker('capability_not_applied')), false)
  const value = health([blocker('runtime_unavailable'), blocker('worker_offline')])
  assert.equal(hasOccupancy(value), false)
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), false)
})

test('active maintenance blocks changes while check stays available', () => {
  const value = health([blocker('maintenance:running')])
  assert.equal(actionBlockedByPolicy('restart', value, policy(true)), true)
  assert.equal(actionBlockedByPolicy('check', value, policy(true)), false)
  assert.equal(actionBlockedByPolicy('update', value, policy(true)), true)
  assert.equal(actionBlockedByPolicy('deploy', value, policy()), false)
})
