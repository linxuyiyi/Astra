import assert from 'node:assert/strict'
import test from 'node:test'
import { workerDisplayName } from '../src/utils/workerName.ts'

test('Worker labels use their own name while several Workers share one employee name', () => {
  const workers = ['构建机 01', '测试机 02'].map((name, index) => ({
    id: `w${index}`, name: '数字员工甲', execution_environment: { name },
  }))
  assert.deepEqual(workers.map(workerDisplayName), ['构建机 01', '测试机 02'])
})

test('a missing Worker name uses its identity instead of a digital employee name', () => {
  assert.equal(workerDisplayName({ id: 'w1', execution_environment: null }), 'Worker · w1')
  assert.equal(workerDisplayName({ id: 'w2', execution_environment: { name: '  ' } }), 'Worker · w2')
  assert.equal(workerDisplayName(null), '未分配 Worker')
})
