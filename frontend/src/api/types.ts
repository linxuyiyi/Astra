// Nexus 后端公开投影的响应类型。
// 只声明实际读取的字段；不根据 OpenAPI 的空泛 schema 猜测响应结构。
// 员工对象来源：backend/nexus_backend/employees.py `_public`。
export type SyncState =
  | 'ready'
  | 'creating'
  | 'updating'
  | 'incomplete'
  | 'deleting'

export interface Employee {
  id: string
  agent_id: string | null
  name: string
  name_source: 'native' | 'local'
  domain: string
  responsibilities: string
  instructions: string
  avatar: string
  enabled: boolean
  knowledge: string[]
  skills: string[]
  tools: string[]
  native_available: boolean
  sync_state: SyncState
  created_at: string
  updated_at: string
}

export interface EmployeeListResponse {
  employees: Employee[]
  total: number
}

// 创建/编辑请求体：只提交实际提供的字段（PATCH 语义由后端 ensure）。
export interface EmployeeInput {
  name?: string
  domain?: string
  responsibilities?: string
  instructions?: string
  avatar?: string
  enabled?: boolean
  knowledge?: string[]
  skills?: string[]
  tools?: string[]
}

// 资源目录投影：backend/nexus_backend/resources.py。
export interface ResourceItem {
  id: string
  name: string
  description: string
  enabled: boolean
  selectable: boolean
  reason: string | null
}

export interface ResourceCatalog {
  available: boolean
  reason: string | null
  items: ResourceItem[]
}

// 员工字段名 → 目录投影键：knowledge → knowledge，skills → skills，tools → mcps。
export type CatalogField = 'skills' | 'mcps' | 'knowledge'

export type ResourceKind = 'skill' | 'mcp' | 'knowledge'

export type ResourceKindResponse = {
  kind: string
} & Partial<Record<CatalogField, ResourceCatalog>>

// reconcile 成功但释放了创建占位时的响应。
export interface ReconciledReleased {
  status: 'released'
  employee_id: string
  sync_state: string
}

export function isReleasedResult(
  value: Employee | ReconciledReleased,
): value is ReconciledReleased {
  return 'status' in value && value.status === 'released'
}
