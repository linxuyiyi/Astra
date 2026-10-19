/**
 * 替换上游 `@/api`。
 *
 * 上游 `useMessages` 从 `sessionApi` 读历史、`sessionApi.streamEvents` 订阅、`chatApi.trigger`
 * 发送/确认。Nexus 侧先只实现历史读取：阶段 A 是只读预览，发送、停止、确认仍在
 * Vue 侧的既有链路上，不由聊天岛接管。
 *
 * 这里刻意保持与上游同签名，好让 `hooks/useMessages.ts` 逐字节不变。
 */
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import { createMsg } from "@agentscope-ai/agentscope/message";
import type { ContentBlock, Msg } from "@agentscope-ai/agentscope/message";

import { nexusGet } from "@/nexus/http";

export interface GitStatus {
  branch: string | null;
  head: string | null;
  ahead: number | null;
  behind: number | null;
  insertions: number;
  deletions: number;
  staged: number;
  unstaged: number;
  untracked: number;
}

export interface CredentialView {
  id: string;
  data: Record<string, unknown>;
}

export interface MessagesResponse {
  messages: Msg[];
  event_cursor: string | null;
  is_running: boolean;
  has_more: boolean;
}

interface NexusTool {
  id?: string;
  name?: string;
  input?: string;
  output?: string;
  state?: string;
}

interface NexusMessage {
  id: string;
  sender: string;
  speaker?: string;
  text: string;
  tools?: NexusTool[];
  created_at: string;
}

interface NexusConversation {
  id: string;
  employee_id: string;
  agentscope_session_id: string;
  event_cursor: string;
  messages: NexusMessage[];
}

let assistantName = "";

/** 助手消息的发送人显示名，由岛入口在拿到员工信息后设置。 */
export function configureAssistantName(name: string): void {
  assistantName = name;
}

function conversationId(): string {
  const value = new URLSearchParams(window.location.search).get("conversation") ?? "";
  if (!value) throw new Error("缺少 conversation 参数");
  return value;
}

/**
 * 把 Nexus 的扁平历史还原成原生 Msg。
 *
 * 这是**过渡实现**：Nexus 目前返回的是 `text + tools`，已经丢掉 thinking、图片和交互状态。
 * 阶段 B 会换成原生 Msg 历史，这个函数随之删除。
 */
function toMsg(message: NexusMessage, employeeName: string): Msg {
  const isUser = message.sender === "user";
  const createdAt = message.created_at || new Date().toISOString();
  const blocks: ContentBlock[] = [];
  if (message.text) {
    blocks.push({ type: "text", id: `${message.id}-text`, text: message.text, created_at: createdAt });
  }
  for (const tool of message.tools ?? []) {
    const id = tool.id || `${message.id}-tool`;
    blocks.push({
      type: "tool_call",
      id,
      name: tool.name || "tool",
      input: tool.input || "",
      state: "finished",
      created_at: createdAt,
    });
    if (tool.output) {
      blocks.push({
        type: "tool_result",
        id,
        name: tool.name || "tool",
        output: tool.output,
        state: tool.state === "error" ? "error" : "success",
        created_at: createdAt,
      });
    }
  }
  return createMsg({
    id: message.id,
    name: isUser ? "user" : employeeName,
    role: isUser ? "user" : "assistant",
    content: blocks,
    created_at: createdAt,
  });
}

export const sessionApi = {
  async messages(_sessionId: string, _agentId: string): Promise<MessagesResponse> {
    const payload = await nexusGet<NexusConversation>(
      `/api/v1/conversations/${conversationId()}`,
    );
    const employee = await nexusGet<{ items: Array<{ id: string; name: string }> }>(
      "/api/v1/employees",
    );
    const name =
      employee.items.find((item) => item.id === payload.employee_id)?.name ?? assistantName;
    configureAssistantName(name);
    return {
      messages: (payload.messages ?? []).map((item) => toMsg(item, name)),
      event_cursor: payload.event_cursor || null,
      // 阶段 A 不订阅事件流，因此不判断运行态；这个字段在阶段 B 由 Nexus 原生历史接口提供。
      is_running: false,
      has_more: false,
    };
  },

  /**
   * 阶段 A 不建立第二条 SSE 订阅：Vue 外壳仍在消费 `/stream`，聊天岛再连一条会重复接收。
   * 保持挂起直到组件卸载（controller.abort），语义上等价于「没有新事件」。
   */
  async *streamEvents(
    _sessionId: string,
    _agentId: string,
    signal?: AbortSignal,
    _after?: string | null,
  ): AsyncGenerator<AgentEvent> {
    if (!signal) return;
    await new Promise<void>((resolve) => {
      if (signal.aborted) resolve();
      else signal.addEventListener("abort", () => resolve(), { once: true });
    });
  },

  async interrupt(_sessionId?: string, _agentId?: string): Promise<void> {
    throw new Error("阶段 A 聊天区只读，停止能力属于阶段 B");
  },
};

export function takeFreshlyCreated(_sessionId: string): boolean {
  return false;
}

export const chatApi = {
  async trigger(_body?: unknown): Promise<{ status: string; session_id: string }> {
    throw new Error("阶段 A 聊天区只读，发送仍由 Nexus 既有入口处理");
  },
};
