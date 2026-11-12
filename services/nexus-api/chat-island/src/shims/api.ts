/**
 * 替换上游 `@/api`。
 *
 * 上游 `useMessages` 从 `sessionApi` 读历史、订阅事件流、停止，并通过 `chatApi.trigger`
 * 发送与确认。这里把三者都落到 Nexus 的会话入口：历史用原生消息，流用 raw 透传，
 * 发送与停止经 Nexus 校验归属后转发。上游 `useMessages.ts` 因此保持逐字节不变。
 */
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { Msg } from "@agentscope-ai/agentscope/message";

import { attachmentIdFromBlock } from "@/nexus/attachments";
import { nexusFetch, nexusGet } from "@/nexus/http";

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

interface NativeHistory {
  messages: Msg[];
  event_cursor: string;
  is_running: boolean;
}

interface SendResponse {
  message: { id: string };
  agentscope: { session_id: string };
}

interface ContentBlockLike {
  type: string;
  id?: string;
  text?: string;
  metadata?: Record<string, unknown>;
  source?: { url?: string };
}

/** 首次发送后 Nexus 才会建 Session；岛入口用它更新绑定并重挂载。 */
let onBound: ((sessionId: string) => void) | null = null;

/**
 * 本标签页刚建出来的 Session。POST 返回时用户输入可能还没落盘，此时拉历史会
 * 拿到空列表并把乐观气泡换掉；hook 据此跳过历史拉取。
 *
 * 只记最后一条：岛页面绑定一条 conversation，重新挂载（含 StrictMode 的
 * 双次 effect）也必须保持同一判断，所以这里不做一次性消费。
 */
let freshlyCreated: string | null = null;

/** 标记“这条 Session 是本标签页刚建的”。 */
export function markFreshlyCreated(sessionId: string): void {
  freshlyCreated = sessionId || null;
}

/** 该 Session 是否由本标签页刚建；是则历史可以合法为空。 */
export function isFreshlyCreated(sessionId: string): boolean {
  return Boolean(sessionId) && freshlyCreated === sessionId;
}

/**
 * 发送时用的是本地临时 id，落库后由后端换成消息 id。历史合并要按这个映射
 * 对账，否则同一条消息会在“乐观气泡 + 原生历史”里各出现一次。
 */
const sentMessageIds = new Map<string, string>();

/** 本地消息 id → 服务端消息 id；没有映射时原样返回。 */
export function resolveMessageId(localId: string): string {
  return sentMessageIds.get(localId) ?? localId;
}

export function onSessionBound(listener: (sessionId: string) => void): () => void {
  onBound = listener;
  return () => {
    if (onBound === listener) onBound = null;
  };
}

function conversationId(): string {
  const value = new URLSearchParams(window.location.search).get("conversation") ?? "";
  if (!value) throw new Error("缺少 conversation 参数");
  return value;
}

function detailOf(payload: unknown, fallback: string): string {
  if (payload && typeof payload === "object" && "detail" in payload) {
    const detail = (payload as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
  }
  return fallback;
}

async function errorDetail(response: Response): Promise<string> {
  try {
    return detailOf(await response.json(), response.statusText);
  } catch {
    return response.statusText || "请求失败";
  }
}

function toSendBody(input: unknown) {
  if (!input || typeof input !== "object" || Array.isArray(input)) {
    throw new Error("暂不支持这种输入类型");
  }
  const content = (input as { content?: ContentBlockLike[] }).content;
  if (!Array.isArray(content)) throw new Error("消息没有内容块");
  const blocks = content
    .filter((block) => block.type === "text")
    .map(({ type, text, id }) => ({ type, text, id }));
  const attachmentIds = content
    .filter((block) => block.type === "data")
    .map((block) => attachmentIdFromBlock(block))
    .filter((value): value is string => Boolean(value));
  return { content: blocks, attachment_ids: attachmentIds };
}

export const sessionApi = {
  async messages(_sessionId: string, _agentId: string): Promise<MessagesResponse> {
    const payload = await nexusGet<NativeHistory>(
      `/api/v1/conversations/${conversationId()}/messages`,
    );
    return {
      messages: payload.messages ?? [],
      event_cursor: payload.event_cursor || null,
      is_running: Boolean(payload.is_running),
      has_more: false,
    };
  },

  async *streamEvents(
    _sessionId: string,
    _agentId: string,
    signal?: AbortSignal,
    after?: string | null,
  ): AsyncGenerator<AgentEvent> {
    if (!signal) return;
    const query = new URLSearchParams();
    if (after) query.set("after", after);
    let response: Response;
    try {
      response = await nexusFetch(
        `/api/v1/conversations/${conversationId()}/stream?${query.toString()}`,
        { signal },
      );
    } catch {
      return; // 连接建立失败或已中止：没有可消费的事件。
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
  },

  async interrupt(_sessionId?: string, _agentId?: string): Promise<void> {
    const response = await nexusFetch(`/api/v1/conversations/${conversationId()}/interrupt`, {
      method: "POST",
    });
    if (!response.ok) throw new Error(await errorDetail(response));
  },
};

const pendingSends = new Map<string, { fingerprint: string; clientId: string }>();

export const chatApi = {
  async trigger(body?: unknown): Promise<{ status: string; session_id: string }> {
    const input = (body as { input?: unknown } | undefined)?.input;
    const localId = (input as { id?: string } | null | undefined)?.id ?? "";
    if (!localId) throw new Error("消息缺少发送标识");
    const payload = toSendBody(input);
    const conversation = conversationId();
    const fingerprint = JSON.stringify([payload.content.map(block => block.text), payload.attachment_ids]);
    const pending = pendingSends.get(conversation);
    const clientId = pending?.fingerprint === fingerprint ? pending.clientId : localId;
    pendingSends.set(conversation, { fingerprint, clientId });
    const response = await nexusFetch(`/api/v1/conversations/${conversation}/messages`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...payload, client_id: clientId }),
    });
    if (!response.ok) throw new Error(await errorDetail(response));
    const result = (await response.json()) as SendResponse;
    if (pendingSends.get(conversation)?.clientId === clientId) pendingSends.delete(conversation);
    const sessionId = result.agentscope.session_id;
    if (sessionId) onBound?.(sessionId);
    sentMessageIds.set(localId, result.message.id);
    return { status: "accepted", session_id: sessionId };
  },
};
