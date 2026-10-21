import assert from 'node:assert/strict'
import test from 'node:test'

import { maintenanceSemanticTone, semanticTag, semanticTone } from '../src/features/workers/semantic.ts'

test('runtime info and neutral tones map to progress and neutral', () => {
  assert.equal(semanticTone('info'), 'progress')
  assert.equal(semanticTone('neutral'), 'neutral')
  assert.equal(semanticTone('warning'), 'attention')
  assert.equal(semanticTone('danger'), 'danger')
  assert.equal(semanticTone('success'), 'success')
})

test('semantic tones already in the target vocabulary pass through', () => {
  for (const tone of ['success', 'progress', 'attention', 'danger', 'neutral'] as const) {
    assert.equal(semanticTone(tone), tone)
  }
})

test('neutral renders with the shared gray class instead of info blue', () => {
  const neutral = semanticTag('neutral')
  assert.equal(neutral.type, 'info')
  assert.equal(neutral.className, 'astra-tag--neutral')
})

test('colored semantics keep Element Plus type colors and no override class', () => {
  assert.deepEqual(semanticTag('success'), { type: 'success', className: '' })
  assert.deepEqual(semanticTag('progress'), { type: 'info', className: '' })
  assert.deepEqual(semanticTag('attention'), { type: 'warning', className: '' })
  assert.deepEqual(semanticTag('danger'), { type: 'danger', className: '' })
})

test('records and probe logs agree on failure, success, cancellation and interruption colors', () => {
  for (const [state, expected] of [
    ['FAILED', 'danger'], ['SUCCEEDED', 'success'], ['CANCELED', 'neutral'],
    ['INTERRUPTED', 'attention'], ['RUNNING', 'progress'], ['UNKNOWN', 'neutral'],
  ] as const) {
    assert.equal(maintenanceSemanticTone(state), expected)
    assert.equal(maintenanceSemanticTone(state.toLowerCase()), expected)
  }
  assert.equal(maintenanceSemanticTone('passed'), 'success')
  assert.equal(maintenanceSemanticTone('success'), 'success')
  assert.equal(maintenanceSemanticTone('RUNNING', 'canceled'), 'neutral')
  assert.equal(maintenanceSemanticTone('RUNNING', 'interrupted'), 'attention')
  assert.equal(maintenanceSemanticTone('RUNNING', 'cancel_requested'), 'attention')
})
