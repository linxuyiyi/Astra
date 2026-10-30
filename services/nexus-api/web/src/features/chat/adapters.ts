import { toRaw } from "vue";
import { appendEvent, type Msg } from "@agentscope-ai/agentscope/message";
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import {
  channelTimeline, currentUid, getConversation, interruptChannel, interruptConversation,
  markChannelRead, nativeConversationMessages, readEventStream, sendChannel, sendConversation,
} from "@/api";
import type { ChatMessage, Channel, Conversation, Employee } from "@/types";
import type { ChatAdapter, ChatAdapterEvent, ChatSendOutcome, ReplyPhase } from "./types";
import { maxCursor, mergeMessages, streamCursor } from "./sessionState";
import { applyMessageEvent, projectMessage, timelineMessage } from "./nativeMessage";

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>(resolve => {
    if (signal.aborted) return resolve();
    const done = () => { clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}
function discussionPhase(channel: Channel): ReplyPhase {
  if (channel.discussion?.state === "running") return "streaming";
  return channel.discussion?.state === "queued" ? "queued" : "idle";
}

export class PrivateChatAdapter implements ChatAdapter {
  readonly kind = "contact" as const;
  readonly key: string;
  readonly hasOlder = false;
  private cursor = "";
  private messages: ChatMessage[] = [];
  private running = false;
  constructor(private employee: Employee, private conversation: Conversation) {
    this.key = `contact:${employee.id}`;
  }
  get title() { return this.employee.name; }
  get subtitle() { return this.employee.domain; }
  get canSend() { return this.employee.enabled; }
  get conversationId() { return this.conversation.id; }
  get sessionId() { return this.conversation.agentscope_session_id; }
  private project(message: Msg, cursor = this.cursor) {
    const peerId = message.metadata?.peer_employee_id;
    return { ...projectMessage(message, {
      id: message.role === "user" ? currentUid() : typeof peerId === "string" ? peerId : this.employee.id,
      kind: message.role === "user" ? "human" : message.role === "system" ? "system" : "employee",
      name: message.name,
    }), replay_cursor: cursor };
  }
  async load(): Promise<ChatMessage[]> {
    const binding = await getConversation(this.conversation.id);
    this.conversation.agentscope_session_id = binding.agentscope_session_id;
    const snapshot = await nativeConversationMessages(this.conversation.id);
    if (maxCursor(this.cursor, snapshot.event_cursor) === snapshot.event_cursor) this.running = snapshot.is_running;
    this.cursor = maxCursor(this.cursor, snapshot.event_cursor);
    this.messages = mergeMessages(this.messages, snapshot.messages.map(message => this.project(message, snapshot.event_cursor)));
    return this.messages;
  }
  async send(text: string, attachmentIds: string[], _signal: AbortSignal, _requestId: string): Promise<ChatSendOutcome> {
    const result = await sendConversation(this.conversation.id, text, attachmentIds);
    if (result.agentscope.session_id) this.conversation.agentscope_session_id = result.agentscope.session_id;
    let messages: ChatMessage[] = [];
    let activity: ReplyPhase = result.agentscope.accepted ? "queued" : "idle";
    let notice = result.agentscope.accepted ? "" : result.agentscope.detail || "消息尚未交给员工";
    // 提交已成功后的读取失败不是发送失败，不能诱导用户重复投递。
    try {
      messages = await this.load();
      activity = this.running ? "streaming" : "idle";
    } catch {
      if (!notice) notice = "消息已提交，回复连接暂不可用，请重新连接";
    }
    return { message: result.message, messages, notice, activity };
  }
  async interrupt() {
    await interruptConversation(this.conversation.id);
    const messages = await this.load();
    return { messages, activity: this.running ? "streaming" as const : "idle" as const };
  }
  taskQuery(): Record<string, string> {
    return this.sessionId ? { source_session_id: this.sessionId } : { source_conversation_id: this.conversation.id };
  }
  async follow(signal: AbortSignal, emit: (event: ChatAdapterEvent) => void): Promise<void> {
    const messages = await this.load();
    if (signal.aborted) return;
    emit({ type: "messages", messages });
    emit({ type: "activity", phase: this.running ? "streaming" : "idle" });
    if (!this.sessionId) return;
    while (!signal.aborted) {
      const params = new URLSearchParams({ raw: "1" });
      if (this.cursor) params.set("after", this.cursor);
      try {
        await readEventStream(`/api/v1/conversations/${this.conversation.id}/stream?${params}`, signal, raw => {
          if (signal.aborted) return;
          if (raw.type === "NEXUS_UPSTREAM_ERROR") {
            emit({ type: "notice", source: "follow", text: String(raw.detail || "回复连接中断，请重新连接") });
            return;
          }
          const nextCursor = streamCursor("", raw.id);
          if (nextCursor && maxCursor(this.cursor, nextCursor) === this.cursor) return;
          this.cursor = maxCursor(this.cursor, nextCursor);
          if (typeof raw.reply_id !== "string") return;
          const event = raw as unknown as AgentEvent;
          let native = this.messages.find(item => item.id === raw.reply_id)?.native;
          if (raw.type === "REPLY_START") {
            native = { id: raw.reply_id, name: String(raw.name || this.title), role: "assistant", content: [], metadata: {}, created_at: String(raw.created_at), finished_at: null };
          } else if (native) {
            native = appendEvent(structuredClone(toRaw(native)), event);
          }
          if (native) {
            const message = this.project(native);
            this.messages = mergeMessages(this.messages, [message]);
            emit({ type: "messages", messages: [message] });
          }
          if (raw.type === "REPLY_START") {
            this.running = true;
            emit({ type: "activity", phase: "streaming" });
          }
          if (raw.type === "REPLY_END") {
            this.running = false;
            emit({ type: "finished" });
            if (raw.finished_reason === "error") emit({ type: "notice", text: "回复失败，请检查员工连接" });
          }
        });
        if (!signal.aborted) await delay(400, signal);
      } catch (error) {
        if (signal.aborted) return;
        emit({ type: "notice", source: "follow", text: error instanceof Error ? error.message : "回复连接中断" });
        await delay(1000, signal);
      }
    }
  }
}

/** 群聊只消费 Nexus 的有序统一事件，不再为成员另建 AgentScope 流。 */
export class GroupChatAdapter implements ChatAdapter {
  readonly kind = "group" as const;
  readonly key: string;
  readonly conversationId = null;
  readonly sessionId = null;
  private cursor = 0;
  private before = 0;
  private latestSeq = 0;
  private messages: ChatMessage[] = [];
  private revoked = false;
  constructor(private channel: Channel, private employees: Employee[]) { this.key = `group:${channel.id}`; }
  setChannel(channel: Channel, employees: Employee[]) { this.channel = channel; this.employees = employees; }
  get title() { return this.channel.name; }
  get subtitle() { return this.channel.description; }
  get canSend() { return !this.revoked; }
  get hasOlder() { return this.before > 0; }
  async load(): Promise<ChatMessage[]> {
    const snapshot = await channelTimeline(this.channel.id);
    this.channel = snapshot.channel;
    this.before = snapshot.before;
    this.latestSeq = snapshot.items.at(-1)?.seq || 0;
    this.cursor = Math.max(this.cursor, snapshot.event_cursor);
    this.messages = mergeMessages(this.messages, snapshot.items.map(timelineMessage));
    return this.messages;
  }
  async loadOlder(): Promise<ChatMessage[]> {
    if (!this.before) return [];
    const snapshot = await channelTimeline(this.channel.id, this.before);
    this.before = snapshot.before;
    const older = snapshot.items.map(timelineMessage);
    this.messages = mergeMessages(this.messages, older);
    return older;
  }
  async send(text: string, attachmentIds: string[], _signal: AbortSignal, requestId: string): Promise<ChatSendOutcome> {
    const result = await sendChannel(this.channel.id, text, attachmentIds, requestId);
    const message = timelineMessage(result.message);
    this.messages = mergeMessages(this.messages, [message]);
    return { message, messages: [], activity: result.discussion_id ? "queued" : "idle", notice: "" };
  }
  async interrupt() {
    await interruptChannel(this.channel.id);
    const messages = await this.load();
    return { messages, activity: discussionPhase(this.channel) };
  }
  taskQuery() { return { source_channel_id: this.channel.id }; }
  async follow(signal: AbortSignal, emit: (event: ChatAdapterEvent) => void): Promise<void> {
    const messages = await this.load();
    if (signal.aborted) return;
    emit({ type: "messages", messages });
    emit({ type: "activity", phase: discussionPhase(this.channel) });
    let readSeq = 0;
    const markRead = (seq: number) => {
      if (seq <= readSeq || signal.aborted) return;
      readSeq = seq;
      void markChannelRead(this.channel.id, seq).catch(() => {
        if (!signal.aborted) emit({ type: "notice", text: "群聊已读标记更新失败" });
      });
    };
    markRead(this.latestSeq);
    while (!signal.aborted) {
      try {
        await readEventStream(`/api/v1/channels/${this.channel.id}/events?after=${this.cursor}`, signal, raw => {
          if (raw.type === "revoked") {
            this.revoked = true;
            emit({ type: "unavailable", text: "你已不在该群组，无法继续收发消息" });
            return;
          }
          const cursor = Number(raw.event_cursor);
          if (signal.aborted || !Number.isFinite(cursor) || cursor <= this.cursor) return;
          this.cursor = cursor;
          if (raw.type === "message" && raw.item) {
            const item = raw.item as Parameters<typeof timelineMessage>[0];
            const message = timelineMessage(item);
            this.messages = mergeMessages(this.messages, [message]);
            emit({ type: "messages", messages: [message] });
            markRead(item.seq);
          } else if (raw.type === "delta" && raw.event) {
            const current = this.messages.find(item => item.id === raw.message_id);
            if (!current) return;
            const message = applyMessageEvent(current, raw.event as AgentEvent, cursor);
            this.messages = mergeMessages(this.messages, [message]);
            emit({ type: "messages", messages: [message] });
          } else if (raw.type === "discussion") {
            const state = String(raw.state || "");
            this.channel.discussion = { state };
            emit({ type: "activity", phase: discussionPhase(this.channel) });
            if (raw.detail) emit({ type: "notice", text: String(raw.detail) });
          } else if (raw.type === "members") {
            window.dispatchEvent(new CustomEvent("nexus-chat-members", { detail: this.channel.id }));
          }
        });
        if (!signal.aborted) await delay(400, signal);
      } catch (error) {
        if (signal.aborted) return;
        emit({ type: "notice", source: "follow", text: error instanceof Error ? error.message : "群聊连接中断" });
        await delay(1000, signal);
      }
    }
  }
}

export function imageFile(type: string) { return type.startsWith("image/"); }
