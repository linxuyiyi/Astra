import test from 'node:test'
import assert from 'node:assert/strict'

import {
  ApiError,
  errorMessage,
  isRetryableValidationError,
  isUncertainWrite,
  parseErrorDetail,
} from '../src/api/errors.ts'

test('解析字符串 detail', () => {
  const parsed = parseErrorDetail(503, { detail: '服务未就绪' })
  assert.equal(parsed.message, '服务未就绪')
  assert.deepEqual(parsed.fieldErrors, {})
})

test('解析业务逐字段错误 detail.errors', () => {
  const parsed = parseErrorDetail(409, {
    detail: { errors: { name: '员工名称已存在', domain: '领域已占用' } },
  })
  assert.deepEqual(parsed.fieldErrors, {
    name: '员工名称已存在',
    domain: '领域已占用',
  })
})

test('解析 FastAPI 默认校验数组', () => {
  const parsed = parseErrorDetail(422, {
    detail: [{ loc: ['body', 'name'], msg: 'Field required' }],
  })
  assert.equal(parsed.fieldErrors.name, 'Field required')
})

test('解析结果未知的 pending detail', () => {
  const parsed = parseErrorDetail(
    504,
    {
      detail: {
        message: '创建结果未确认',
        employee_id: 'emp_1',
        sync_state: 'creating',
      },
    },
    { mutating: true },
  )
  assert.equal(parsed.message, '创建结果未确认')
  assert.equal(parsed.employeeId, 'emp_1')
  assert.equal(parsed.syncState, 'creating')
  assert.equal(parsed.uncertainWrite, true)
})

test('只读 GET 的 5xx 不是未确认写入', () => {
  const parsed = parseErrorDetail(502, { detail: 'upstream down' })
  assert.equal(parsed.uncertainWrite, false)
})

test('写请求 5xx 标记为未确认写入', () => {
  const parsed = parseErrorDetail(500, null, { mutating: true })
  assert.equal(parsed.uncertainWrite, true)
})

test('写请求 400/409/422 是可修改后重试的可靠错误', () => {
  for (const status of [400, 409, 422]) {
    const error = new ApiError(parseErrorDetail(status, null, { mutating: true }))
    assert.equal(error.uncertainWrite, false)
    assert.equal(isRetryableValidationError(error), true)
  }
})

test('错误对象提供可读消息而非 [object Object]', () => {
  const error = new ApiError(parseErrorDetail(422, { detail: { errors: {} } }))
  assert.match(errorMessage(error), /请求失败/)
})

test('isUncertainWrite 只认 ApiError 的标记', () => {
  const error = new ApiError(
    parseErrorDetail(
      504,
      {
        detail: {
          message: '未确认',
          employee_id: 'emp_2',
          sync_state: 'creating',
        },
      },
      { mutating: true },
    ),
  )
  assert.equal(isUncertainWrite(error), true)
  assert.equal(
    isUncertainWrite(new ApiError(parseErrorDetail(409, null, { mutating: true }))),
    false,
  )
  assert.equal(isUncertainWrite(new Error('x')), false)
})
