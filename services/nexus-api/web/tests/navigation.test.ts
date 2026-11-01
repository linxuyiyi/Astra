import assert from 'node:assert/strict'
import test from 'node:test'

import { getVisibleNavItems } from '../src/navigation.ts'

test('primary navigation puts schedules after digital employees', () => {
  const labels = getVisibleNavItems('/schedules').map(item => item.label)
  assert.deepEqual(labels.slice(0, 3), ['数字员工', '日程安排', '任务大厅'])
})

test('schedule route activates schedules and not settings', () => {
  const items = getVisibleNavItems('/schedules')
  assert.equal(items.find(item => item.label === '日程安排')?.active, true)
  assert.equal(items.find(item => item.label === '配置中心')?.active, false)
})
