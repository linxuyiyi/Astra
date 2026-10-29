import assert from 'node:assert/strict'
import test from 'node:test'

import { shortText } from '../src/features/chat/shortText.ts'

test('shortText maps network failures', () => {
  assert.equal(shortText(new Error('Failed to fetch')), '网络异常，请重试')
  assert.equal(shortText('timeout while reading'), '网络异常，请重试')
})

test('shortText maps interrupt and upstream', () => {
  assert.equal(shortText('interrupted by user'), '回复已中断')
  assert.equal(shortText('AgentScope 502 upstream'), '服务暂时不可用，请稍后重试')
})

test('shortText keeps brief Chinese copy', () => {
  assert.equal(shortText('附件格式不支持'), '附件格式不支持')
})

test('shortText falls back for long opaque text', () => {
  assert.equal(shortText('Error: at Object.<anonymous> (/tmp/x.js:1:1)'), '出了点问题，请重试')
})
