import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";
import { nexusFetch, nexusGet } from "./http";

export interface GroupMember {
  member_id: string;
  member_type: "human" | "employee";
  employee_id: string;
  name: string;
  avatar: string;
  role: "owner" | "admin" | "member";
  online: boolean;
  agentscope_session_id: string | null;
}
export interface GroupChannel {
  id: string;
  name: string;
  description: string;
  viewer_id: string;
  members: GroupMember[];
  discussion?: { state: string; detail?: string } | null;
}
export interface GroupItem {
  id: string;
  seq: number;
  sender_type: "human" | "employee" | "system";
  sender_id: string;
  sender_name: string;
  text: string;
  created_at: string;
  discussion_id: string;
  message: Msg;
}
export interface GroupTimeline {
  channel: GroupChannel;
  items: GroupItem[];
  before: number;
  event_cursor: number;
}
export interface ChannelEvent {
  cursor: number;
  type: "message" | "delta" | "discussion" | "members" | "revoked";
  item?: GroupItem;
  message_id?: string;
  event?: AgentEvent;
  state?: string;
  detail?: string;
}
export function loadGroupTimeline(channelId: string, before = 0): Promise<GroupTimeline> {
  return nexusGet(`/api/v1/channels/${channelId}/messages?before=${before}&limit=50`);
}
export async function sendGroupMessage(channelId: string, text: string, attachmentIds: string[], clientId: string): Promise<{ message: GroupItem }> {
  const response = await nexusFetch(`/api/v1/channels/${channelId}/messages`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, attachment_ids: attachmentIds, client_id: clientId }),
  });
  if (!response.ok) {
    const payload = await response.json() as { detail?: string };
    throw new Error(payload.detail || "发送失败");
  }
  return response.json();
}
export async function* streamChannelEvents(channelId: string, signal: AbortSignal, after: number): AsyncGenerator<ChannelEvent> {
  const response = await nexusFetch(`/api/v1/channels/${channelId}/events?after=${after}`, { signal });
  if (!response.ok || !response.body) throw new Error(response.status === 403 ? "你已不在该群组" : "实时连接失败");
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
        const lines = frame.split("\n");
        const data = lines.find(line => line.startsWith("data:"));
        if (!data) continue;
        const cursor = Number(lines.find(line => line.startsWith("id:"))?.slice(3) || after);
        yield { ...JSON.parse(data.slice(5)), cursor } as ChannelEvent;
      }
    }
  } finally { reader.releaseLock(); }
}
export async function interruptGroup(channelId: string): Promise<void> {
  const response = await nexusFetch(`/api/v1/channels/${channelId}/interrupt`, { method: "POST" });
  if (!response.ok) throw new Error("停止失败，请重试");
}
