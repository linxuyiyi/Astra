import { toRaw } from "vue";
import { appendEvent, type DataBlock, type Msg } from "@agentscope-ai/agentscope/message";
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { ChatMessage } from "@/types";
import type { TimelineItem } from "@/api";

export interface MessageIdentity {
  id: string;
  kind: "human" | "employee" | "system";
  name: string;
}

/** 仅为视图投影；原生内容和状态由 AgentScope SDK 保留，不另建执行状态。 */
export function projectMessage(native: Msg, identity: MessageIdentity): ChatMessage {
  const tools: NonNullable<ChatMessage["tools"]> = [];
  const data: DataBlock[] = [];
  const thinking: string[] = [];
  const hints: NonNullable<ChatMessage["hints"]> = [];
  const text: string[] = [];
  for (const block of native.content) {
    if (block.type === "text") text.push(block.text);
    if (block.type === "thinking") thinking.push(block.thinking);
    if (block.type === "data") data.push(block);
    if (block.type === "hint") {
      hints.push({ id: block.id, source: block.source || "提示", text: typeof block.hint === "string" ? block.hint
        : block.hint.filter(item => item.type === "text").map(item => item.text).join("\n") });
      if (Array.isArray(block.hint)) data.push(...block.hint.filter((item): item is DataBlock => item.type === "data"));
    }
    if (block.type === "tool_call") {
      tools.push({ id: block.id, name: block.name, input: block.input, output: "", state: block.state });
    }
    if (block.type === "tool_result") {
      let tool = tools.find(item => item.id === block.id);
      if (!tool) {
        tool = { id: block.id, name: block.name, input: "", output: "", state: block.state };
        tools.push(tool);
      }
      tool.state = block.state;
      tool.output = typeof block.output === "string" ? block.output
        : block.output.filter(item => item.type === "text").map(item => item.text).join("\n");
      if (Array.isArray(block.output)) data.push(...block.output.filter((item): item is DataBlock => item.type === "data"));
    }
  }
  const uploaded = (native.metadata?.attachments ?? []) as unknown as ChatMessage["attachments"];
  const attachments: ChatMessage["attachments"] = [...uploaded];
  const inlineData: DataBlock[] = [];
  for (const block of data) {
    const id = block.source.type === "url" ? block.source.url.match(/^\/api\/v1\/attachments\/([\w-]+)$/)?.[1] : undefined;
    if (id) attachments.push({ id, name: block.name || "附件", type: block.source.media_type, size: 0 });
    else if (!(native.role === "user" && uploaded.length)) inlineData.push(block);
  }
  return {
    id: native.id,
    sender: native.role === "user" ? "user" : identity.id,
    speaker: identity.id,
    sender_type: identity.kind,
    sender_name: identity.name,
    text: typeof native.metadata?.display_text === "string" ? native.metadata.display_text : text.join("\n"),
    tools, thinking, hints, data: inlineData, attachments,
    native,
    created_at: native.created_at,
    finished_at: native.finished_at,
    finished_reason: native.finished_reason,
    usage: native.usage,
  };
}

export function timelineMessage(item: TimelineItem): ChatMessage {
  let native = structuredClone(toRaw(item.message));
  const events = (native.metadata?.stream_events ?? []) as unknown as AgentEvent[];
  for (const event of events) {
    if (event.type === "REPLY_START") native.finished_at = null;
    native = appendEvent(native, event);
  }
  // 后端在完成持久化时替换原生消息；运行中重放只发生在这份视图副本。
  delete native.metadata.stream_events;
  return projectMessage(native, { id: item.sender_id, kind: item.sender_type, name: item.sender_name });
}

export function applyMessageEvent(message: ChatMessage, event: AgentEvent, cursor: number): ChatMessage {
  const native = message.native!;
  if (Number(native.metadata?.stream_cursor ?? 0) >= cursor) return message;
  const next = structuredClone(toRaw(native));
  if (event.type === "REPLY_START") next.finished_at = null;
  appendEvent(next, event);
  next.metadata = { ...next.metadata, stream_cursor: cursor };
  return projectMessage(next, { id: message.speaker!, kind: message.sender_type!, name: message.sender_name! });
}
