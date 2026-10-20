import { workerFacingMessage } from '@/utils/format'
import type {
  AstraEvent,
  CodeRepository,
  DashboardActivity,
  DashboardAttention,
  DashboardEmployees,
  DashboardKnowledge,
  DashboardOverview,
  DashboardTasks,
  Employee,
  EmployeeUsage,
  ExecutionEnvironment,
  KnowledgeDetail,
  KnowledgePage,
  KnowledgeSpace,
  KnowledgeUsageRecord,
  LibraryPermissions,
  Metrics,
  LibraryStats,
  LibraryUsageSummary,
  LibraryServiceStatus,
  RelatedTask,
  Task,
  TaskReview,
  TaskCategory,
  TaskCategoryWrite,
  TaskUsage,
  TaskWorkspaceBinding,
} from './types'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message)
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }))
    const detail = body.detail ?? '请求失败'
    const message = typeof detail === 'string' ? detail : '请求失败'
    const workerSurface = path.startsWith('/api/agent-workers') || path.startsWith('/api/tasks')
    throw new ApiError(workerSurface ? workerFacingMessage(message) : message, response.status)
  }
  return response.json() as Promise<T>
}

export const api = {
  metrics: () => request<Metrics>('/api/system/metrics'),
  health: () => request<{ status: string; time: string }>('/api/health'),
  dashboardOverview: (signal?: AbortSignal) => request<DashboardOverview>('/api/dashboard/overview', { signal }),
  dashboardEmployees: (signal?: AbortSignal) => request<DashboardEmployees>('/api/dashboard/agent-workers', { signal }),
  dashboardAttention: (signal?: AbortSignal) => request<DashboardAttention>('/api/dashboard/attention', { signal }),
  dashboardTasks: () => request<DashboardTasks>('/api/dashboard/tasks'),
  dashboardActivity: (signal?: AbortSignal) => request<DashboardActivity>('/api/dashboard/activity', { signal }),
  dashboardKnowledge: () => request<DashboardKnowledge>('/api/dashboard/knowledge'),
  categories: (includeDisabled = false) => request<TaskCategory[]>(`/api/task-categories${includeDisabled ? '?include_disabled=true' : ''}`),
  createCategory: (body: TaskCategoryWrite) => request<TaskCategory>('/api/task-categories', { method: 'POST', body: JSON.stringify(body) }),
  updateCategory: (id: string, body: TaskCategoryWrite) => request<TaskCategory>(`/api/task-categories/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  employees: () => request<Employee[]>('/api/agent-workers'),
  employee: (id: string) => request<Employee>(`/api/agent-workers/${id}`),
  employeeUsage: (id: string, days = 7) => request<EmployeeUsage>(`/api/agent-workers/${id}/usage?days=${days}`),
  createEmployee: (body: Record<string, unknown>) =>
    request<Employee>('/api/agent-workers', { method: 'POST', body: JSON.stringify(body) }),
  updateEmployee: (id: string, body: Record<string, unknown>) =>
    request<Employee>(`/api/agent-workers/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteEmployee: (id: string) =>
    request<{ deleted: string }>(`/api/agent-workers/${id}`, { method: 'DELETE' }),
  updateEnvironment: (id: string, body: Record<string, unknown>) =>
    request<ExecutionEnvironment>(`/api/agent-workers/${id}/environment`, { method: 'PUT', body: JSON.stringify(body) }),
  repositories: () => request<CodeRepository[]>('/api/repositories'),
  createRepository: (body: { url: string; branch: string; token: string }) =>
    request<CodeRepository>('/api/repositories', { method: 'POST', body: JSON.stringify(body) }),
  tasks: (query = '') => request<Task[]>(`/api/tasks${query}`),
  task: (id: string) => request<Task>(`/api/tasks/${id}`),
  taskReview: (id: string) => request<TaskReview>(`/api/tasks/${encodeURIComponent(id)}/review`),
  retryTaskReview: (id: string) => request<TaskReview>(`/api/tasks/${encodeURIComponent(id)}/review/retry`, { method: 'POST' }),
  taskWorkspace: (id: string) => request<TaskWorkspaceBinding>(`/api/tasks/${id}/workspace`),
  taskRepo: (id: string) => request<{name: string; branch: string; commit: string; dirty: boolean; changes: number}>(`/api/tasks/${id}/repo`),
  uploadTaskFile: async (id: string, file: File): Promise<{path: string}> => {
    const response = await fetch(`${API_BASE}/api/tasks/${id}/files/${encodeURIComponent(file.name)}`, { method: 'POST', body: file })
    const result = await response.json()
    if (!response.ok) throw new ApiError(result.detail, response.status)
    return result
  },
  taskUsage: (id: string) => request<TaskUsage>(`/api/tasks/${id}/usage`),
  events: (limit = 50) => request<AstraEvent[]>(`/api/events?limit=${limit}`),
  taskEvents: (id: string) => request<AstraEvent[]>(`/api/tasks/${id}/events`),
  taskAction: (id: string, action: string, body: Record<string, unknown> = {}) =>
    request<Task>(`/api/tasks/${id}/${action}`, { method: 'POST', body: JSON.stringify(body) }),
  deleteTask: (id: string) =>
    request<{ deleted: string }>(`/api/tasks/${id}`, { method: 'DELETE' }),
  employeeAction: (id: string, action: 'pause' | 'resume') =>
    request<Employee>(`/api/agent-workers/${id}/${action}`, { method: 'POST' }),
  createTask: (body: Record<string, unknown>) =>
    request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify(body) }),
  updateTask: (id: string, body: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${encodeURIComponent(id)}`, { method: 'PATCH', body: JSON.stringify(body) }),
  libraryPermissions: () => request<LibraryPermissions>('/api/library/permissions'),
  libraryServiceStatus: () => request<LibraryServiceStatus>('/api/library/service/status'),
  libraryResync: () => request<{ pending_count: number }>('/api/library/service/resync', { method: 'POST' }),
  libraryStats: () => request<LibraryStats>('/api/library/stats'),
  librarySpaces: () => request<KnowledgeSpace[]>('/api/library/spaces'),
  createLibrarySpace: (body: Record<string, unknown>) =>
    request<KnowledgeSpace>('/api/library/spaces', { method: 'POST', body: JSON.stringify(body) }),
  updateLibrarySpace: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeSpace>(`/api/library/spaces/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  moveLibrarySpace: (id: string, targetParentId: string | null) =>
    request<KnowledgeSpace>(`/api/library/spaces/${id}/move`, { method: 'POST', body: JSON.stringify({ target_parent_id: targetParentId }) }),
  deleteLibrarySpace: (id: string) =>
    request<{ deleted: string }>(`/api/library/spaces/${id}`, { method: 'DELETE' }),
  libraryKnowledge: (query = '') => request<KnowledgePage>(`/api/library/knowledge${query}`),
  libraryKnowledgeDetail: (id: string) => request<KnowledgeDetail>(`/api/library/knowledge/${id}`),
  createLibraryKnowledge: (body: Record<string, unknown>) =>
    request<KnowledgeDetail>('/api/library/knowledge', { method: 'POST', body: JSON.stringify(body) }),
  updateLibraryKnowledge: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}`, { method: 'PUT', body: JSON.stringify(body) }),
  publishLibraryKnowledge: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/publish`, { method: 'POST', body: JSON.stringify(body) }),
  rejectLibraryKnowledge: (id: string, reason: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/reject`, { method: 'POST', body: JSON.stringify({ reason }) }),
  supplementLibraryKnowledge: (id: string, reason: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/request-supplement`, { method: 'POST', body: JSON.stringify({ reason }) }),
  moveLibraryKnowledge: (id: string, spaceId: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/move`, { method: 'POST', body: JSON.stringify({ space_id: spaceId }) }),
  batchMoveLibraryKnowledge: (ids: string[], spaceId: string) =>
    request<{ moved: number }>('/api/library/knowledge/batch-move', { method: 'POST', body: JSON.stringify({ knowledge_ids: ids, space_id: spaceId }) }),
  batchArchiveLibraryKnowledge: (ids: string[]) =>
    request<{ archived: number }>('/api/library/knowledge/batch-archive', { method: 'POST', body: JSON.stringify({ knowledge_ids: ids }) }),
  batchPublishLibraryKnowledge: (ids: string[], spaceId: string, verificationStatus: string) =>
    request<{ published: number }>('/api/library/knowledge/batch-publish', { method: 'POST', body: JSON.stringify({ knowledge_ids: ids, space_id: spaceId, verification_status: verificationStatus }) }),
  archiveLibraryKnowledge: (id: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/archive`, { method: 'POST' }),
  deleteLibraryKnowledge: (id: string) =>
    request<{ deleted: string }>(`/api/library/knowledge/${id}`, { method: 'DELETE' }),
  batchDeleteLibraryKnowledge: (ids: string[]) =>
    Promise.all(ids.map((id) => request<{ deleted: string }>(`/api/library/knowledge/${id}`, { method: 'DELETE' }))),
  libraryKnowledgeUsages: (id: string, limit = 20) =>
    request<KnowledgeUsageRecord[]>(`/api/library/knowledge/${id}/usages?limit=${limit}`),
  favoriteLibraryKnowledge: (id: string) =>
    request<{ favorite: boolean }>(`/api/library/knowledge/${id}/favorite`, { method: 'POST' }),
  helpfulLibraryKnowledge: (id: string) =>
    request<{ created: boolean; helpful_count: number }>(`/api/library/knowledge/${id}/helpful`, { method: 'POST' }),
  libraryRelatedTasks: (id: string) => request<RelatedTask[]>(`/api/library/knowledge/${id}/related-tasks`),
  libraryUsageSummary: (days = 7) => request<LibraryUsageSummary>(`/api/library/usage/summary?days=${days}`),
}
