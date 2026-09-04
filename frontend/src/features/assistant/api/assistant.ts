import type {
  ConversationResponse,
  ConversationCreate,
  MessageResponse,
  MessageCreate,
  OperationResponse
} from '../types/assistant'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...options?.headers },
    ...options,
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail ?? 'Request failed')
  }
  return response.json() as Promise<T>
}

export const AssistantAPI = {
  getConversations: () => request<ConversationResponse[]>('/api/assistant/conversations'),
  getConversation: (id: string) => request<ConversationResponse>(`/api/assistant/conversations/${id}`),
  createConversation: (data: ConversationCreate) => request<ConversationResponse>('/api/assistant/conversations', { method: 'POST', body: JSON.stringify(data) }),
  deleteConversation: (id: string) => request<void>(`/api/assistant/conversations/${id}`, { method: 'DELETE' }),

  getMessages: (conversationId: string) => request<MessageResponse[]>(`/api/assistant/conversations/${conversationId}/messages`),
  createMessage: (conversationId: string, data: MessageCreate) => request<MessageResponse>(`/api/assistant/conversations/${conversationId}/messages`, { method: 'POST', body: JSON.stringify(data) }),
  abortMessage: (conversationId: string) => request<void>(`/api/assistant/conversations/${conversationId}/messages/abort`, { method: 'POST' }),

  getOperation: (operationId: string) => request<OperationResponse>(`/api/assistant/operations/${operationId}`),
  confirmOperation: (operationId: string) => request<OperationResponse>(`/api/assistant/operations/${operationId}/confirm`, { method: 'POST' }),
  cancelOperation: (operationId: string) => request<OperationResponse>(`/api/assistant/operations/${operationId}/cancel`, { method: 'POST' }),
}
