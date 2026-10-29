import {
  channelMessages,
  channelTimeline,
  getConversation,
  interruptChannel,
  interruptConversation,
  readEventStream,
  sendChannel,
  sendConversation,
  type TimelineItem,
} from "@/api";
import type { ChatMessage, Channel, Conversation, Employee } from "@/types";
import type {
  ChatAdapter,
  ChatAdapterEvent,
  ChatSendOutcome,
  LiveLine,
  LiveTool,
} from "./types";
import { maxCursor, mergeMessages, sequenceIsCurrent, streamCursor } from "./sessionState";

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener("abort", () => {
      window.clearTimeout(timer);
      resolve();
    }, { once: true });
  });
}

function isImage(type: string) {
  return type.startsWith("image/");
}

function fromTimeline(item: TimelineItem): ChatMessage {
  return {
    id: item.id,
    sender: item.kind === "human" ? "user" : item.speaker,
    speaker: item.speaker,
    text: item.text,
    attachments: item.attachments ?? [],
    created_at: item.created_at,
    finished_at: item.finished_at,
    usage: item.usage ?? null,
  };
}


function applyRequireUserConfirm(tools: LiveTool[], event: Record<string, unknown>) {
  const calls = Array.isArray(event.tool_calls) ? event.tool_calls : [];
  for (const raw of calls) {
    if (!raw || typeof raw !== "object") continue;
    const call = raw as Record<string, unknown>;
    const id = typeof call.id === "string" ? call.id : "";
    if (!id) continue;
    const card = ensureTool(tools, id, typeof call.name === "string" ? call.name : "");
    if (typeof call.input === "string" && call.input && !card.input) card.input = call.input;
    card.state = "asking";
  }
}

function applyUserConfirmResult(tools: LiveTool[], event: Record<string, unknown>) {
  const results = Array.isArray(event.confirm_results) ? event.confirm_results : [];
  for (const raw of results) {
    if (!raw || typeof raw !== "object") continue;
    const result = raw as Record<string, unknown>;
    const toolCall = result.tool_call && typeof result.tool_call === "object"
      ? (result.tool_call as Record<string, unknown>)
      : null;
    const id = toolCall && typeof toolCall.id === "string" ? toolCall.id : "";
    if (!id) continue;
    const card = tools.find((item) => item.id === id);
    if (!card || card.state !== "asking") continue;
    card.state = result.confirmed ? "allowed" : "finished";
  }
}

function ensureTool(tools: LiveTool[], id: string, name = "") {
  let card = tools.find((item) => item.id === id);
  if (!card) {
    card = { id, name, input: "", output: "", state: "running" };
    tools.push(card);
  } else if (name && !card.name) {
    card.name = name;
  }
  return card;
}

function cloneLines(lines: LiveLine[]): LiveLine[] {
  return lines.map((line) => ({ ...line, tools: line.tools.map((tool) => ({ ...tool })) }));
}

export class PrivateChatAdapter implements ChatAdapter {
  readonly key: string;
  readonly kind = "contact" as const;
  private conversation: Conversation;
  private reloadSequence = 0;
  private liveText = "";
  private liveTools: LiveTool[] = [];
  private liveCreatedAt = "";
  private eventCursor = "";

  constructor(private employee: Employee, conversation: Conversation) {
    this.key = `contact:${employee.id}`;
    this.conversation = conversation;
  }

  get title() {
    return this.employee.name;
  }

  get subtitle() {
    return this.employee.domain;
  }

  get canSend() {
    return this.employee.enabled;
  }

  get conversationId() {
    return this.conversation.id;
  }

  get sessionId() {
    return this.conversation.agentscope_session_id;
  }

  async load(): Promise<ChatMessage[]> {
    const payload = await getConversation(this.conversation.id);
    this.conversation.agentscope_session_id = payload.agentscope_session_id;
    this.eventCursor = maxCursor(this.eventCursor, payload.event_cursor || "");
    return payload.messages;
  }

  async send(text: string, attachmentIds: string[], _signal: AbortSignal): Promise<ChatSendOutcome> {
    const result = await sendConversation(this.conversation.id, text, attachmentIds);
    if (result.agentscope.session_id) {
      this.conversation.agentscope_session_id = result.agentscope.session_id;
    }
    return {
      message: result.message,
      notice: result.agentscope.accepted ? "" : result.agentscope.detail || "没有生成回复",
      triggered: [],
    };
  }

  async interrupt(): Promise<void> {
    await interruptConversation(this.conversation.id);
  }

  taskQuery(): Record<string, string> {
    if (this.conversation.agentscope_session_id) {
      return { source_session_id: this.conversation.agentscope_session_id };
    }
    return { source_conversation_id: this.conversation.id };
  }

  async follow(signal: AbortSignal, emit: (event: ChatAdapterEvent) => void): Promise<void> {
    const payload = await getConversation(this.conversation.id);
    if (signal.aborted) return;
    this.conversation.agentscope_session_id = payload.agentscope_session_id;
    this.eventCursor = maxCursor(this.eventCursor, payload.event_cursor || "");
    emit({ type: "messages", messages: payload.messages });
    if (payload.history_error) emit({ type: "notice", text: payload.history_error });
    if (!payload.agentscope_session_id) return;

    const seen = new Set<string>(); // event id de-dupe across reconnects
    const cursor = { value: this.eventCursor };
    let liveActive = false;
    const reload = () => {
      const sequence = ++this.reloadSequence;
      return getConversation(this.conversation.id).then((next) => {
        if (!sequenceIsCurrent(sequence, this.reloadSequence, signal.aborted)) return;
        this.conversation.agentscope_session_id = next.agentscope_session_id;
        this.eventCursor = maxCursor(this.eventCursor, next.event_cursor || "");
        cursor.value = maxCursor(cursor.value, this.eventCursor);
        emit({ type: "messages", messages: next.messages });
      }).catch(() => undefined);
    };
    const publishLive = () => {
      // REPLY_START 后即便尚无 delta，也要推一条空 live，避免「停止」可见却无「正在回复」。
      if (!liveActive && !this.liveText && !this.liveTools.length) {
        emit({ type: "live", lines: [] });
        return;
      }
      emit({
        type: "live",
        lines: [{
          employeeId: this.employee.id,
          name: this.employee.name,
          text: this.liveText,
          tools: this.liveTools.map((tool) => ({ ...tool })),
          createdAt: this.liveCreatedAt || undefined,
        }],
      });
    };
    publishLive();

    // 对齐岛侧 raw=1：同连接承接多轮回复，不因 NEXUS_RUN_FINISHED 关流。
    for (let attempt = 0; attempt < 32; attempt += 1) {
      if (signal.aborted) return;
      const params = new URLSearchParams({ raw: "1" });
      if (cursor.value) params.set("after", cursor.value);
      try {
        await readEventStream(
          `/api/v1/conversations/${this.conversation.id}/stream?${params.toString()}`,
          signal,
          (event) => {
            cursor.value = streamCursor(cursor.value, event.id);
            this.eventCursor = cursor.value;
            if (typeof event.id === "string") {
              if (seen.has(event.id)) return;
              seen.add(event.id);
            }
            if (event.type === "REPLY_START") {
              liveActive = true;
              this.liveText = "";
              this.liveTools = [];
              this.liveCreatedAt = new Date().toISOString();
              publishLive();
              return;
            }
            if (event.type === "TEXT_BLOCK_DELTA" && typeof event.delta === "string") {
              liveActive = true;
              this.liveText += event.delta;
              publishLive();
              return;
            }
            if (event.type === "TOOL_CALL_START" && typeof event.tool_call_id === "string") {
              liveActive = true;
              ensureTool(this.liveTools, event.tool_call_id, typeof event.tool_call_name === "string" ? event.tool_call_name : "");
              publishLive();
              return;
            }
            if (event.type === "TOOL_CALL_DELTA" && typeof event.tool_call_id === "string" && typeof event.delta === "string") {
              ensureTool(this.liveTools, event.tool_call_id).input += event.delta;
              publishLive();
              return;
            }
            if (event.type === "TOOL_RESULT_TEXT_DELTA" && typeof event.tool_call_id === "string" && typeof event.delta === "string") {
              ensureTool(this.liveTools, event.tool_call_id).output += event.delta;
              publishLive();
              return;
            }
            if (event.type === "TOOL_CALL_END" && typeof event.tool_call_id === "string") {
              // 参数收齐；若随后 REQUIRE_USER_CONFIRM 会把 state 打成 asking。
              ensureTool(this.liveTools, event.tool_call_id);
              publishLive();
              return;
            }
            if (event.type === "REQUIRE_USER_CONFIRM") {
              liveActive = true;
              applyRequireUserConfirm(this.liveTools, event);
              publishLive();
              return;
            }
            if (event.type === "USER_CONFIRM_RESULT") {
              applyUserConfirmResult(this.liveTools, event);
              publishLive();
              return;
            }
            if (event.type === "TOOL_RESULT_START" && typeof event.tool_call_id === "string") {
              ensureTool(this.liveTools, event.tool_call_id).state = "done";
              publishLive();
              return;
            }
            if (event.type === "REPLY_END" || event.type === "NEXUS_RUN_FINISHED") {
              const reason = String(event.finished_reason ?? "");
              if (reason === "error" || reason === "interrupted") {
                emit({ type: "notice", text: reason === "interrupted" ? "回复已中断" : "回复失败" });
              }
              liveActive = false;
              this.liveText = "";
              this.liveTools = [];
              this.liveCreatedAt = "";
              emit({ type: "live", lines: [] });
              emit({ type: "finished" });
              void reload();
              // raw=1 下继续听下一轮，不退出 follow。
              return;
            }
            if (event.type === "NEXUS_UPSTREAM_ERROR") {
              emit({ type: "notice", text: String(event.detail ?? "事件流中断") });
            }
          },
        );
        if (signal.aborted) return;
        // 连接被服务端正常结束：短暂退避后带着 cursor 重连，保住后续发送的 delta。
        await delay(400, signal);
      } catch (error) {
        if (signal.aborted) return;
        emit({ type: "notice", text: error instanceof Error ? error.message : "事件流中断" });
        await delay(1000, signal);
      }
    }
  }
}

export class GroupChatAdapter implements ChatAdapter {
  readonly key: string;
  readonly kind = "group" as const;
  private memberCursors = new Map<string, string>();
  private memberSeen = new Map<string, Set<string>>();
  private liveState = new Map<string, LiveLine>();
  private reloadSequence = 0;

  constructor(private channel: Channel, private employees: Employee[]) {
    this.key = `group:${channel.id}`;
  }

  setChannel(channel: Channel, employees: Employee[]) {
    this.channel = channel;
    this.employees = employees;
  }

  get title() {
    return this.channel.name;
  }

  get subtitle() {
    return `${this.channel.members.length} 位数字员工`;
  }

  get canSend() {
    return true;
  }

  get conversationId() {
    return null;
  }

  get sessionId() {
    return null;
  }

  get memberEmployees() {
    return this.channel.members
      .map((member) => this.employees.find((item) => item.id === member.employee_id))
      .filter((item): item is Employee => Boolean(item));
  }

  async load(): Promise<ChatMessage[]> {
    const payload = await channelTimeline(this.channel.id);
    return payload.items.map(fromTimeline);
  }

  async send(text: string, attachmentIds: string[], _signal: AbortSignal): Promise<ChatSendOutcome> {
    const result = await sendChannel(this.channel.id, text, attachmentIds);
    const rejected = result.triggered.filter((item) => !item.accepted);
    let notice = "";
    if (!result.triggered.length) notice = "没有点名已启用的员工，因此没有触发任何人。";
    else if (rejected.length) {
      notice = rejected.map((item) => `${item.name}：${item.detail}`).filter(Boolean).join(" ");
    }
    return {
      message: result.message,
      notice,
      triggered: result.triggered.map((item) => ({
        employee_id: item.employee_id,
        name: item.name,
        accepted: item.accepted,
        detail: item.detail,
      })),
    };
  }

  async interrupt(): Promise<void> {
    await interruptChannel(this.channel.id);
  }

  taskQuery(): Record<string, string> {
    return { source_channel_id: this.channel.id };
  }

  private speakerName(employeeId: string) {
    return this.employees.find((item) => item.id === employeeId)?.name ?? "数字员工";
  }

  async follow(signal: AbortSignal, emit: (event: ChatAdapterEvent) => void): Promise<void> {
    const initial = await this.load();
    if (signal.aborted) return;
    let latestMessages = initial;
    emit({ type: "messages", messages: initial });
    const publishLive = () => emit({ type: "live", lines: cloneLines([...this.liveState.values()]) });
    publishLive();

    const reload = async () => {
      if (signal.aborted) return;
      const sequence = ++this.reloadSequence;
      try {
        const items = await this.load();
        if (!sequenceIsCurrent(sequence, this.reloadSequence, signal.aborted)) return;
        latestMessages = mergeMessages(latestMessages, items);
        emit({ type: "messages", messages: latestMessages });
      } catch {
        /* 保留当前消息，下一次事件会再尝试 */
      }
    };

    // 对齐私聊 follow：同连接/重连保活，不因 quiet 或 120s 自退丢流。
    const followChannel = async () => {
      for (let attempt = 0; attempt < 32; attempt += 1) {
        if (signal.aborted) return;
        try {
          await readEventStream(`/api/v1/channels/${this.channel.id}/events`, signal, () => {
            void reload();
          });
          if (signal.aborted) return;
          await delay(400, signal);
        } catch (error) {
          if (signal.aborted) return;
          emit({ type: "notice", text: error instanceof Error ? error.message : "事件流中断" });
          await delay(1000, signal);
        }
      }
    };

    const followMember = async (employeeId: string) => {
      const seen = this.memberSeen.get(employeeId) ?? new Set<string>();
      this.memberSeen.set(employeeId, seen);
      const cursor = { value: this.memberCursors.get(employeeId) ?? "" };
      for (let attempt = 0; attempt < 32; attempt += 1) {
        if (signal.aborted) return;
        try {
          const params = new URLSearchParams({ raw: "1" });
          if (cursor.value) params.set("after", cursor.value);
          await readEventStream(
            `/api/v1/channels/${this.channel.id}/members/${employeeId}/stream?${params.toString()}`,
            signal,
            (event) => {
              cursor.value = streamCursor(cursor.value, event.id);
              this.memberCursors.set(employeeId, cursor.value);
              if (typeof event.id === "string") {
                if (seen.has(event.id)) return;
                seen.add(event.id);
              }
              if (event.type === "REPLY_START") {
                this.liveState.set(employeeId, {
                  employeeId,
                  name: this.speakerName(employeeId),
                  text: "",
                  tools: [],
                  createdAt: new Date().toISOString(),
                });
                publishLive();
                return;
              }
              if (event.type === "TEXT_BLOCK_DELTA" && typeof event.delta === "string") {
                const line = this.liveState.get(employeeId);
                if (line) line.text += event.delta;
                else this.liveState.set(employeeId, { employeeId, name: this.speakerName(employeeId), text: event.delta, tools: [], createdAt: new Date().toISOString() });
                publishLive();
                return;
              }
              if (event.type === "TOOL_CALL_START" && typeof event.tool_call_id === "string") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                ensureTool(line.tools, event.tool_call_id, typeof event.tool_call_name === "string" ? event.tool_call_name : "");
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "TOOL_CALL_DELTA" && typeof event.tool_call_id === "string" && typeof event.delta === "string") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                ensureTool(line.tools, event.tool_call_id).input += event.delta;
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "TOOL_CALL_END" && typeof event.tool_call_id === "string") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                ensureTool(line.tools, event.tool_call_id);
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "TOOL_RESULT_TEXT_DELTA" && typeof event.tool_call_id === "string" && typeof event.delta === "string") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                ensureTool(line.tools, event.tool_call_id).output += event.delta;
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "TOOL_RESULT_START" && typeof event.tool_call_id === "string") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                ensureTool(line.tools, event.tool_call_id).state = "done";
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "REQUIRE_USER_CONFIRM") {
                const line = this.liveState.get(employeeId) ?? { employeeId, name: this.speakerName(employeeId), text: "", tools: [], createdAt: new Date().toISOString() };
                applyRequireUserConfirm(line.tools, event);
                this.liveState.set(employeeId, line);
                publishLive();
                return;
              }
              if (event.type === "USER_CONFIRM_RESULT") {
                const line = this.liveState.get(employeeId);
                if (line) {
                  applyUserConfirmResult(line.tools, event);
                  publishLive();
                }
                return;
              }
              if (event.type === "REPLY_END" || event.type === "NEXUS_RUN_FINISHED") {
                const reason = String(event.finished_reason ?? "");
                if (reason === "error" || reason === "interrupted") {
                  emit({ type: "notice", text: reason === "interrupted" ? "回复已中断" : "回复失败" });
                }
                this.liveState.delete(employeeId);
                publishLive();
                emit({ type: "finished" });
                void reload();
                // raw=1 下继续听下一轮，不退出 follow。
                return;
              }
              if (event.type === "NEXUS_UPSTREAM_ERROR") {
                emit({ type: "notice", text: String(event.detail ?? "事件流中断") });
              }
            },
          );
          if (signal.aborted) return;
          await delay(400, signal);
        } catch (error) {
          if (signal.aborted) return;
          emit({ type: "notice", text: error instanceof Error ? error.message : "事件流中断" });
          await delay(1000, signal);
        }
      }
    };

    void followChannel();
    const watching = new Set<string>();
    while (!signal.aborted) {
      let payload;
      try {
        payload = await channelMessages(this.channel.id);
      } catch (error) {
        if (signal.aborted) return;
        emit({ type: "notice", text: error instanceof Error ? error.message : "事件流中断" });
        await delay(1000, signal);
        continue;
      }
      if (signal.aborted) return;
      this.channel.members = payload.members;
      if (payload.history_error) emit({ type: "notice", text: payload.history_error });
      for (const member of payload.members) {
        if (!member.agentscope_session_id || watching.has(member.employee_id)) continue;
        watching.add(member.employee_id);
        void followMember(member.employee_id);
      }
      await delay(1000, signal);
    }
  }

}

export function imageFile(type: string) {
  return isImage(type);
}
