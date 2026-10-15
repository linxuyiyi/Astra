import test, { after, beforeEach } from 'node:test'
import assert from 'node:assert/strict'

import { requestJson } from '../src/api/client.ts'
import { ApiError, isUncertainWrite } from '../src/api/errors.ts'

const realFetch = globalThis.fetch

after(() => {
  globalThis.fetch = realFetch
})

beforeEach(() => {
  globalThis.fetch = realFetch
})

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

test('GET 网络失败：不是未确认写入', async () => {
  globalThis.fetch = async () => {
    throw new TypeError('network down')
  }
  await assert.rejects(requestJson('/api/employees'), (error: unknown) => {
    assert.ok(error instanceof ApiError)
    assert.equal(error.status, 0)
    assert.equal(error.uncertainWrite, false)
    assert.equal(isUncertainWrite(error), false)
    return true
  })
})

test('POST 网络失败：标记为未确认写入', async () => {
  globalThis.fetch = async () => {
    throw new TypeError('network down')
  }
  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, true)
      assert.equal(isUncertainWrite(error), true)
      return true
    },
  )
})

test('POST 读取响应体失败：标记为未确认写入', async () => {
  const failingText = {
    ok: true,
    status: 200,
    text: async () => {
      throw new Error('body stream broken')
    },
  } as unknown as Response
  globalThis.fetch = async () => failingText

  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, true)
      return true
    },
  )
})

test('无 employee_id 的代理 5xx：未确认写入且不暴露代理 HTML', async () => {
  globalThis.fetch = async () =>
    new Response('<html><body>502 Bad Gateway</body></html>', {
      status: 502,
      headers: { 'content-type': 'text/html' },
    })

  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, true)
      assert.equal(error.employeeId, null)
      assert.doesNotMatch(error.message, /<html>/)
      return true
    },
  )
})

test('POST 业务校验 400：可修改后重试，不是未确认写入', async () => {
  globalThis.fetch = async () =>
    jsonResponse(400, { detail: { errors: { name: '必填' } } })

  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, false)
      assert.equal(error.fieldErrors.name, '必填')
      return true
    },
  )
})

test('成功响应返回解析后的 body', async () => {
  globalThis.fetch = async () => jsonResponse(200, { total: 2 })
  const data = await requestJson<{ total: number }>('/api/employees')
  assert.equal(data.total, 2)
})

test('写请求 2xx 空响应体：未确认写入，绝不冒充成功', async () => {
  globalThis.fetch = async () => new Response('', { status: 201 })

  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, true)
      return true
    },
  )
})

test('写请求 2xx 非 JSON 响应：未确认写入', async () => {
  globalThis.fetch = async () =>
    new Response('created', {
      status: 201,
      headers: { 'content-type': 'text/plain' },
    })

  await assert.rejects(
    requestJson('/api/employees', { method: 'POST', body: '{}' }),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, true)
      return true
    },
  )
})

test('只读 GET 2xx 空响应体：明确读取失败而非未确认写入', async () => {
  globalThis.fetch = async () => new Response('', { status: 200 })

  await assert.rejects(
    requestJson('/api/employees'),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, false)
      assert.match(error.message, /无法解析/)
      return true
    },
  )
})

test('只读 GET 2xx 非 JSON 响应体：明确读取失败', async () => {
  globalThis.fetch = async () => new Response('<html>ok</html>', { status: 200 })

  await assert.rejects(
    requestJson('/api/resources'),
    (error: unknown) => {
      assert.ok(error instanceof ApiError)
      assert.equal(error.uncertainWrite, false)
      assert.doesNotMatch(error.message, /<html>/)
      return true
    },
  )
})
