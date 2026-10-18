import assert from 'node:assert/strict'
import test from 'node:test'

import {
  mergeMessages,
  sameIds,
  selectionIsCurrent,
  sequenceIsCurrent,
  streamCursor,
} from '../src/features/chat/sessionState.ts'
import type { ChatMessage } from '../src/types.ts'

function message(id: string, text: string, second = 0): ChatMessage {
  return {
    id,
    sender: 'user',
    text,
    attachments: [],
    created_at: `2026-01-01T00:00:${String(second).padStart(2, '0')}Z`,
  }
}

test('mergeMessages dedupes by id and keeps an unconfirmed local message in time order', () => {
  const local = [message('m1', 'old', 1), message('local', 'sending', 3)]
  const incoming = [message('m1', 'updated', 1), message('m2', 'reply', 2)]
  const merged = mergeMessages(local, incoming)
  assert.deepEqual(merged.map((item) => item.id), ['m1', 'm2', 'local'])
  assert.equal(merged[0].text, 'updated')
  assert.equal(merged[2].text, 'sending')
})

test('a sent message is inserted after existing history, not before it', () => {
  const history = [message('m1', 'first', 1), message('m2', 'second', 2)]
  const sent = message('sent', 'my new message', 3)
  assert.deepEqual(mergeMessages(history, [sent]).map((item) => item.id), ['m1', 'm2', 'sent'])
})

test('mergeMessages does not duplicate the same server message arriving twice', () => {
  const once = mergeMessages([], [message('m1', 'reply', 1)])
  const twice = mergeMessages(once, [message('m1', 'reply', 1)])
  assert.equal(twice.length, 1)
})

test('streamCursor only accepts numeric replay cursors', () => {
  assert.equal(streamCursor('', '12-3'), '12-3')
  assert.equal(streamCursor('12-3', 'bad-id'), '12-3')
  assert.equal(streamCursor('12-3', undefined), '12-3')
})

test('selection and reload guards reject stale async work', () => {
  assert.equal(selectionIsCurrent('contact:a', 'contact:a', 4, 4, false), true)
  assert.equal(selectionIsCurrent('contact:b', 'contact:a', 4, 4, false), false)
  assert.equal(selectionIsCurrent('contact:a', 'contact:a', 3, 4, false), false)
  assert.equal(selectionIsCurrent('contact:a', 'contact:a', 4, 4, true), false)
  assert.equal(sequenceIsCurrent(2, 2, false), true)
  assert.equal(sequenceIsCurrent(1, 2, false), false)
  assert.equal(sequenceIsCurrent(2, 2, true), false)
  assert.equal(sameIds(['a', 'b'], ['a', 'b']), true)
  assert.equal(sameIds(['a'], ['a', 'b']), false)
})

test('replayed events are ignored by the per-member seen set', () => {
  const seen = new Set<string>()
  const applied: string[] = []
  for (const id of ['1-1', '1-2', '1-2', '1-3', '1-1']) {
    if (seen.has(id)) continue
    seen.add(id)
    applied.push(id)
  }
  assert.deepEqual(applied, ['1-1', '1-2', '1-3'])
})

test('a delayed timeline response cannot overwrite a newer one', () => {
  let latest = 0
  const writes: string[] = []
  const apply = (sequence: number, items: string[]) => {
    if (!sequenceIsCurrent(sequence, latest, false)) return
    writes.push(items.join(','))
  }
  latest = 2
  apply(1, ['old'])
  apply(2, ['new'])
  assert.deepEqual(writes, ['new'])
})
