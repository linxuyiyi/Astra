/**
 * 群组数据通道：原生时间线、每位成员的 raw 事件流、发送与停止。
 *
 * 群组不是一条 AgentScope Session，而是 Nexus channel + 每位员工各自的 Session。
 * 这里只做数据搬运，渲染由 GroupApp 复用上游消息组件。
 */
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";

import { nexusFetch, nexusGet } from "./http";

export interface GroupMember {
  employee_id: string;
  agentscope_session_id: string;
}

export interface GroupChannel {
  id: string;
  name: string;
  description: string;
  members: GroupMember[];
}

export interface GroupStream {
  session_id: string;
  employee_id: string;
  label: string;
  event_cursor?: string;
  messages: Msg[];
}

export interface GroupTimeline {
  channel: GroupChannel;
  streams: GroupStream[];
}

export interface GroupMessage {
  id: string;
  text: string;
  created_at: string;
  accepted?: boolean;
  attachments?: Array<{ id: string; name: string; type: string; size: number }>;
}

/** 一位被 @ 的成员的投递结果；accepted=false 时 detail 是后端给出的原因。 */
export interface GroupTrigger {
  employee_id: string;
  name: string;
  accepted: boolean;
  detail?: string;
  session_id: string | null;
}

export interface GroupSendResult {
  message: GroupMessage;
  triggered: GroupTrigger[];
  record: { accepted: boolean; detail?: string };
}

/** 群事件：其他人类成员据此同步消息与刚建出来的成员 Session。 */
export interface ChannelEvent {
  id: string;
  text: string;
  speaker: string;
  message?: Msg;
  sessions?: Array<{ employee_id: string; session_id: string }>;
}

export async function loadGroupTimeline(channelId: string): Promise<GroupTimeline> {
  return nexusGet<GroupTimeline>(`/api/v1/channels/${channelId}/timeline/native`);
}

/** 单个成员的 raw 事件流：一轮结束后不关闭，便于连续多轮回复。 */
export async function* streamMember(
  channelId: string,
  employeeId: string,
  signal: AbortSignal,
  after?: string,
): AsyncGenerator<AgentEvent> {
  const query = new URLSearchParams({ raw: "1" });
  if (after) query.set("after", after);
  let response: Response;
  try {
    response = await nexusFetch(
      `/api/v1/channels/${channelId}/members/${employeeId}/stream?${query.toString()}`,
      { signal },
    );
  } catch {
    return;
  }
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
        if (!dataLine) continue;
        const raw = dataLine.slice(5).trim();
        if (!raw) continue;
        try {
          yield JSON.parse(raw) as AgentEvent;
        } catch {
          // 坏帧不终止整条流。
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function sendGroupMessage(
  channelId: string,
  text: string,
  attachmentIds: string[],
): Promise<GroupSendResult> {
  const response = await nexusFetch(`/api/v1/channels/${channelId}/messages`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, attachment_ids: attachmentIds }),
  });
  if (!response.ok) {
    let detail = response.statusText || "发送失败";
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // 保留状态文本
    }
    throw new Error(detail);
  }
  return (await response.json()) as GroupSendResult;
}

/** 人类消息事件：只推送消息本身与新建的成员绑定，不推送员工回复。 */
export async function* streamChannelEvents(
  channelId: string,
  signal: AbortSignal,
): AsyncGenerator<ChannelEvent> {
  let response: Response;
  try {
    response = await nexusFetch(`/api/v1/channels/${channelId}/events`, { signal });
  } catch {
    return;
  }
  if (!response.ok || !response.body) return;
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const dataLine = frame.split("\n").find((line) => line.startsWith("data:"));
        if (!dataLine) continue;
        const raw = dataLine.slice(5).trim();
        if (!raw) continue;
        try {
          yield JSON.parse(raw) as ChannelEvent;
        } catch {
          // 坏帧不终止整条流。
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}

export async function interruptMember(channelId: string, employeeId: string): Promise<void> {
  const response = await nexusFetch(
    `/api/v1/channels/${channelId}/members/${employeeId}/interrupt`,
    { method: "POST" },
  );
  if (!response.ok) throw new Error(response.statusText || "停止失败");
}
