import assert from 'node:assert/strict'
import test from 'node:test'
import { streamRecovery } from '../src/stream-recovery.ts'
const sse = (body?: ReadableStream<Uint8Array>) => new Response(body ?? new ReadableStream({ start(c) { c.close() } }), { headers: { 'content-type': 'text/event-stream' } })
test('EOF and transport errors cause one recovery on next successful stream', async () => {
  let calls = 0
  const track = streamRecovery(() => calls++)
  await track('/event', sse()).text()
  track('/other', sse())
  assert.equal(calls, 0)
  track('/event', sse())
  assert.equal(calls, 1)
  track('/event', sse())
  assert.equal(calls, 1)
  await assert.rejects(track('/event', sse(new ReadableStream({ start(c) { c.error(new Error('lost')) } }))).text())
  track('/event', sse())
  assert.equal(calls, 2)
})
test('intentional abort/cancellation and non-SSE responses do not recover', async () => {
  let calls = 0
  const track = streamRecovery(() => calls++)
  const abort = new AbortController()
  const response = track('/event', sse(), abort.signal)
  abort.abort()
  await response.text()
  track('/event', sse())
  assert.equal(calls, 0)
  const pending = track('/cancel', sse(new ReadableStream({})))
  await pending.body!.cancel()
  track('/cancel', sse())
  const json = new Response('{}', {headers:{'content-type':'application/json'}})
  assert.equal(track('/json', json), json)
  track('/json', sse())
  assert.equal(calls, 0)
})

test('an aborted reconnect does not consume the outstanding history recovery', async () => {
  let calls = 0
  const track = streamRecovery(() => calls++)
  await track('/event', sse()).text()
  const abort = new AbortController()
  abort.abort()
  track('/event', sse(), abort.signal)
  assert.equal(calls, 0)
  track('/event', sse())
  assert.equal(calls, 1)
})
