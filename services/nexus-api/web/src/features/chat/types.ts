import type { Channel, Employee } from "@/types";

export type ChatTargetKind = "contact" | "group";

/**
 * 会话绑定：聊天岛只从这里取 agent/session 与任务查询条件。
 * 消息读取、订阅与发送都在 web/nexus-chat 里，不再经过适配器。
 */
export interface ChatAdapter {
  readonly key: string;
  readonly kind: ChatTargetKind;
  readonly title: string;
  readonly subtitle: string;
  readonly canSend: boolean;
  readonly conversationId: string | null;
  readonly sessionId: string | null;
  taskQuery(): Record<string, string>;
}

export interface ChatTarget {
  key: string;
  kind: ChatTargetKind;
  id: string;
  name: string;
  subtitle: string;
  avatar: string;
  enabled: boolean;
  searchText: string;
  createdAt: string;
  members?: Employee[];
  channel?: Channel;
  employee?: Employee;
}

export interface ChatSessionState {
  key: string;
  target: ChatTarget;
  adapter: ChatAdapter | null;
  status: "loading" | "ready" | "error";
  error: string;
  tasks: import("@/platform").PlatformTask[];
  tasksLoading: boolean;
  taskError: string;
  taskGeneration: number;
  tab: "tasks" | "materials" | "members";
}
