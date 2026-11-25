import { appendEvent, type DataBlock, type Msg } from "@agentscope-ai/agentscope/message";
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import type { ThreadMessageLike } from "@assistant-ui/react";

export type SenderKind = "employee" | "human" | "system";
export interface Sender {
  senderId: string;
  senderName: string;
  senderKind: SenderKind;
  avatar: string;
  self: boolean;
}
export interface TimelineEntry {
  native: Msg;
  sender: Sender;
  sequence?: number;
  delivery?: "sent";
  awaitingSnapshot?: boolean;
}
export type ConversationPhase = "idle" | "waiting" | "streaming" | "tool" | "approval" | "syncing" | "stopping";
type Part = Exclude<ThreadMessageLike["content"], string>[number];
type ToolPart = Extract<Part, { type: "tool-call" }>;

export interface ConversationTaskRef {
  taskId: string;
  status?: string;
  text?: string;
  workerName?: string;
  progress?: string;
  waitingReason?: string;
}

export interface ConversationTaskCallback {
  taskId: string;
  status?: string;
  statusLabel: string;
  eventId?: string;
  reason?: string;
  rawText: string;
}

export interface ConversationToolResult {
  state: string;
  output: string;
  summary?: string;
  task?: ConversationTaskRef;
}

export const TASK_TOOL_LABELS: Record<string, string> = {
  nexus_task_create: "委派任务",
  nexus_task_categories: "查询任务分类",
  nexus_task_get: "查询任务状态",
  nexus_task_list: "查看任务列表",
  nexus_task_update: "更新任务内容",
  nexus_task_assign: "分配 Worker",
  nexus_task_start: "启动任务执行",
  nexus_task_pause: "暂停任务",
  nexus_task_resume: "恢复任务执行",
  nexus_task_terminate: "终止任务",
  nexus_task_retry: "重新执行任务",
  nexus_task_logs: "查看任务事件",
  nexus_task_result: "读取执行结果",
  nexus_task_summary: "查看复盘总结",
  nexus_task_intervene: "转人工介入",
  nexus_task_assistance: "读取执行端求助",
  nexus_task_reply: "回复执行端请求",
  nexus_task_input: "补充执行指令",
  nexus_task_confirm: "确认任务完成",
};

const TASK_STATUS_LABELS: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING: "待介入",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCELED: "已取消",
};

const LABEL_TO_TASK_STATUS: Record<string, string> = {
  待执行: "PENDING",
  待领取: "PENDING",
  执行中: "RUNNING",
  运行中: "RUNNING",
  待介入: "WAITING",
  待处理: "WAITING",
  等待处理: "WAITING",
  等待人工处理: "WAITING",
  等待中: "WAITING",
  等待: "WAITING",
  已完成: "SUCCEEDED",
  成功: "SUCCEEDED",
  失败: "FAILED",
  执行失败: "FAILED",
  已取消: "CANCELED",
  已终止: "CANCELED",
};

export function shortToolName(toolName: string): string {
  return toolName.split("__").at(-1) || toolName;
}

export function taskToolLabel(toolName: string): string | undefined {
  return TASK_TOOL_LABELS[shortToolName(toolName)];
}

export function parseTaskWakeHint(source: string, text: string): ConversationTaskCallback | undefined {
  if (source.trim().toLowerCase() !== "nexus-task") return undefined;
  const trimmed = text.trim();
  const match = trimmed.match(/^任务\s+([^\s，。；（）()]+)\s+状态变为([^\s，。；（）()]+)(?:（事件\s*([^）]+)）)?[。.]?(.*)$/s);
  if (!match) {
    const idMatch = trimmed.match(/\b(T-\d{8}-[A-Z0-9]+)\b/i);
    if (!idMatch) return undefined;
    return {
      taskId: idMatch[1],
      statusLabel: "状态更新",
      rawText: trimmed,
    };
  }
  const [, taskId, rawLabel, eventId, rest] = match;
  const status = LABEL_TO_TASK_STATUS[rawLabel];
  const reasonMatch = rest.match(/原因[：:]\s*([^。\n]+)[。.]?/);
  return {
    taskId,
    status,
    statusLabel: status ? TASK_STATUS_LABELS[status] : rawLabel,
    eventId: eventId?.trim() || undefined,
    reason: reasonMatch?.[1]?.trim() || undefined,
    rawText: trimmed,
  };
}

function toolResult(name: string, state: string, output: string, textItems: string[]): ConversationToolResult {
  const result: ConversationToolResult = { state, output };
  const firstLine = output.trim().split("\n", 1)[0];
  if (firstLine && !/^[{\[]/.test(firstLine)) result.summary = firstLine.slice(0, 160);
  const shortName = shortToolName(name);
  const task = shortName.startsWith("nexus_task_");
  const librarySearch = shortName === "library_search";
  const libraryRead = shortName === "library_get";
  if (state === "running" || (!task && !librarySearch && !libraryRead && shortName !== "contact_employee"))
    return result;
  try {
    if (state === "success" && librarySearch && !result.summary) {
      // 完整 MCP 结果为多条 JSON 内容，只有验证过的结果才汇总。
      const rows = textItems.filter((item) => item.trim()).flatMap((item) => JSON.parse(item));
      if (rows.every((row) => typeof row?.resource_id === "string")) {
        const title = rows[0]?.title;
        result.summary = rows.length
          ? `${rows.length} 项资料${typeof title === "string" ? ` · ${title.slice(0, 120)}` : ""}`
          : "未找到资料";
      }
      return result;
    }
    const value = JSON.parse(output);
    if (state === "success" && libraryRead && typeof value?.path === "string" && typeof value.content_md === "string")
      result.summary = `已读取 ${value.path.slice(0, 130)}`;
    if (value?.ok === false) {
      result.state = "error";
      if (typeof value.error === "string") result.summary = value.error.slice(0, 160);
    } else if (shortName === "contact_employee" && value?.ok === true) {
      if (value.status === "delivered") result.state = "delivered";
      if (value.status === "scheduled") result.state = "scheduled";
    }
    if (task && value?.ok !== false && state === "success") {
      if (shortName === "nexus_task_categories" && Array.isArray(value?.categories)) {
        result.summary = `${value.categories.length} 个可用分类`;
      } else if (shortName === "nexus_task_list" && Array.isArray(value?.tasks)) {
        result.summary = `共 ${value.tasks.length} 项任务`;
      } else if (shortName === "nexus_task_assistance") {
        const pending = Array.isArray(value?.pending_requests)
          ? value.pending_requests.length
          : Array.isArray(value?.requests)
            ? value.requests.length
            : 0;
        result.summary = pending > 0 ? `${pending} 项待处理求助` : "已读取执行上下文";
      }
      const row = value?.task ?? value;
      const id = row?.task_id ?? row?.id;
      if (typeof id === "string" && id) {
        const status = typeof row.status === "string" ? row.status : undefined;
        const title = typeof row.title === "string" ? row.title : undefined;
        const workerName =
          typeof row.worker_name === "string" && row.worker_name.trim()
            ? row.worker_name.trim()
            : typeof row.agent_worker_id === "string" && row.agent_worker_id.trim()
              ? row.agent_worker_id.trim()
              : undefined;
        const progress =
          typeof row.current_progress === "string" && row.current_progress.trim()
            ? row.current_progress.trim()
            : undefined;
        const waitingReason =
          typeof row.waiting_reason === "string" && row.waiting_reason.trim()
            ? row.waiting_reason.trim()
            : undefined;
        result.task = {
          taskId: id,
          status,
          text: title,
          workerName,
          progress,
          waitingReason,
        };
        if (!result.summary) {
          const statusText = status ? TASK_STATUS_LABELS[status] || status : "";
          result.summary = [title || id, statusText].filter(Boolean).join(" · ").slice(0, 160);
        }
      }
    }
  } catch {
    // 工具也可能返回文本，原始输出始终可展开。
  }
  return result;
}

function filePart(block: DataBlock): Part {
  const source = block.source;
  return {
    type: "data",
    name: "nexus-file",
    data: {
      name: block.name || "附件",
      mediaType: source.media_type,
      url: source.type === "url" ? source.url : `data:${source.media_type};base64,${source.data}`,
    },
  };
}

function internalHint(source: string) {
  if (!source) return false;
  try {
    const value = JSON.parse(source);
    return (
      value?.label?.toLowerCase() === "system" && value?.sublabel?.trim().toLowerCase() === "runtime state"
    );
  } catch {
    return /^system\s*[·/]\s*runtime state$/i.test(source.trim());
  }
}

export function projectEntry({ native, sender, delivery, sequence, awaitingSnapshot }: TimelineEntry): ThreadMessageLike {
  let parts: Part[] = [];
  const toolIndex = new Map<string, number>();
  const states: Record<string, string> = {};
  const metadata = native.metadata ?? {};
  const files = new Set<string>();
  const uploaded =
    sender.senderKind === "human" && Array.isArray(metadata.attachments)
      ? (metadata.attachments as unknown as { id: string; name: string; type: string }[])
      : [];
  const appendFile = (block: DataBlock) => {
    const url = block.source.type === "url" ? block.source.url : block.id;
    if (files.has(url)) return;
    files.add(url);
    parts.push(filePart(block));
  };
  const display = typeof metadata.display_text === "string" ? metadata.display_text : null;
  if (display) parts.push({ type: "text", text: display });
  for (const block of native.content) {
    switch (block.type) {
      case "text":
        if (display === null && block.text) parts.push({ type: "text", text: block.text });
        break;
      case "thinking":
        if (sender.senderKind === "employee" && block.thinking)
          parts.push({ type: "reasoning", text: block.thinking });
        break;
      case "data":
        if (!uploaded.length) appendFile(block);
        break;
      case "hint": {
        if (internalHint(block.source || "")) break;
        const hintSource = block.source || "提示";
        const hintText =
          typeof block.hint === "string"
            ? block.hint
            : block.hint
                .filter((item) => item.type === "text")
                .map((item) => item.text)
                .join("\n");
        const taskCallback = parseTaskWakeHint(hintSource, hintText);
        parts.push({
          type: "data",
          name: "nexus-hint",
          data: {
            source: hintSource,
            text: hintText,
            ...(taskCallback ? { taskCallback } : {}),
          },
        });
        if (Array.isArray(block.hint))
          block.hint.forEach((item) => {
            if (item.type === "data") appendFile(item);
          });
        break;
      }
      case "tool_call":
      case "tool_result": {
        const index = toolIndex.get(block.id) ?? parts.length;
        const tool: ToolPart = (parts[index] as ToolPart | undefined) ?? {
          type: "tool-call",
          toolName: block.name,
          toolCallId: block.id,
        };
        toolIndex.set(block.id, index);
        if (block.type === "tool_call") {
          states[block.id] ??= block.state;
          parts[index] = { ...tool, argsText: block.input };
        } else {
          const texts = typeof block.output === "string"
            ? [block.output]
            : block.output.filter((item) => item.type === "text").map((item) => item.text);
          const result = toolResult(block.name, block.state, texts.join("\n"), texts);
          states[block.id] = result.state;
          parts[index] = {
            ...tool,
            result,
            isPreliminary: block.state === "running",
            isError: ["error", "denied", "interrupted"].includes(result.state),
          };
          if (Array.isArray(block.output))
            block.output.forEach((item) => {
              if (item.type === "data") appendFile(item);
            });
        }
        break;
      }
    }
  }
  for (const file of uploaded) {
    appendFile({
      type: "data",
      id: file.id,
      name: file.name,
      created_at: native.created_at,
      source: { type: "url", url: `/api/v1/attachments/${file.id}`, media_type: file.type },
    });
  }
  const taskId = typeof metadata.task_id === "string" ? metadata.task_id : undefined;
  if (taskId && sender.senderKind === "system") {
    const text = native.content
      .filter((item) => item.type === "text")
      .map((item) => item.text)
      .join("\n");
    parts = [
      { type: "data", name: "nexus-task", data: { taskId, text, status: metadata.task_status } },
      ...parts.filter((part) => part.type !== "text"),
    ];
  }
  const role = sender.senderKind === "human" ? "user" : "assistant";
  const finishedReason = awaitingSnapshot ? undefined : native.finished_reason;
  const status: ThreadMessageLike["status"] =
    role === "user"
      ? undefined
      : finishedReason === "error"
        ? { type: "incomplete", reason: "error", error: native.error?.message || "回复失败" }
        : finishedReason === "interrupted"
          ? { type: "incomplete", reason: "cancelled" }
          : finishedReason === "exceed_max_iters"
            ? { type: "incomplete", reason: "length" }
            : Object.values(states).includes("asking")
              ? { type: "requires-action", reason: "tool-calls" }
              : awaitingSnapshot || native.finished_at === null
                ? { type: "running" }
                : { type: "complete", reason: "stop" };
  return {
    id: native.id,
    createdAt: new Date(native.created_at),
    role,
    content: parts,
    status,
    metadata: {
      custom: {
        ...sender,
        delivery,
        sequence,
        toolStates: states,
        usage: native.usage && { ...native.usage },
        finishedAt: awaitingSnapshot ? undefined : native.finished_at,
        finishedReason,
        taskId,
        taskStatus: metadata.task_status,
      },
    },
  };
}

export function replyPhase(msg?: Msg): ConversationPhase {
  if (!msg || msg.finished_at) return "waiting";
  const results = new Map(
    msg.content.filter((block) => block.type === "tool_result").map((block) => [block.id, block.state]),
  );
  for (const block of msg.content) {
    if (block.type === "tool_call" && !results.has(block.id) && block.state === "asking") return "approval";
  }
  if (
    [...results.values()].includes("running") ||
    msg.content.some(
      (block) =>
        block.type === "tool_call" &&
        !results.has(block.id) &&
        ["allowed", "submitted"].includes(block.state),
    )
  )
    return "tool";
  const tail = msg.content.at(-1);
  return tail?.type === "text" && tail.text && !tail.finished_at ? "streaming" : "waiting";
}

// AgentScope 每个 event.id 是事件标识，私聊的续读游标只能来自持久化快照。
// SSE 保序。遇到重放边界、重复帧或先到的增量时，按 reply 和 block 归并。
export class ReplyEvents {
  private seen = new Set<string>();
  private waiting: AgentEvent[] = [];

  apply(msg: Msg, event: AgentEvent) {
    if (event.id && this.seen.has(event.id)) return false;
    if (event.id) this.seen.add(event.id);
    if (event.type === "REPLY_START") {
      if (!msg.finished_reason) msg.finished_at = null;
      return true;
    }
    this.waiting.push(event);
    const pending = [];
    for (const item of this.waiting) {
      if (this.ready(msg, item)) appendEvent(msg, item);
      else pending.push(item);
    }
    this.waiting = [];
    for (const item of pending) {
      if (this.ready(msg, item)) appendEvent(msg, item);
      else this.waiting.push(item);
    }
    return true;
  }

  private ready(msg: Msg, event: AgentEvent) {
    if (event.type.endsWith("_START") || event.type === "HINT_BLOCK") return true;
    if ("tool_call_id" in event) {
      const kind = event.type.startsWith("TOOL_RESULT") ? "tool_result" : "tool_call";
      return msg.content.some((block) => block.type === kind && block.id === event.tool_call_id);
    }
    if ("block_id" in event) return msg.content.some((block) => block.id === event.block_id);
    return true;
  }
}

export function compareEntries(a: TimelineEntry, b: TimelineEntry) {
  if (a.sequence !== undefined && b.sequence !== undefined) return a.sequence - b.sequence;
  const time = Date.parse(a.native.created_at) - Date.parse(b.native.created_at);
  if (time) return time;
  const micros = (s: string) => (s.match(/\.(\d+)/)?.[1] || "").slice(3, 6).padEnd(3, "0");
  return (
    micros(a.native.created_at).localeCompare(micros(b.native.created_at)) ||
    a.native.id.localeCompare(b.native.id)
  );
}
