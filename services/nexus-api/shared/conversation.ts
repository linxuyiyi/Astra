/** 聊天内活状态任务卡所需的轻量事实快照，来源于平台唯一事实源 /api/tasks。 */
export interface ConversationTaskSummary {
  id: string;
  title: string;
  status: string;
  waitingReason?: string | null;
  currentProgress?: string | null;
  workerId?: string | null;
  workerName?: string | null;
  workerOnline?: boolean | null;
  startedAt?: string | null;
  lastActivityAt?: string | null;
  finishedAt?: string | null;
  updatedAt?: string | null;
}

export interface ConversationTaskFeed {
  subscribe: (listener: () => void) => () => void;
  snapshot: () => Record<string, ConversationTaskSummary>;
}

export interface ConversationTaskActivity {
  kind: "tool" | "callback" | "system";
  taskId?: string;
  toolName?: string;
}

/** 数字员工页与对话区之间的边界。id 是 Nexus 私聊或群组 id。 */
export interface ConversationProps {
  kind: "private" | "group";
  id: string;
  viewerId: string;
  title: string;
  avatar?: string;
  enabled: boolean;
  taskFeed?: ConversationTaskFeed;
  onActivity?: (summary: string) => void;
  onMembersChanged?: () => void;
  onTaskActivity?: (activity: ConversationTaskActivity) => void;
}

export type MountConversation = (element: HTMLElement, props: ConversationProps) => () => void;
