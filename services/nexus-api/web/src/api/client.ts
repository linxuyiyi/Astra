import { workerFacingMessage } from '@/utils/format'
import type {
  AstraEvent,
  Worker,
  Task,
  TaskCategory,
  TaskCategoryWrite,
  Metrics,
  TaskWorkspaceBinding,
  RelatedTask,
  ExperienceReviewJob,
  KnowledgeDetail,
  KnowledgePage,
  KnowledgeSpace,
  KnowledgeUsageRecord,
  LibraryPermissions,
  LibraryStats,
  LibraryServiceStatus,
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

async function fetchResponse(
  path: string,
  options?: RequestInit,
): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!response.ok) {
    const body = await response
      .json()
      .catch(() => ({ detail: response.statusText }))
    const detail = body.detail ?? '请求失败'
    const message = typeof detail === 'string' ? detail : '请求失败'
    const workerSurface =
      path.startsWith('/api/workers') || path.startsWith('/api/tasks')
    throw new ApiError(
      workerSurface ? workerFacingMessage(message) : message,
      response.status,
    )
  }
  return response
}

export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  return (await fetchResponse(path, options)).json() as Promise<T>
}

async function listPage<T>(path: string, query: URLSearchParams, signal?: AbortSignal) {
  const response = await fetchResponse(`${path}?${query}`, { signal })
  return { items: await response.json() as T[], total: Number(response.headers.get('X-Total-Count')) }
}

export const api = {
  metrics: () => request<Metrics>('/api/metrics'),
  categories: (includeDisabled = false) =>
    request<TaskCategory[]>(
      `/api/task-categories${includeDisabled ? '?include_disabled=true' : ''}`,
    ),
  createCategory: (body: TaskCategoryWrite) =>
    request<TaskCategory>('/api/task-categories', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateCategory: (id: string, body: TaskCategoryWrite) =>
    request<TaskCategory>(`/api/task-categories/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  workers: () => request<Worker[]>('/api/workers'),
  workerPage: (query: URLSearchParams, signal?: AbortSignal) => listPage<Worker>('/api/workers', query, signal),
  worker: (id: string, signal?: AbortSignal) =>
    request<Worker>(`/api/workers/${encodeURIComponent(id)}`, { signal }),
  saveWorker: (body: Record<string, unknown>, id?: string) => {
    const payload = { ...body }
    if (id) delete payload.id
    return request<Worker>(
      id ? `/api/workers/${encodeURIComponent(id)}` : '/api/workers',
      { method: id ? 'PUT' : 'POST', body: JSON.stringify(payload) },
    )
  },
  deleteWorker: (id: string) =>
    request<{ deleted: string }>(`/api/workers/${encodeURIComponent(id)}`, {
      method: 'DELETE',
    }),
  workerAction: (id: string, action: 'pause' | 'resume') =>
    request<Worker>(`/api/workers/${encodeURIComponent(id)}/${action}`, {
      method: 'POST',
    }),
  tasks: (query = '') => request<Task[]>(`/api/tasks${query}`),
  taskPage: (query: URLSearchParams, signal?: AbortSignal) => listPage<Task>('/api/tasks', query, signal),
  task: (id: string, signal?: AbortSignal) => request<Task>(`/api/tasks/${id}`, { signal }),
  taskWorkspace: (id: string, signal?: AbortSignal) =>
    request<TaskWorkspaceBinding>(`/api/tasks/${id}/workspace`, { signal }),
  experienceReviewJobs: (taskId: string, signal?: AbortSignal) =>
    request<ExperienceReviewJob[]>(
      `/api/library/review-jobs?task_id=${encodeURIComponent(taskId)}`, { signal },
    ),
  retryExperienceReview: (id: string) =>
    request<ExperienceReviewJob>(
      `/api/library/review-jobs/${encodeURIComponent(id)}/retry`,
      { method: 'POST' },
    ),
  taskRepo: (id: string, signal?: AbortSignal) =>
    request<{
      name: string
      branch: string
      commit: string
      dirty: boolean
      changes: number
    }>(`/api/tasks/${id}/repo`, { signal }),
  uploadTaskFile: async (id: string, file: File): Promise<{ path: string }> => {
    const response = await fetch(
      `${API_BASE}/api/tasks/${id}/files/${encodeURIComponent(file.name)}`,
      { method: 'POST', body: file },
    )
    const result = await response.json()
    if (!response.ok) throw new ApiError(result.detail, response.status)
    return result
  },
  events: (limit = 50) => request<AstraEvent[]>(`/api/events?limit=${limit}`),
  taskEvents: (id: string, query: URLSearchParams, signal?: AbortSignal) =>
    request<AstraEvent[]>(`/api/tasks/${id}/events?${query}`, { signal }),
  taskAction: (
    id: string,
    action: string,
    body: Record<string, unknown> = {},
  ) =>
    request<Task>(`/api/tasks/${id}/${action}`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  deleteTask: (id: string) =>
    request<{ deleted: string }>(`/api/tasks/${id}`, { method: 'DELETE' }),
  createTask: (body: Record<string, unknown>) =>
    request<Task>('/api/tasks', { method: 'POST', body: JSON.stringify(body) }),
  updateTask: (id: string, body: Record<string, unknown>) =>
    request<Task>(`/api/tasks/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(body),
    }),
  libraryPermissions: () =>
    request<LibraryPermissions>('/api/library/permissions'),
  libraryServiceStatus: () =>
    request<LibraryServiceStatus>('/api/library/service/status'),
  libraryResync: () =>
    request<{ pending_count: number }>('/api/library/service/resync', {
      method: 'POST',
    }),
  libraryStats: () => request<LibraryStats>('/api/library/stats'),
  librarySpaces: () => request<KnowledgeSpace[]>('/api/library/spaces'),
  createLibrarySpace: (body: Record<string, unknown>) =>
    request<KnowledgeSpace>('/api/library/spaces', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateLibrarySpace: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeSpace>(`/api/library/spaces/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  moveLibrarySpace: (id: string, targetParentId: string | null) =>
    request<KnowledgeSpace>(`/api/library/spaces/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ target_parent_id: targetParentId }),
    }),
  deleteLibrarySpace: (id: string) =>
    request<{ deleted: string }>(`/api/library/spaces/${id}`, {
      method: 'DELETE',
    }),
  libraryKnowledge: (query = '') =>
    request<KnowledgePage>(`/api/library/knowledge${query}`),
  libraryKnowledgeDetail: (id: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}`),
  createLibraryKnowledge: (body: Record<string, unknown>) =>
    request<KnowledgeDetail>('/api/library/knowledge', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  updateLibraryKnowledge: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  publishLibraryKnowledge: (id: string, body: Record<string, unknown>) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/publish`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  rejectLibraryKnowledge: (id: string, reason: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/reject`, {
      method: 'POST',
      body: JSON.stringify({ reason }),
    }),
  supplementLibraryKnowledge: (id: string, reason: string) =>
    request<KnowledgeDetail>(
      `/api/library/knowledge/${id}/request-supplement`,
      { method: 'POST', body: JSON.stringify({ reason }) },
    ),
  moveLibraryKnowledge: (id: string, spaceId: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/move`, {
      method: 'POST',
      body: JSON.stringify({ space_id: spaceId }),
    }),
  batchMoveLibraryKnowledge: (ids: string[], spaceId: string) =>
    request<{ moved: number }>('/api/library/knowledge/batch-move', {
      method: 'POST',
      body: JSON.stringify({ knowledge_ids: ids, space_id: spaceId }),
    }),
  batchArchiveLibraryKnowledge: (ids: string[]) =>
    request<{ archived: number }>('/api/library/knowledge/batch-archive', {
      method: 'POST',
      body: JSON.stringify({ knowledge_ids: ids }),
    }),
  batchPublishLibraryKnowledge: (
    ids: string[],
    spaceId: string,
    verificationStatus: string,
  ) =>
    request<{ published: number }>('/api/library/knowledge/batch-publish', {
      method: 'POST',
      body: JSON.stringify({
        knowledge_ids: ids,
        space_id: spaceId,
        verification_status: verificationStatus,
      }),
    }),
  archiveLibraryKnowledge: (id: string) =>
    request<KnowledgeDetail>(`/api/library/knowledge/${id}/archive`, {
      method: 'POST',
    }),
  deleteLibraryKnowledge: (id: string) =>
    request<{ deleted: string }>(`/api/library/knowledge/${id}`, {
      method: 'DELETE',
    }),
  batchDeleteLibraryKnowledge: (ids: string[]) =>
    Promise.all(
      ids.map((id) =>
        request<{ deleted: string }>(`/api/library/knowledge/${id}`, {
          method: 'DELETE',
        }),
      ),
    ),
  libraryKnowledgeUsages: (id: string, limit = 20) =>
    request<KnowledgeUsageRecord[]>(
      `/api/library/knowledge/${id}/usages?limit=${limit}`,
    ),
  favoriteLibraryKnowledge: (id: string) =>
    request<{ favorite: boolean }>(`/api/library/knowledge/${id}/favorite`, {
      method: 'POST',
    }),
  helpfulLibraryKnowledge: (id: string) =>
    request<{ created: boolean; helpful_count: number }>(
      `/api/library/knowledge/${id}/helpful`,
      { method: 'POST' },
    ),
  libraryRelatedTasks: (id: string) =>
    request<RelatedTask[]>(`/api/library/knowledge/${id}/related-tasks`),
}
