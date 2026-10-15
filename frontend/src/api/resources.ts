import { requestJson } from './client'
import type { ResourceKind, ResourceKindResponse } from './types'

// 单类目录（含独立重试）：响应带 kind 与对应投影键
// （skill→skills、mcp→mcps、knowledge→knowledge）。
export function getResourceKind(
  kind: ResourceKind,
): Promise<ResourceKindResponse> {
  return requestJson<ResourceKindResponse>(`/api/resources/${kind}`)
}
