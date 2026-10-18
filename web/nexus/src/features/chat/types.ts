import type { Attachment, ChatMessage, Channel, Employee } from "@/types";

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
  messages: ChatMessage[];
  liveLines: LiveLine[];
  notice: string;
  draft: string;
  draftFiles: Attachment[];
  uploading: boolean;
  sending: boolean;
  tasks: import("@/platform").PlatformTask[];
  tasksLoading: boolean;
  taskError: string;
  taskGeneration: number;
  generation: number;
  abort: AbortController | null;
  tab: "tasks" | "materials" | "memory" | "members";
}
