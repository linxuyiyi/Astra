import assert from 'node:assert/strict'
import test from 'node:test'

import {
  insertMention,
  mentionCandidates,
  mentionTrigger,
} from '../src/features/chat/mentions.ts'

test('mentionTrigger starts at a word boundary and reads the typed query', () => {
  assert.deepEqual(mentionTrigger('请 @星', 4), { start: 2, query: '星' })
  assert.deepEqual(mentionTrigger('@', 1), { start: 0, query: '' })
  assert.deepEqual(mentionTrigger('@星河', 3), { start: 0, query: '星河' })
  assert.deepEqual(mentionTrigger('（@星河', 4), { start: 1, query: '星河' })
})

test('mentionTrigger ignores email addresses and closed whitespace', () => {
  assert.equal(mentionTrigger('mail@example.com', 16), null)
  assert.equal(mentionTrigger('请 @星河 看一下', 8), null)
  assert.equal(mentionTrigger('没有 at 符号', 6), null)
})

test('mentionCandidates filters, dedupes and prefers prefix matches', () => {
  const members = [
    { id: 'e1', name: '星河', avatar: 'a' },
    { id: 'e2', name: '小牛码', avatar: 'b' },
    { id: 'e3', name: '星河', avatar: 'a' },
    { id: 'e4', name: '测试星河', avatar: 'c' },
  ]
  assert.deepEqual(
    mentionCandidates(members, '星').map(item => item.id),
    ['e1', 'e4'],
  )
  assert.deepEqual(
    mentionCandidates(members, '码').map(item => item.id),
    ['e2'],
  )
  assert.equal(mentionCandidates(members, '').length, 3)
})

test('insertMention replaces the active token and preserves the suffix', () => {
  const text = '请 @星 一起看'
  const trigger = mentionTrigger(text, 4)
  assert.ok(trigger)
  assert.deepEqual(insertMention(text, trigger, '星河', 4), {
    text: '请 @星河 一起看',
    caret: 5,
  })
})
