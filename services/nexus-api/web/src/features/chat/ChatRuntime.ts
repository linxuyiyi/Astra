import type { Attachment, ChatMessage } from "@/types";
import { uploadAttachment } from "@/api";
import type {
  ChatAdapter,
  ChatAdapterEvent,
  ChatSessionState,
  ReplyPhase,
} from "./types";
import { mergeMessages, sameIds } from "./sessionState";
import { shortText } from "./shortText";

/**
 * 同文档聊天运行时：经 adapter 走 Nexus conversations/channels，
 * 负责历史、发送、流式、停流与 error→shortText。不直连 agentscope-api。
 */
export class ChatRuntime {
  constructor(
    private readonly state: ChatSessionState,
    private readonly isActive: () => boolean = () => true,
  ) {}

  get phase(): ReplyPhase {
    return this.state.phase;
  }

  get streaming(): boolean {
    return ["queued", "streaming", "interrupting"].includes(this.state.phase);
  }

  bind(adapter: ChatAdapter) {
    this.stopFollow();
    this.state.generation += 1;
    this.state.adapter = adapter;
    this.state.status = this.state.messages.length ? "ready" : "loading";
    this.state.error = "";
    this.state.notice = "";
    this.state.failedSend = null;
    this.state.noticeSource = "";
    this.state.phase = "idle";
    this.ensureFollow();
  }

  suspend() { this.stopFollow(); this.state.generation += 1; }

  resume() { this.ensureFollow(); }

  dispose() {
    this.stopFollow();
    this.state.generation += 1;
  }

  private followAlive = false;
  private eventRevision = 0;

  private stopFollow() {
    this.state.abort?.abort();
    this.state.abort = null;
    this.followAlive = false;
  }

  private startFollow() {
    const adapter = this.state.adapter;
    if (!adapter) return;
    this.stopFollow();
    const generation = ++this.state.generation;
    const abort = new AbortController();
    this.state.abort = abort;
    this.followAlive = true;
    const emit = (event: ChatAdapterEvent) => {
      if (abort.signal.aborted || this.state.generation !== generation || this.state.adapter !== adapter) return;
      if (event.type !== "notice") {
        this.eventRevision += 1;
        if (this.state.noticeSource === "follow") this.clearNotice();
      }
      if (event.type === "activity") {
        this.state.phase = event.phase;
      } else if (event.type === "unavailable") {
        this.setError(event.text);
        this.stopFollow();
      } else if (event.type === "messages") {
        this.state.status = "ready";
        const before = this.state.messages.length;
        this.state.messages = mergeMessages(this.state.messages, event.messages);
        this.updateSummary();
        if (!this.isActive() && this.state.messages.length > before) {
          this.bumpUnread(this.state.messages.length - before);
        }
      } else if (event.type === "notice") {
        this.state.notice = shortText(event.text);
        this.state.noticeSource = event.source || "other";
      } else if (event.type === "finished") {
        this.state.phase = "idle";
        if (this.state.noticeSource === "stop") this.clearNotice();
      }
    };
    void adapter.follow(abort.signal, emit).catch((error) => {
      if (abort.signal.aborted || this.state.generation !== generation) return;
      this.followAlive = false;
      if (this.state.status === "loading") this.setError(error);
      this.state.notice = shortText(error);
      this.state.noticeSource = "follow";
      this.state.failedSend = null;
    }).finally(() => {
      if (this.state.generation === generation && this.state.abort === abort) {
        this.followAlive = false;
      }
    });
  }

  /** 发送后若 follow 已退出，立即重连，避免 phase=streaming 却收不到 delta。 */
  private ensureFollow() {
    if (!this.state.adapter || !this.isActive()) return;
    if (this.followAlive && this.state.abort && !this.state.abort.signal.aborted) return;
    this.startFollow();
  }

  private updateSummary() {
    const last = [...this.state.messages].reverse().find((item) => item.text?.trim());
    this.state.lastSummary = last?.text?.trim().replace(/\s+/g, " ").slice(0, 48) ?? "";
  }

  markRead() {
    this.state.unread = 0;
  }

  bumpUnread(count = 1) {
    this.state.unread = Math.max(0, this.state.unread + count);
  }

  async send(text: string, files: Attachment[], requestId: string = crypto.randomUUID()): Promise<boolean> {
    const adapter = this.state.adapter;
    if (!adapter || this.state.sending) return false;
    const trimmed = text.trim();
    if (!trimmed && !files.length) return false;
    this.state.sending = true;
    this.state.notice = "";
    this.state.noticeSource = "";
    const sentDraft = this.state.draft;
    const revision = this.eventRevision;
    const snapshotFiles = [...files];
    const fileIds = snapshotFiles.map((item) => item.id);
    try {
      const result = await adapter.send(trimmed, fileIds, new AbortController().signal, requestId);
      this.state.messages = mergeMessages(this.state.messages, [result.message, ...result.messages]);
      this.updateSummary();
      if (this.state.draft === sentDraft) this.state.draft = "";
      if (sameIds(this.state.draftFiles.map((item) => item.id), fileIds)) this.state.draftFiles = [];
      this.state.failedSend = null;
      if (result.notice) {
        this.state.notice = shortText(result.notice);
        this.state.noticeSource = "other";
      }
      if (this.eventRevision === revision) this.state.phase = result.activity;
      this.ensureFollow();
      return true;
    } catch (error) {
      this.state.failedSend = { text: trimmed, files: snapshotFiles, requestId };
      this.state.notice = shortText(error);
      this.state.noticeSource = "send";
      return false;
    } finally {
      this.state.sending = false;
    }
  }

  /** 用失败时保存的同 payload 再发一次。 */
  async retryFailedSend(): Promise<boolean> {
    const failed = this.state.failedSend;
    if (!failed) return false;
    // 把失败稿还原进输入区，再走 send，保证 UI 与 payload 一致。
    this.state.draft = failed.text;
    this.state.draftFiles = [...failed.files];
    return this.send(failed.text, failed.files, failed.requestId);
  }

  clearNotice() {
    this.state.notice = "";
    this.state.noticeSource = "";
  }

  async stop(): Promise<void> {
    const adapter = this.state.adapter;
    if (!adapter) return;
    const previousPhase = this.state.phase;
    const revision = this.eventRevision;
    this.state.phase = "interrupting";
    try {
      const result = await adapter.interrupt();
      this.state.messages = mergeMessages(this.state.messages, result.messages);
      if (this.state.noticeSource === "stop") this.clearNotice();
      if (this.eventRevision === revision && result.activity === "idle") this.state.phase = "idle";
      // 仍在执行时只表示已请求停止；终态由服务事件确认，不设时间兜底。
    } catch (error) {
      // 停流失败：phase 回到 streaming/idle，停止钮可再点；notice 仅提示。
      this.state.notice = shortText(error, "停止失败，请重试");
      this.state.noticeSource = "stop";
      this.state.failedSend = null;
      if (this.eventRevision === revision) this.state.phase = previousPhase;
    }
  }

  async attach(files: FileList | File[]): Promise<void> {
    const list = Array.from(files);
    if (!list.length) return;
    if (!this.state.draftPending) this.state.draftPending = [];
    if (this.state.draftFiles.length + this.state.draftPending.length + list.length > 6) {
      this.state.notice = "每条消息最多添加 6 个附件";
      this.state.noticeSource = "attach";
      this.state.failedSend = null;
      return;
    }
    const room = 6 - this.state.draftFiles.length - this.state.draftPending.length;
    const batch = list.slice(0, room).map((file) => ({
      file,
      pending: { id: `pending-${crypto.randomUUID()}`, name: file.name },
    }));
    this.state.draftPending = [...this.state.draftPending, ...batch.map((item) => item.pending)];
    this.state.uploading = true;
    try {
      for (const item of batch) {
        try {
          const attachment = await uploadAttachment(item.file);
          this.state.draftFiles = [...this.state.draftFiles, attachment];
        } finally {
          this.state.draftPending = this.state.draftPending.filter((pending) => pending.id !== item.pending.id);
        }
      }
    } catch (error) {
      this.state.notice = shortText(error, "附件上传失败");
      this.state.noticeSource = "attach";
      this.state.failedSend = null;
    } finally {
      this.state.uploading = this.state.draftPending.length > 0;
    }
  }

  removeDraftFile(id: string) {
    this.state.draftFiles = this.state.draftFiles.filter((item) => item.id !== id);
  }


  get hasOlder() { return this.state.adapter?.hasOlder ?? false; }

  async loadOlder() {
    const adapter = this.state.adapter;
    if (!adapter?.loadOlder || this.state.loadingOlder) return;
    const generation = this.state.generation;
    this.state.loadingOlder = true;
    try {
      const messages = await adapter.loadOlder();
      if (this.state.generation === generation) this.state.messages = mergeMessages(this.state.messages, messages);
    } catch (error) {
      this.state.notice = shortText(error, "较早消息读取失败，请重试");
    } finally { this.state.loadingOlder = false; }
  }

  setError(error: unknown) {
    this.state.status = "error";
    this.state.error = shortText(error);
  }
}

export function createSessionState(target: ChatSessionState["target"]): ChatSessionState {
  return {
    key: target.key,
    target,
    adapter: null,
    status: "loading",
    error: "",
    messages: [],
    notice: "",
    failedSend: null,
    noticeSource: "",
    draft: "",
    draftFiles: [],
    draftPending: [],
    loadingOlder: false,
    uploading: false,
    sending: false,
    phase: "idle",
    lastSummary: "",
    unread: 0,
    tasks: [],
    tasksLoading: false,
    taskError: "",
    taskGeneration: 0,
    generation: 0,
    abort: null,
    tab: "tasks",
  };
}

export function imageFile(type: string) {
  return type.startsWith("image/");
}

export type { ChatMessage };
