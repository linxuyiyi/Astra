// 统一的错误提取：把 FastAPI 各种 detail 形态收敛成安全可读的错误信息，
// 绝不直接展示 "[object Object]"。纯函数，便于行为测试。
//
// 另区分“结果未确认的写入”（uncertainWrite）：写请求在网络失败、读响应失败
// 或 5xx（含代理 5xx）时，服务端可能已经写入成功。只读 GET 错误永不是
// uncertainWrite。可靠校验错误（400/409/422）也不是。

export interface ParsedError {
  status: number
  fieldErrors: Record<string, string>
  message: string | null
  employeeId: string | null
  syncState: string | null
  uncertainWrite: boolean
}

export interface ParseErrorOptions {
  // 该请求是否为可能产生副作用的写请求（POST/PATCH/PUT/DELETE）。
  mutating?: boolean
}

function isUncertainStatus(status: number): boolean {
  return status === 0 || status >= 500
}

export class ApiError extends Error {
  readonly status: number
  readonly fieldErrors: Record<string, string>
  readonly message: string
  readonly detailMessage: string | null
  readonly employeeId: string | null
  readonly syncState: string | null
  readonly uncertainWrite: boolean

  constructor(parsed: ParsedError) {
    const fallback =
      parsed.message ??
      (Object.keys(parsed.fieldErrors).length > 0
        ? '请求未通过校验，请检查表单字段。'
        : `请求失败（HTTP ${parsed.status || '网络错误'}）。`)
    super(fallback)
    this.name = 'ApiError'
    this.status = parsed.status
    this.fieldErrors = parsed.fieldErrors
    this.detailMessage = parsed.message
    this.message = fallback
    this.employeeId = parsed.employeeId
    this.syncState = parsed.syncState
    this.uncertainWrite = parsed.uncertainWrite
  }
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function asFieldErrors(value: unknown): Record<string, string> {
  const record = asRecord(value)
  if (!record) return {}
  const result: Record<string, string> = {}
  for (const [key, raw] of Object.entries(record)) {
    if (typeof raw === 'string' && raw) result[key] = raw
  }
  return result
}

// FastAPI 默认 422 校验错误 detail 为数组，形如
// [{ type, loc: ["body", "name"], msg, input }]。
function asValidationErrors(value: unknown): Record<string, string> {
  if (!Array.isArray(value)) return {}
  const result: Record<string, string> = {}
  for (const entry of value) {
    const record = asRecord(entry)
    if (!record) continue
    const loc = Array.isArray(record.loc) ? record.loc : []
    const field = [...loc]
      .reverse()
      .find((part): part is string => typeof part === 'string')
    if (!field) continue
    const msg = typeof record.msg === 'string' ? record.msg : '输入无效'
    result[field] = msg
  }
  return result
}

export function parseErrorDetail(
  status: number,
  body: unknown,
  options: ParseErrorOptions = {},
): ParsedError {
  const mutating = options.mutating === true
  const parsed: ParsedError = {
    status,
    fieldErrors: {},
    message: null,
    employeeId: null,
    syncState: null,
    uncertainWrite: mutating && isUncertainStatus(status),
  }

  if (typeof body === 'string') {
    parsed.message = body.trim() || null
    return parsed
  }

  const envelope = asRecord(body)
  const detail = envelope && 'detail' in envelope ? envelope.detail : body

  if (typeof detail === 'string') {
    parsed.message = detail.trim() || null
    return parsed
  }

  if (Array.isArray(detail)) {
    parsed.fieldErrors = asValidationErrors(detail)
    if (Object.keys(parsed.fieldErrors).length === 0) {
      parsed.message = '请求参数校验未通过。'
    }
    return parsed
  }

  const record = asRecord(detail)
  if (record) {
    parsed.fieldErrors = asFieldErrors(record.errors)
    if (typeof record.message === 'string' && record.message.trim()) {
      parsed.message = record.message.trim()
    }
    if (typeof record.employee_id === 'string') {
      parsed.employeeId = record.employee_id
    }
    if (typeof record.sync_state === 'string') {
      parsed.syncState = record.sync_state
    }
    return parsed
  }

  return parsed
}

export function fieldErrorsOf(error: unknown): Record<string, string> {
  return error instanceof ApiError ? error.fieldErrors : {}
}

// 面向用户的安全可读错误；优先服务端消息，其次字段错误，最后兜底文案。
export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.detailMessage) return error.detailMessage
    const entries = Object.values(error.fieldErrors)
    if (entries.length > 0) return entries.join('；')
    return error.message
  }
  if (error instanceof Error && error.message) return error.message
  return '发生未知错误，请重试。'
}

// 写请求结果未确认：可能已写入，禁止直接重复提交，应刷新列表/核对。
export function isUncertainWrite(error: unknown): error is ApiError {
  return error instanceof ApiError && error.uncertainWrite
}

// 可靠的校验/冲突错误（可修改后重试），与只读错误一样不属于未确认写入。
export function isRetryableValidationError(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false
  return error.status === 400 || error.status === 409 || error.status === 422
}
