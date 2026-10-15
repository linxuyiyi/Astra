import { ApiError, parseErrorDetail } from './errors.ts'

// 只有 Nexus 后端的同源 /api 请求；开发期由 Vite 代理到真实后端。
export interface RequestOptions extends RequestInit {
  // 显式声明该请求是否可能产生副作用；缺省按 HTTP 方法推断。
  mutating?: boolean
}

const WRITE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE'])

function isWriteRequest(init: RequestOptions): boolean {
  if (init.mutating !== undefined) return init.mutating
  return WRITE_METHODS.has((init.method ?? 'GET').toUpperCase())
}

function uncertainWriteError(
  mutating: boolean,
  message: string,
  status: number,
): ApiError {
  return new ApiError({
    status,
    fieldErrors: {},
    message,
    employeeId: null,
    syncState: null,
    uncertainWrite: mutating,
  })
}

export async function requestJson<T>(
  path: string,
  init: RequestOptions = {},
): Promise<T> {
  const mutating = isWriteRequest(init)
  const headers = new Headers(init.headers)
  if (init.body !== undefined && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }

  let response: Response
  try {
    response = await fetch(path, { ...init, headers })
  } catch {
    throw uncertainWriteError(
      mutating,
      mutating
        ? '无法确认写入是否到达后端（网络失败）。请刷新列表核对真实状态，不要直接重试。'
        : '无法连接 Nexus 后端，请确认服务已启动后重试。',
      0,
    )
  }

  let text: string
  try {
    text = await response.text()
  } catch {
    throw uncertainWriteError(
      mutating,
      mutating
        ? '写入请求已发出但读取响应失败，结果未确认。请刷新列表核对真实状态。'
        : '读取后端响应失败，请稍后重试。',
      response.status,
    )
  }

  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      // 非 JSON 响应（如代理错误页）：不把原文当作用户消息展示。
      body = null
    }
  }

  if (!response.ok) {
    throw new ApiError(parseErrorDetail(response.status, body, { mutating }))
  }

  // 正常 API 成功响应始终是非空 JSON 对象。空体、非 JSON 或非对象一律视为
  // 不可解析：写请求结果未确认（锁定保存），只读请求明确报读取失败；
  // 绝不返回 null 冒充成功。仅做最小顶层校验，不引入 schema 框架。
  if (typeof body !== 'object' || body === null) {
    throw uncertainWriteError(
      mutating,
      mutating
        ? '写入已发出但后端返回了无法解析的响应，结果未确认。请刷新列表核对真实状态，不要直接重试。'
        : '后端返回了无法解析的响应，读取失败，请稍后重试。',
      response.status,
    )
  }
  return body as T
}
