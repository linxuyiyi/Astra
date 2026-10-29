import type { Attachment, ChatMessage } from "@/types";
import { uploadAttachment } from "@/api";
import type {
  ChatAdapter,
  ChatAdapterEvent,
  ChatSessionState,
  LiveLine,
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
    return this.state.phase === "streaming" || this.state.liveLines.length > 0;
  }

  bind(adapter: ChatAdapter) {
    this.stopFollow();
    this.state.adapter = adapter;
    this.state.status = "ready";
    this.state.error = "";
    this.state.notice = "";
    this.state.failedSend = null;
    this.state.noticeSource = "";
    this.state.phase = "idle";
    this.startFollow();
  }

  dispose() {
    this.stopFollow();
    this.state.generation += 1;
  }

  private followAlive = false;

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
      if (this.state.generation !== generation || this.state.adapter !== adapter) return;
      if (event.type === "messages") {
        const before = this.state.messages.length;
        this.state.messages = mergeMessages(this.state.messages, event.messages);
        this.updateSummary();
        // 历史刷新不等于停流：仅当没有 live 行且不在 interrupting 时回到 idle。
        if (this.state.liveLines.length === 0 && this.state.phase !== "interrupting" && this.state.phase !== "streaming") {
          this.state.phase = "idle";
        }
        if (!this.isActive() && this.state.messages.length > before) {
          this.bumpUnread(this.state.messages.length - before);
        }
      } else if (event.type === "live") {
        this.state.liveLines = event.lines;
        this.state.phase = event.lines.length ? "streaming" : this.state.phase === "interrupting" ? "interrupting" : "idle";
        if (event.lines.length) {
          this.state.notice = "";
          this.state.noticeSource = "";
        }
        if (!this.isActive() && event.lines.length) this.bumpUnread(1);
      } else if (event.type === "notice") {
        this.state.notice = shortText(event.text);
        this.state.noticeSource = this.state.noticeSource || "other";
      } else if (event.type === "finished") {
        this.state.phase = "idle";
        this.state.liveLines = [];
      }
    };
    void adapter.follow(abort.signal, emit).catch((error) => {
      if (abort.signal.aborted || this.state.generation !== generation) return;
      this.followAlive = false;
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
    if (!this.state.adapter) return;
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

  async send(text: string, files: Attachment[]): Promise<boolean> {
    const adapter = this.state.adapter;
    if (!adapter || this.state.sending) return false;
    const trimmed = text.trim();
    if (!trimmed && !files.length) return false;
    this.state.sending = true;
    this.state.notice = "";
    this.state.noticeSource = "";
    const sentDraft = this.state.draft;
    const snapshotFiles = [...files];
    const fileIds = snapshotFiles.map((item) => item.id);
    try {
      const result = await adapter.send(trimmed, fileIds, new AbortController().signal);
      this.state.messages = mergeMessages(this.state.messages, [result.message]);
      this.updateSummary();
      if (this.state.draft === sentDraft) this.state.draft = "";
      if (sameIds(this.state.draftFiles.map((item) => item.id), fileIds)) this.state.draftFiles = [];
      this.state.failedSend = null;
      if (result.notice) {
        this.state.notice = shortText(result.notice);
        this.state.noticeSource = "other";
      }
      this.state.phase = "streaming";
      this.ensureFollow();
      return true;
    } catch (error) {
      this.state.failedSend = { text: trimmed, files: snapshotFiles };
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
    return this.send(failed.text, failed.files);
  }

  clearNotice() {
    this.state.notice = "";
    this.state.noticeSource = "";
  }

  async stop(): Promise<void> {
    const adapter = this.state.adapter;
    if (!adapter) return;
    const generation = this.state.generation;
    this.state.phase = "interrupting";
    try {
      await adapter.interrupt();
      // 若终止事件丢失，10s 后松开 interrupting，避免停止按钮永久 loading。
      window.setTimeout(() => {
        if (this.state.generation !== generation) return;
        if (this.state.phase === "interrupting") {
          this.state.phase = this.state.liveLines.length ? "streaming" : "idle";
        }
      }, 10_000);
    } catch (error) {
      // 停流失败：phase 回到 streaming/idle，停止钮可再点；notice 仅提示。
      this.state.notice = shortText(error, "停止失败，请重试");
      this.state.noticeSource = "stop";
      this.state.failedSend = null;
      this.state.phase = this.state.liveLines.length ? "streaming" : "idle";
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
          if (!this.isActive()) return;
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


  /**
   * 壳内 HITL 选择：只更新 live/history 工具态，不另开 AgentWork/PR#12 通道。
   * 允许 / 允许并记住 → allowed；拒绝 → finished。卡片随 state 离开 asking。
   */
  resolveHitl(toolId: string, choice: "yes" | "yes_with_rule" | "no") {
    if (!toolId) return;
    const next = choice === "no" ? "finished" : "allowed";
    let touched = false;
    this.state.liveLines = this.state.liveLines.map((line) => {
      let changed = false;
      const tools = line.tools.map((tool) => {
        if (tool.id !== toolId || tool.state !== "asking") return tool;
        changed = true;
        touched = true;
        return { ...tool, state: next };
      });
      return changed ? { ...line, tools } : line;
    });
    this.state.messages = this.state.messages.map((message) => {
      const tools = message.tools;
      if (!tools?.some((tool) => tool.id === toolId && tool.state === "asking")) return message;
      touched = true;
      return {
        ...message,
        tools: tools.map((tool) =>
          tool.id === toolId && tool.state === "asking" ? { ...tool, state: next } : tool,
        ),
      };
    });
    if (touched && this.state.liveLines.every((line) => !line.tools.some((tool) => tool.state === "asking"))) {
      // 无其它待确认时保持当前 phase；不强制 idle，避免打断仍在流式的文本。
    }
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
    liveLines: [],
    notice: "",
    failedSend: null,
    noticeSource: "",
    draft: "",
    draftFiles: [],
    draftPending: [],
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

export type { ChatMessage, LiveLine };
