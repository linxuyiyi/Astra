import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";
import type { ThreadMessageLike } from "@assistant-ui/react";
import {
  compareEntries,
  projectEntry,
  replyPhase,
  ReplyEvents,
  type ConversationPhase,
  type TimelineEntry,
} from "./conversationMessage.ts";
import type { UploadedFile } from "./attachments.ts";
import { ConversationHttpError, nexusGet, nexusPost, readFrames } from "./http.ts";

type Kind = "private" | "group";
interface Member {
  id: string;
  name: string;
  avatar: string;
  role: string;
}
interface PrivatePage {
  employee: { id: string; name: string; domain: string; avatar: string; enabled: boolean };
  session_id: string | null;
  viewer_id: string;
  messages: Msg[];
  before: string | null;
  event_cursor: string;
  is_running: boolean;
  pending_peer_replies?: boolean;
}
interface GroupItem {
  message: Msg;
  seq: number;
  discussion_id?: string;
  sender_type: "employee" | "human" | "system";
  sender_id: string;
  sender_name: string;
}
interface Discussion {
  id: string;
  state: string;
  detail?: string;
  active_employee_id?: string;
}
interface GroupPage {
  channel: {
    name: string;
    viewer_id: string;
    members: {
      member_id: string;
      member_type: "employee" | "human";
      name: string;
      avatar: string;
      role: string;
      enabled: boolean;
    }[];
    discussion?: Discussion;
  };
  items: GroupItem[];
  before: number;
  event_cursor: number;
}
type ChannelEvent =
  | { type: "message"; item: GroupItem }
  | { type: "delta"; message_id: string; seq: number; event: AgentEvent }
  | { type: "discussion-worker"; id: string; active_employee_id: string }
  | { type: "revoked" | "members" }
  | ({ type: "discussion" } & Discussion);
type Snapshot = {
  messages: ThreadMessageLike[];
  title: string;
  members: Member[];
  activeEmployee: string;
  phase: ConversationPhase;
  loading: boolean;
  loadingOlder: boolean;
  hasOlder: boolean;
  canType: boolean;
  canSend: boolean;
  canStop: boolean;
  sending: boolean;
  error: string;
  retryable: "send" | "connection" | null;
};
const empty: Snapshot = {
  messages: [],
  title: "",
  members: [],
  activeEmployee: "",
  phase: "idle",
  loading: true,
  loadingOlder: false,
  hasOlder: false,
  canType: false,
  canSend: false,
  canStop: false,
  sending: false,
  error: "",
  retryable: null,
};
type Attempt = { text: string; files: UploadedFile[]; clientId: string; editing?: true };
type SendOutcome = "accepted" | "rejected" | "unconfirmed";
const detail = (error: unknown) => (error instanceof Error ? error.message : String(error));
const DELTA_PAINT_INTERVAL = 32;
const RECONCILE_INTERVAL = 750;

function delay(signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    if (signal.aborted) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, 1200);
    signal.addEventListener("abort", done, { once: true });
  });
}

export class ConversationStore {
  private kind: Kind;
  private base: string;
  private state = empty;
  private listeners = new Set<() => void>();
  private entries = new Map<string, TimelineEntry>();
  private views = new Map<string, ThreadMessageLike>();
  private events = new Map<string, ReplyEvents>();
  private dirty = new Set<string>();
  private orderChanged = false;
  private ordered: TimelineEntry[] = [];
  private frame = 0;
  private paintTimer?: ReturnType<typeof setTimeout>;
  private lastPaint = -Infinity;
  private life = new AbortController();
  private stream?: AbortController;
  private before: string | number | null = null;
  private cursor = "";
  private viewer = "";
  private privateEmployee?: PrivatePage["employee"];
  private sessionId: string | null = null;
  private members = new Map<string, { name: string; avatar: string }>();
  private enabled = false;
  private busy = false;
  private syncing = false;
  private stopping = false;
  private activeReply = "";
  private activeEmployee = "";
  private discussion = "";
  private discussionState = "";
  private marked = 0;
  private errorSource: "send" | "connect" | "other" | "" = "";
  private failed?: Attempt;
  private hasSnapshot = false;
  private peerTimer?: ReturnType<typeof setTimeout>;
  private reconcileTimer?: ReturnType<typeof setTimeout>;
  onMembersChanged?: () => void;

  constructor(kind: Kind, id: string) {
    this.kind = kind;
    this.base = `/api/v1/${kind === "private" ? "conversations" : "channels"}/${encodeURIComponent(id)}`;
  }

  snapshot = () => this.state;
  subscribe = (fn: () => void) => {
    this.listeners.add(fn);
    return () => {
      this.listeners.delete(fn);
    };
  };

  start() {
    this.life = new AbortController();
    this.follow();
    return () => {
      this.life.abort();
      this.stream?.abort();
      clearTimeout(this.peerTimer);
      clearTimeout(this.reconcileTimer);
      this.cancelPaint();
    };
  }

  private publish(patch: Partial<Snapshot> = {}) {
    if (this.life.signal.aborted) return;
    this.cancelPaint();
    if (this.dirty.size) {
      for (const key of this.dirty) this.views.set(key, projectEntry(this.entries.get(key)!));
      this.dirty.clear();
      if (this.orderChanged) {
        this.ordered = [...this.entries.values()].sort(compareEntries);
        this.orderChanged = false;
      }
      patch.messages = this.ordered.map((entry) => this.views.get(entry.native.id)!);
    }
    this.state = { ...this.state, ...patch };
    const phase =
      this.stopping && this.busy
        ? "stopping"
        : this.syncing
          ? "syncing"
          : this.busy
            ? replyPhase(this.entries.get(this.activeReply)?.native)
            : "idle";
    this.state.phase = phase;
    const active = this.entries.get(this.activeReply);
    const employee = active?.native.finished_at ? "" : this.activeEmployee || active?.sender.senderId;
    this.state.activeEmployee = employee ? this.members.get(employee)?.name || "" : "";
    this.state.hasOlder = Boolean(this.before);
    this.state.canType = this.hasSnapshot && this.enabled;
    this.state.canStop = this.busy && !this.syncing && !this.stopping && this.enabled;
    const unconfirmed = this.failed && !this.failed.editing;
    this.state.canSend =
      this.state.canType && !unconfirmed && !this.state.sending &&
      (this.kind === "group" || (!this.busy && !this.syncing));
    this.state.retryable = unconfirmed ? "send" : this.errorSource === "connect" ? "connection" : null;
    this.listeners.forEach((fn) => fn());
  }

  private cancelPaint() {
    clearTimeout(this.paintTimer);
    this.paintTimer = undefined;
    if (this.frame) cancelAnimationFrame(this.frame);
    this.frame = 0;
  }

  private schedule(batchDeltas = false) {
    if (this.frame || (batchDeltas && this.paintTimer !== undefined)) return;
    clearTimeout(this.paintTimer);
    const nextFrame = () => {
      this.paintTimer = undefined;
      this.frame = requestAnimationFrame(() => {
        this.frame = 0;
        this.lastPaint = performance.now();
        this.publish();
      });
    };
    const wait = DELTA_PAINT_INTERVAL - (performance.now() - this.lastPaint);
    // 接收、工具状态与终态走下一帧。连续增量最多约每 32 毫秒更新一次。
    if (batchDeltas && wait > 0) this.paintTimer = setTimeout(nextFrame, wait);
    else nextFrame();
  }

  private put(entry: TimelineEntry) {
    if (!this.entries.has(entry.native.id)) this.orderChanged = true;
    this.entries.set(entry.native.id, entry);
    this.dirty.add(entry.native.id);
  }

  private privateEntry(native: Msg): TimelineEntry {
    const human = native.role === "user";
    const peer = native.metadata?.peer_employee_id;
    const employee = this.privateEmployee!;
    return {
      native,
      sender: {
        senderId: human ? this.viewer : typeof peer === "string" ? peer : employee.id,
        senderName: human ? this.viewer : typeof peer === "string" ? native.name : employee.name,
        senderKind: human ? "human" : native.role === "system" ? "system" : "employee",
        self: human,
        avatar: human ? "" : peer ? String(native.metadata?.peer_avatar || "") : employee.avatar,
      },
      delivery: human ? "sent" : undefined,
    };
  }

  private putGroup(item: GroupItem) {
    const native = item.message;
    native.metadata ??= {};
    const prev = this.entries.get(native.id);
    if (Number(prev?.native.metadata.stream_cursor || 0) > Number(native.metadata.stream_cursor || 0)) return;
    const replay = native.metadata.stream_events as unknown as AgentEvent[] | undefined;
    if (replay) {
      const seen = new ReplyEvents();
      for (const event of replay) seen.apply(native, event);
      if (!native.finished_at) this.events.set(native.id, seen);
      delete native.metadata.stream_events;
    }
    if (native.finished_at) this.events.delete(native.id);
    const self = item.sender_type === "human" && item.sender_id === this.viewer;
    this.put({
      native,
      sequence: item.seq,
      delivery: self ? "sent" : undefined,
      sender: {
        senderId: item.sender_id,
        senderKind: item.sender_type,
        senderName: item.sender_name,
        self,
        avatar: this.members.get(item.sender_id)?.avatar || "",
      },
    });
  }

  private setDiscussion(discussion?: Discussion) {
    if (discussion?.id !== this.discussion) {
      this.activeReply = "";
      this.stopping = false;
    }
    this.discussion = discussion?.id ?? "";
    this.discussionState = discussion?.state ?? "";
    this.busy = !!discussion && ["queued", "running", "superseded"].includes(discussion.state);
    this.setActiveEmployee(this.busy ? discussion?.active_employee_id ?? "" : "");
    if (discussion?.state === "superseded") this.stopping = true;
    if (!this.busy) {
      this.stopping = false;
      this.activeReply = "";
    }
  }

  private setActiveEmployee(employee: string) {
    if (employee !== this.activeEmployee) this.activeReply = "";
    this.activeEmployee = employee;
  }

  private async page(signal: AbortSignal, older = false) {
    const query = new URLSearchParams({ limit: "50" });
    if (older && this.before) query.set("before", String(this.before));
    const path = `${this.base}/messages?${query}`;
    const patch: Partial<Snapshot> = {};
    if (this.kind === "private") {
      const page = await nexusGet<PrivatePage>(path, signal);
      if (signal.aborted) return;
      this.viewer = page.viewer_id;
      this.privateEmployee = page.employee;
      for (const native of page.messages) {
        if (older && this.entries.has(native.id)) continue;
        const prior = this.entries.get(native.id);
        // 同一 checkpoint 未前移时，已在本页消费的增量可能领先于持久化消息。
        if (!older && prior && (this.events.has(native.id) || prior.awaitingSnapshot) &&
          !native.finished_at && page.event_cursor === this.cursor)
          continue;
        this.events.delete(native.id);
        this.put(this.privateEntry(native));
      }
      if (!older) {
        this.enabled = Boolean(page.employee.enabled);
        this.sessionId = page.session_id;
        this.cursor = page.event_cursor;
        if (page.pending_peer_replies) this.refreshParticipants(signal);
        patch.title = page.employee.name;
        const tail = page.messages.filter((msg) => !msg.metadata?.peer_employee_id).at(-1);
        const active = tail?.role === "assistant" && !tail.finished_at
          ? this.entries.get(tail.id)
          : this.entries.get(this.activeReply);
        const confirming = replyPhase(active?.native) === "approval";
        this.busy = page.is_running || confirming;
        const lastReply = active?.native ?? (tail?.role === "assistant" ? tail : undefined);
        const missingTerminal = Boolean(active?.awaitingSnapshot);
        this.syncing = missingTerminal || (page.is_running && !!lastReply?.finished_at);
        // 上游先读历史再读运行锁，若两次读取横跨提交，保留待同步回复并继续对账。
        this.activeReply = this.busy || this.syncing ? active?.native.id ?? "" : "";
        if (!this.busy) this.stopping = false;
        if (this.syncing || this.stopping) this.reconcilePrivate();
      }
      if (older || !this.hasSnapshot) this.before = page.before;
    } else {
      const page = await nexusGet<GroupPage>(path, signal);
      if (signal.aborted) return;
      if (!older) {
        const channel = page.channel;
        this.viewer = channel.viewer_id;
        this.members = new Map(channel.members.map((member) => [member.member_id, member]));
        this.enabled = true;
        this.setDiscussion(channel.discussion);
        this.cursor = String(page.event_cursor);
        patch.title = channel.name;
        patch.members = channel.members
          .filter((member) => member.member_type === "employee" && member.enabled)
          .map((member) => ({
            id: member.member_id,
            name: member.name,
            avatar: member.avatar,
            role: member.role,
          }));
        if (this.errorSource !== "send")
          patch.error = channel.discussion?.detail || (channel.discussion?.state === "failed" ? "讨论失败" : "");
      }
      for (const item of page.items) {
        if (older && this.entries.has(item.message.id)) continue;
        this.putGroup(item);
        if (
          !older &&
          item.sender_type === "employee" &&
          !item.message.finished_at &&
          item.discussion_id === this.discussion
        )
          this.activeReply = item.message.id;
      }
      if (older || !this.hasSnapshot) this.before = page.before;
      if (!older) this.markRead(page.items.at(-1)?.seq);
    }
    if (!older) {
      if (!this.hasSnapshot) {
        this.failed = this.readPendingSend();
        if (this.failed && !this.failed.editing) {
          this.errorSource = "send";
          patch.error = "上次发送结果尚未确认，可重试原消息";
        }
      }
      this.hasSnapshot = true;
      patch.loading = false;
      if (this.errorSource === "connect" && !patch.error) {
        this.errorSource = "";
        patch.error ??= "";
      }
    }
    this.publish(patch);
  }

  private markRead(seq?: number) {
    if (!seq || seq <= this.marked) return;
    this.marked = seq;
    const signal = this.life.signal;
    void nexusPost(`${this.base}/read`, { seq }, signal).catch(() => {
      if (!signal.aborted && this.marked === seq) this.marked = 0;
    });
  }

  private receive(event: AgentEvent, messageId: string, cursor?: string) {
    if (!messageId || event.type === "CUSTOM") return;
    let entry = this.entries.get(messageId);
    if (!entry && this.kind === "private") {
      // 同一 SSE 内不同回复各自归并。尚未收到开始帧的增量也按 reply_id 保留。
      entry = this.privateEntry({
        id: messageId,
        role: event.type === "REPLY_START" && event.role === "user" ? "user" : "assistant",
        name: this.privateEmployee!.name,
        created_at: event.created_at,
        content: [],
        metadata: {},
        finished_at: null,
      });
      this.put(entry);
    }
    if (!entry) return;
    // 已落盘的完整消息可能仍包含在上游 replay log 中，不能再次拼接增量。
    if (entry.native.finished_at) return;
    let seen = this.events.get(messageId);
    if (!seen) {
      seen = new ReplyEvents();
      this.events.set(messageId, seen);
    }
    if (cursor) {
      if (Number(entry.native.metadata.stream_cursor || 0) >= Number(cursor)) return;
      entry.native.metadata.stream_cursor = Number(cursor);
    }
    if (!seen.apply(entry.native, event)) return;
    if (event.type === "REPLY_END") {
      this.events.delete(messageId);
      if (this.kind === "private") {
        entry.awaitingSnapshot = true;
        this.reconcilePrivate();
      }
    }
    this.dirty.add(messageId);
    if (entry.sender.senderKind === "employee") {
      if (this.kind === "private") {
        this.activeReply = messageId;
        this.syncing = event.type === "REPLY_END";
        this.busy = true;
      } else if (this.busy && entry.native.metadata.discussion_id === this.discussion) {
        this.activeReply = messageId;
      }
    }
    this.schedule(event.type.endsWith("_DELTA"));
  }

  private revoke(reason: string) {
    this.enabled = this.busy = this.syncing = this.stopping = false;
    this.entries.clear();
    this.views.clear();
    this.events.clear();
    this.dirty.clear();
    this.ordered = [];
    this.failed = undefined;
    this.before = null;
    this.errorSource = "other";
    this.publish({ ...empty, loading: false, error: reason });
    this.life.abort();
    this.stream?.abort();
    clearTimeout(this.peerTimer);
    clearTimeout(this.reconcileTimer);
    this.onMembersChanged?.();
  }

  private reconcilePrivate() {
    if (this.reconcileTimer !== undefined) return;
    this.reconcileTimer = setTimeout(() => {
      this.reconcileTimer = undefined;
      if (!this.life.signal.aborted && (this.syncing || this.stopping)) this.follow();
    }, RECONCILE_INTERVAL);
  }

  // 主员工的 SSE 不含其他标签提交的用户消息，也不含同事邮箱正文。
  // 回复开始时读取一次，仅在有未回复的协作时继续查询。
  private refreshParticipants(signal: AbortSignal, delayMs = 5000) {
    clearTimeout(this.peerTimer);
    this.peerTimer = setTimeout(async () => {
      if (signal.aborted) return;
      try {
        const page = await nexusGet<PrivatePage>(`${this.base}/messages?limit=50`, signal);
        if (signal.aborted) return;
        for (const native of page.messages) {
          if ((native.role === "user" || native.metadata?.peer_employee_id) && !this.entries.has(native.id)) {
            this.put(this.privateEntry(native));
          }
        }
        if (this.dirty.size) this.publish();
        if (page.pending_peer_replies) this.refreshParticipants(signal);
      } catch (error) {
        if (signal.aborted) return;
        if (error instanceof ConversationHttpError && [401, 403, 404].includes(error.status))
          this.revoke(error.message);
        else this.refreshParticipants(signal);
      }
    }, delayMs);
  }

  private async listen(signal: AbortSignal) {
    if (this.kind === "private") {
      if (!this.sessionId) return;
      type PrivateEvent = AgentEvent | { type: "NEXUS_UPSTREAM_ERROR"; detail: string };
      const params = this.cursor ? "?" + new URLSearchParams({ after: this.cursor }) : "";
      for await (const { data } of readFrames<PrivateEvent>(`${this.base}/stream${params}`, signal)) {
        if (signal.aborted) return;
        if (data.type === "NEXUS_UPSTREAM_ERROR") throw new Error(data.detail || "员工回复连接中断");
        if (!("reply_id" in data)) continue;
        this.receive(data, data.reply_id);
        if (
          (data.type === "REPLY_START" && !this.state.sending) ||
          (data.type === "TOOL_RESULT_END" &&
            this.entries
              .get(data.reply_id)
              ?.native.content.some(
                (part) =>
                  part.type === "tool_call" &&
                  part.id === data.tool_call_id &&
                  part.name === "contact_employee",
              ))
        )
          this.refreshParticipants(signal, 0);
      }
    } else {
      const checkpoint = Number(this.cursor);
      const seen = new Set<string>();
      for await (const { data, cursor } of readFrames<ChannelEvent>(
        `${this.base}/events?after=${this.cursor}`,
        signal,
      )) {
        if (signal.aborted) return;
        if (this.errorSource === "connect") {
          this.errorSource = "";
          this.publish({ error: "" });
        }
        if (data.type === "revoked") {
          this.revoke("你已退出群组或登录已失效");
          return;
        }
        if (!cursor || Number(cursor) <= checkpoint || seen.has(cursor)) continue;
        if (Number(cursor) <= Number(this.cursor)) {
          this.follow();
          return;
        }
        seen.add(cursor);
        if (seen.size > 256) seen.delete(seen.values().next().value!);
        if (data.type === "delta") {
          // 分页以外的回复由快照提供。推进游标，继续接收当前窗口的消息。
          if (!this.before || data.seq >= Number(this.before) || this.entries.has(data.message_id))
            this.receive(data.event, data.message_id, cursor);
        } else if (data.type === "discussion-worker") {
          if (this.busy && data.id === this.discussion) this.setActiveEmployee(data.active_employee_id);
        } else if (data.type === "message") {
          const boundary = Number(this.before);
          if (!boundary || data.item.seq >= boundary || this.entries.has(data.item.message.id))
            this.putGroup(data.item);
          this.markRead(data.item.seq);
        } else if (data.type === "members") {
          this.onMembersChanged?.();
          this.follow();
          return;
        } else if (data.type === "discussion") {
          const currentRunning = ["running", "superseded"].includes(this.discussionState);
          if (!this.discussion || data.id === this.discussion || data.state === "running" ||
            (data.state === "queued" && !currentRunning)) {
            this.setDiscussion(data);
            if (this.errorSource !== "send") {
              this.publish({ error: data.detail || (data.state === "failed" ? "讨论失败" : "") });
            }
            // 完成旧讨论时，快照可能还有下一轮待执行。
            if (!this.busy) {
              this.cursor = cursor;
              this.follow();
              return;
            }
          }
        }
        this.cursor = cursor;
        if (data.type !== "delta") this.schedule();
      }
    }
  }

  private follow() {
    this.stream?.abort();
    clearTimeout(this.peerTimer);
    clearTimeout(this.reconcileTimer);
    this.reconcileTimer = undefined;
    this.stream = new AbortController();
    const signal = this.stream.signal;
    void (async () => {
      let needSnapshot = true;
      while (!signal.aborted) {
        try {
          if (needSnapshot || this.kind === "private") await this.page(signal);
          needSnapshot = false;
          if (signal.aborted || (this.kind === "private" && !this.sessionId)) return;
          await this.listen(signal);
          if (signal.aborted || (!this.enabled && this.kind === "group")) return;
          throw new Error("实时连接中断，正在重连");
        } catch (error) {
          if (signal.aborted) return;
          if (error instanceof ConversationHttpError && [401, 403, 404].includes(error.status)) {
            this.revoke(error.message);
            return;
          }
          if (!this.failed || this.failed.editing) {
            this.errorSource = "connect";
            this.publish({ loading: false, error: detail(error) });
          }
          if (!this.hasSnapshot) return;
        }
        await delay(signal);
      }
    })();
  }

  loadOlder = async () => {
    if (!this.before || this.state.loadingOlder) return;
    const signal = this.life.signal;
    this.publish({ loadingOlder: true });
    try {
      await this.page(signal, true);
    } catch (error) {
      if (!signal.aborted) this.publish({ error: detail(error) });
    } finally {
      if (!signal.aborted) this.publish({ loadingOlder: false });
    }
  };

  private pendingSendKey() {
    return `nexus:pending-send:${this.viewer}:${this.base}`;
  }

  private readPendingSend(): Attempt | undefined {
    try {
      const attempt = JSON.parse(sessionStorage.getItem(this.pendingSendKey()) || "null");
      if (
        typeof attempt?.text === "string" &&
        Array.isArray(attempt.files) &&
        typeof attempt.clientId === "string"
      ) return attempt;
    } catch {
      /* 标签存储不可用 */
    }
  }

  send = async (text: string, files: UploadedFile[]): Promise<SendOutcome> => {
    if (!this.state.canSend) return "rejected";
    const pending = this.failed;
    const sameMessage = pending?.text === text && pending.files.length === files.length &&
      files.every((file, index) => file.id === pending.files[index].id);
    return this.deliver(text, files, sameMessage ? pending : undefined);
  };

  private async deliver(text: string, files: UploadedFile[], previous?: Attempt): Promise<SendOutcome> {
    if (!this.enabled || this.state.sending) return "rejected";
    const attachmentIds = files.map((file) => file.id);
    if (!text.trim() && !attachmentIds.length) return "rejected";
    if (attachmentIds.length > 6) {
      this.errorSource = "send";
      this.publish({ error: "每条消息最多添加 6 个附件" });
      return "rejected";
    }
    const signal = this.life.signal;
    const draft = {
      text,
      files,
      clientId: previous?.clientId ?? crypto.randomUUID(),
    };
    this.writePendingSend(draft);
    this.errorSource = "";
    this.publish({ sending: true, error: "" });
    try {
      const payload = {
        content: text ? [{ type: "text", text }] : [],
        attachment_ids: attachmentIds,
        client_id: draft.clientId,
      };
      if (this.kind === "private") {
        type Receipt = {
          message: {
            id: string;
            created_at: string;
            attachments: { id: string; name: string; type: string }[];
          };
          agentscope: { session_id: string };
        };
        const result = await nexusPost<Receipt>(`${this.base}/messages`, payload, signal);
        if (signal.aborted) return "unconfirmed";
        this.sessionId = result.agentscope.session_id;
        const receipt = result.message;
        if (!this.entries.has(receipt.id))
          this.put(
            this.privateEntry({
              id: receipt.id,
              role: "user",
              name: this.viewer,
              created_at: receipt.created_at,
              metadata: {},
              content: [
                { type: "text", id: receipt.id, created_at: receipt.created_at, text },
                ...receipt.attachments.map((file) => ({
                  type: "data" as const,
                  id: file.id,
                  name: file.name,
                  created_at: receipt.created_at,
                  source: {
                    type: "url" as const,
                    url: `/api/v1/attachments/${file.id}`,
                    media_type: file.type,
                  },
                })),
              ],
            }),
          );
        this.busy = true;
        this.follow();
      } else {
        const result = await nexusPost<{ message: GroupItem }>(`${this.base}/messages`, payload, signal);
        if (signal.aborted) return "unconfirmed";
        this.putGroup(result.message);
        this.markRead(result.message.seq);
      }
      this.failed = undefined;
      this.clearPendingSend();
      return "accepted";
    } catch (error) {
      if (signal.aborted) return "unconfirmed";
      const rejected = !previous && error instanceof ConversationHttpError &&
        error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status);
      if (rejected) {
        this.failed = undefined;
        this.clearPendingSend();
      } else {
        this.failed = draft;
      }
      this.errorSource = "send";
      this.publish({ error: rejected ? detail(error) : `投递结果未确认 · ${detail(error)}` });
      return rejected ? "rejected" : "unconfirmed";
    } finally {
      if (!signal.aborted) this.publish({ sending: false });
    }
  }

  retry = async () => {
    if (this.failed && !this.failed.editing)
      return this.deliver(this.failed.text, this.failed.files, this.failed);
    this.follow();
  };

  restorePendingDraft = () => {
    const pending = this.failed;
    if (!pending || pending.editing || this.state.sending) return;
    this.failed = { ...pending, editing: true };
    this.writePendingSend(this.failed);
    this.dismissError();
    return { text: pending.text, files: pending.files };
  };

  dismissError = () => {
    this.errorSource = "";
    this.publish({ error: "" });
  };

  private writePendingSend(attempt: Attempt) {
    try {
      sessionStorage.setItem(this.pendingSendKey(), JSON.stringify(attempt));
    } catch {
      /* 标签存储不可用 */
    }
  }

  private clearPendingSend() {
    try {
      sessionStorage.removeItem(this.pendingSendKey());
    } catch {
      /* 标签存储不可用 */
    }
  }

  stop = async () => {
    if (!this.state.canStop) return;
    const signal = this.life.signal;
    this.stopping = true;
    this.publish({ error: "" });
    try {
      await nexusPost(`${this.base}/interrupt`, {}, signal);
      if (!signal.aborted) this.follow();
    } catch (error) {
      if (!signal.aborted) {
        this.stopping = false;
        this.publish({ error: detail(error) });
      }
    }
  };
}
