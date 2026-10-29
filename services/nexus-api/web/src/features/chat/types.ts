import type { Attachment, Channel, ChatMessage, Employee } from "@/types";

export type ChatTargetKind = "contact" | "group";

export interface LiveTool {
  id: string;
  name: string;
  input: string;
  output: string;
  state: string;
}

export interface LiveLine {
  employeeId: string;
  name: string;
  text: string;
  tools: LiveTool[];
}

export type ChatAdapterEvent =
  | { type: "messages"; messages: ChatMessage[] }
  | { type: "live"; lines: LiveLine[] }
  | { type: "notice"; text: string }
  | { type: "finished" };

export interface TriggeredMember {
  employee_id: string;
  name: string;
  accepted: boolean;
  detail: string;
}

export interface ChatSendOutcome {
  message: ChatMessage;
  notice: string;
  triggered: TriggeredMember[];
}

/**
 * 会话绑定 + 读写：历史/发送/流式经 Nexus conversations（或群组）API，
 * 不直连 agentscope-api。
 */
export interface ChatAdapter {
  readonly key: string;
  readonly kind: ChatTargetKind;
  readonly title: string;
  readonly subtitle: string;
  readonly canSend: boolean;
  readonly conversationId: string | null;
  readonly sessionId: string | null;
  load(): Promise<ChatMessage[]>;
  send(text: string, attachmentIds: string[], signal: AbortSignal): Promise<ChatSendOutcome>;
  follow(signal: AbortSignal, emit: (event: ChatAdapterEvent) => void): Promise<void>;
  interrupt(): Promise<void>;
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

export type ReplyPhase = "idle" | "streaming" | "interrupting";

export interface ChatSessionState {
  key: string;
  target: ChatTarget;
  adapter: ChatAdapter | null;
  status: "loading" | "ready" | "error";
  error: string;
  messages: ChatMessage[];
  liveLines: LiveLine[];
  notice: string;
  /** 最近一次发送失败的原文+附件，供「重试」同 payload 再发。 */
  failedSend: { text: string; files: Attachment[] } | null;
  /** stop 失败后仍可点停止；重试条对此类 notice 不抢绑定会话。 */
  noticeSource: "send" | "stop" | "follow" | "attach" | "other" | "";
  draft: string;
  draftFiles: Attachment[];
  /** Local placeholders while uploadAttachment is in flight (island-style uploading chips). */
  draftPending: { id: string; name: string }[];
  uploading: boolean;
  sending: boolean;
  phase: ReplyPhase;
  lastSummary: string;
  unread: number;
  tasks: import("@/platform").PlatformTask[];
  tasksLoading: boolean;
  taskError: string;
  taskGeneration: number;
  generation: number;
  abort: AbortController | null;
  tab: "tasks" | "materials" | "members";
}
