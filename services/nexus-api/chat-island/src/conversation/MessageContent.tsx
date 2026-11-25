import {
  createContext,
  memo,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import {
  ActionBarPrimitive,
  MessagePrimitive,
  useAuiState,
  type DataMessagePartProps,
  type ReasoningMessagePartProps,
  type TextMessagePartProps,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import {
  MarkdownTextPrimitive,
  type CodeHeaderProps,
  type SyntaxHighlighterProps,
} from "@assistant-ui/react-markdown";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  ArrowDownToLine,
  ArrowUpRight,
  BellRing,
  BriefcaseBusiness,
  Check,
  CircleAlert,
  CircleCheck,
  CirclePause,
  CirclePlay,
  ChevronRight,
  Copy,
  FileText,
  ListChecks,
  MessageSquareText,
  Sparkles,
  UsersRound,
  Wrench,
} from "lucide-react";
import type {
  ConversationTaskFeed,
  ConversationTaskSummary,
} from "../../../shared/conversation";
import { employeeAppearance } from "../../../shared/employeeAvatar";
import {
  shortToolName,
  taskToolLabel,
  type ConversationTaskCallback,
  type ConversationTaskRef,
  type ConversationToolResult,
} from "../nexus/conversationMessage";

export const TaskFeedContext = createContext<ConversationTaskFeed | undefined>(undefined);

const noopSubscribe = () => () => {};
const EMPTY_TASK_MAP: Record<string, ConversationTaskSummary> = {};
const emptySnapshot = () => EMPTY_TASK_MAP;

function useLiveTask(taskId?: string): ConversationTaskSummary | undefined {
  const feed = useContext(TaskFeedContext);
  const map = useSyncExternalStore(
    feed ? feed.subscribe : noopSubscribe,
    feed ? feed.snapshot : emptySnapshot,
  );
  return taskId ? map[taskId] : undefined;
}

interface FilePart {
  name: string;
  url: string;
  mediaType: string;
}
interface MessageMeta {
  senderId?: string;
  senderName?: string;
  senderKind?: "human" | "employee" | "system";
  avatar?: string;
  self?: boolean;
  delivery?: string;
  finishedReason?: string;
  finishedAt?: string;
  usage?: { input_tokens?: number; output_tokens?: number };
  taskId?: string;
  taskStatus?: string;
  toolStates?: Record<string, string>;
}

const taskLabels: Record<string, string> = {
  PENDING: "待执行",
  RUNNING: "执行中",
  WAITING: "待介入",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCELED: "已取消",
};

const waitingReasonLabels: Record<string, string> = {
  question: "执行端提出问题，等待答复",
  permission: "等待工具或命令执行授权",
  confirmation: "回合结束，等待确认验收",
  manual_intervene: "已转人工介入处理",
  paused: "任务已暂停",
  session_missing: "远端执行会话丢失，等待恢复",
  worker_offline: "关联 Worker 当前离线",
  delivery_uncertain: "指令投递状态待确认",
  reconcile_timeout: "执行状态对账超时",
};

function taskTone(status?: string): "info" | "warning" | "success" | "danger" | "neutral" {
  switch (status) {
    case "RUNNING":
      return "info";
    case "WAITING":
      return "warning";
    case "SUCCEEDED":
      return "success";
    case "FAILED":
      return "danger";
    default:
      return "neutral";
  }
}

function formatWaitingReason(reason?: string | null): string {
  if (!reason) return "";
  const key = reason.trim();
  return waitingReasonLabels[key] || key;
}
const toolLabels: Record<string, string> = {
  pending: "准备调用",
  asking: "等待人工确认",
  allowed: "已允许",
  submitted: "已提交",
  finished: "调用结束",
  running: "执行中",
  success: "已完成",
  delivered: "已投递",
  scheduled: "已安排",
  error: "失败",
  denied: "已拒绝",
  interrupted: "已中断",
};
const markdownPlugins = [remarkGfm];
const timeFormat = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
});
// 常见的纯正文直接显示，含 Markdown 或 GFM 标点的内容都交给解析器。
const plainText = /^[\p{L}\p{N} ，。！？：；、（）“”‘’]+$/u;

export function PersonMark({
  avatar,
  name,
}: {
  avatar?: string;
  name: string;
}) {
  const image = avatar && employeeAppearance(avatar)?.portrait;
  return image ? (
    <img className="nc-person" src={image} alt="" />
  ) : (
    <span className="nc-monogram" aria-hidden>
      {name.slice(0, 1)}
    </span>
  );
}

function CodeHeader({ language, code }: CodeHeaderProps) {
  const [copyStatus, setCopyStatus] = useState("");
  useEffect(() => {
    if (!copyStatus) return;
    const timer = window.setTimeout(() => setCopyStatus(""), 1600);
    return () => window.clearTimeout(timer);
  }, [copyStatus]);
  const copied = copyStatus === "已复制";
  return (
    <div className="nc-code-header">
      <span>{language || "text"}</span>
      <button
        type="button"
        className={copied ? "is-copied" : undefined}
        aria-label="复制代码"
        title={copyStatus || "复制代码"}
        onClick={async () => {
          try {
            await navigator.clipboard.writeText(code);
            setCopyStatus("已复制");
          } catch {
            setCopyStatus("无法访问剪贴板");
          }
        }}
      >
        {copied ? <Check /> : <Copy />}
        <span>{copyStatus || "复制"}</span>
      </button>
    </div>
  );
}
const Highlight = memo(function Highlight({
  code,
  language,
}: SyntaxHighlighterProps) {
  const running = useAuiState((s) => s.message.status?.type === "running");
  const [highlight, setHighlight] = useState<{ code: string; html: string }>();
  useEffect(() => {
    let current = true;
    if (!running && language && code.length <= 24000) {
      void import("highlight.js/lib/common").then(({ default: hljs }) => {
        if (current && hljs.getLanguage(language))
          setHighlight({
            code,
            html: hljs.highlight(code, { language, ignoreIllegals: true })
              .value,
          });
      });
    }
    return () => {
      current = false;
    };
  }, [running, code, language]);
  return (
    <pre>
      {highlight?.code === code ? (
        <code dangerouslySetInnerHTML={{ __html: highlight.html }} />
      ) : (
        <code>{code}</code>
      )}
    </pre>
  );
});
function MarkdownTable({ children }: { children?: ReactNode }) {
  return (
    <div className="nc-table-scroll">
      <table>{children}</table>
    </div>
  );
}
const markdownComponents = {
  table: MarkdownTable,
  CodeHeader,
  SyntaxHighlighter: Highlight,
};
const MarkdownPart = memo(function MarkdownPart({
  text,
}: TextMessagePartProps) {
  if (plainText.test(text))
    return <p className="nc-prose nc-plain">{text}</p>;
  return (
    <MarkdownTextPrimitive
      className="nc-prose"
      remarkPlugins={markdownPlugins}
      components={markdownComponents}
      smooth={false}
      defer
    />
  );
});

function taskHref(id: string) {
  return `/tasks/${encodeURIComponent(id)}`;
}

function TaskLink({
  data,
}: {
  data: ConversationTaskRef;
}) {
  const live = useLiveTask(data.taskId);
  const status = live?.status || data.status;
  const tone = taskTone(status);
  const statusText = status ? taskLabels[status] || status : "";
  const title = live?.title || data.text || data.taskId;
  const workerName = live?.workerName || data.workerName || "";
  const isFinished = status === "SUCCEEDED" || status === "CANCELED";
  const workerOnline = isFinished ? undefined : live?.workerOnline;
  const detailLine =
    status === "WAITING"
      ? formatWaitingReason(live?.waitingReason || data.waitingReason) ||
        live?.currentProgress ||
        data.progress ||
        ""
      : live?.currentProgress || data.progress || "";
  const showDetail = Boolean(detailLine && detailLine !== statusText && detailLine !== title);
  return (
    <a className="nc-task" data-tone={tone} href={taskHref(data.taskId)}>
      <div className="nc-task-head">
        <span className="nc-task-icon" aria-hidden>
          <BriefcaseBusiness />
        </span>
        {statusText && (
          <span className="nc-task-badge" data-tone={tone}>
            <i className="nc-task-dot" aria-hidden />
            {statusText}
          </span>
        )}
        <code className="nc-task-id">{data.taskId}</code>
        <ArrowUpRight className="nc-task-arrow" aria-hidden />
      </div>
      <strong className="nc-task-title">{title}</strong>
      {showDetail && (
        <p className={`nc-task-detail${status === "WAITING" ? " is-warning" : ""}`}>
          {detailLine}
        </p>
      )}
      {workerName && (
        <div className="nc-task-foot">
          <span className="nc-task-worker">
            <i
              className={`nc-worker-dot${workerOnline === true ? " is-on" : workerOnline === false ? " is-off" : ""}`}
              aria-hidden
            />
            {workerName}
            {workerOnline === false && <em className="nc-worker-off">离线</em>}
          </span>
        </div>
      )}
    </a>
  );
}
function toolOutcome(state?: string) {
  switch (state) {
    case "success":
    case "finished":
    case "delivered":
      return { Icon: CircleCheck, tone: "done" };
    case "error":
    case "denied":
    case "interrupted":
      return { Icon: CircleAlert, tone: "error" };
    case "asking":
      return { Icon: CirclePause, tone: "attention" };
    default:
      return { Icon: CirclePlay, tone: "working" };
  }
}

function ToolPart({
  toolCallId,
  toolName,
  args,
  argsText,
  result: raw,
}: ToolCallMessagePartProps) {
  const result = raw as ConversationToolResult | undefined;
  const callState = useAuiState(
    (s) => (s.message.metadata.custom as MessageMeta).toolStates?.[toolCallId],
  );
  const messageTaskId = useAuiState((s) => (s.message.metadata.custom as MessageMeta).taskId);
  const state = result?.state ?? callState;
  const [open, setOpen] = useState(false);
  const shortName = shortToolName(toolName);
  const { Icon: OutcomeIcon, tone } = toolOutcome(state);
  const contact =
    shortName === "contact_employee" || shortName === "contact_human";
  const task = shortName.startsWith("nexus_task_");
  const libraryLabel = { library_search: "搜索资料", library_get: "读取资料" }[shortName];
  const Icon = contact ? UsersRound : task ? BriefcaseBusiness : Wrench;
  const mcp = toolName.startsWith("mcp__")
    ? toolName.split("__").slice(1).join(" / ")
    : shortName;
  const title = contact
    ? "联系" + (shortName === "contact_employee" ? "同事" : "用户")
    : taskToolLabel(toolName) || libraryLabel || mcp;
  const inputSummary = contact
    ? args.employee_name
    : args.title ||
      args.subject ||
      args.task_id ||
      args.query ||
      args.file_path ||
      args.path ||
      args.pattern;
  const shortInput = typeof inputSummary === "string" ? inputSummary : "";
  const output = result?.output;
  const approvalTaskId = state === "asking"
    ? messageTaskId || (task && typeof args.task_id === "string" ? args.task_id : undefined)
    : undefined;
  const statusLabel = state === "asking" ? "待确认" : state ? toolLabels[state] || state : "";
  const summary = state === "asking"
    ? approvalTaskId ? "在任务详情查看处理方式" : "当前会话无法确认，可停止本轮"
    : [contact ? shortInput : "", result?.summary || (contact ? "" : shortInput)].filter(Boolean).join(" · ");
  return (
    <div className="nc-tool-wrap">
      <details
        className="nc-tool"
        data-tone={tone}
        open={open}
        onToggle={(e) => setOpen(e.currentTarget.open)}
      >
        <summary>
          <Icon className="nc-tool-icon" aria-hidden />
          <span className="nc-tool-name" title={toolName}>{title}</span>
          {summary && (
            <span className="nc-tool-summary" title={summary}>{summary}</span>
          )}
          {statusLabel && (
            <small className="nc-tool-state">
              <OutcomeIcon aria-hidden />
              {statusLabel}
            </small>
          )}
          <ChevronRight className="nc-chevron" aria-hidden />
        </summary>
        {open && (
          <div className="nc-tool-detail">
            {contact && <p>{String(args.message || "")}</p>}
            {argsText && (
              <>
                <b>调用参数</b>
                <pre>{argsText}</pre>
              </>
            )}
            {output && (
              <>
                <b>返回结果</b>
                <pre>{output}</pre>
              </>
            )}
          </div>
        )}
      </details>
      {approvalTaskId && <TaskLink data={{ taskId: approvalTaskId, text: "查看关联任务" }} />}
    </div>
  );
}

function safeFileUrl(url: string) {
  if (/^data:[\w.+-]+\/[\w.+-]+;base64,[\w+/=\s]+$/i.test(url)) return url;
  try {
    const parsed = new URL(url, location.href);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch {
    return "";
  }
}
function TextPreview({ file, url }: { file: FilePart; url: string }) {
  const [result, setResult] = useState({ text: "", error: "", loading: true });
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      const response = await fetch(url, { signal: controller.signal });
      if (!response.ok || !response.body)
        throw new Error(`附件读取失败（HTTP ${response.status}）`);
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let text = "";
      while (text.length <= 20000) {
        const chunk = await reader.read();
        if (chunk.done) {
          text += decoder.decode();
          break;
        }
        text += decoder.decode(chunk.value, { stream: true });
      }
      if (text.length > 20000) {
        await reader.cancel();
        text = text.slice(0, 20000) + "\n…（预览已截断）";
      }
      if (!controller.signal.aborted)
        setResult({ text, error: "", loading: false });
    })().catch((error) => {
      if (!controller.signal.aborted)
        setResult({ text: "", error: error.message, loading: false });
    });
    return () => controller.abort();
  }, [url]);
  if (result.loading || result.error)
    return <small role="status">{result.error || "正在读取附件…"}</small>;
  return /markdown|\.md$/i.test(file.mediaType + file.name) ? (
    <div className="nc-prose">
      <ReactMarkdown remarkPlugins={markdownPlugins} components={{ table: MarkdownTable }}>
        {result.text}
      </ReactMarkdown>
    </div>
  ) : (
    <pre>{result.text}</pre>
  );
}
function FilePartView({ data }: DataMessagePartProps<FilePart>) {
  const [open, setOpen] = useState(false);
  const url = safeFileUrl(data.url);
  const isImage = /^(?:image\/(?:png|jpe?g|webp|gif|avif|bmp))$/i.test(
    data.mediaType,
  );
  if (!url) return <span className="nc-file">{data.name} · 无法打开附件</span>;
  const preview =
    /^(?:text\/|application\/markdown)/i.test(data.mediaType) &&
    (url.startsWith("data:") || new URL(url).origin === location.origin);
  return (
    <div className="nc-file-wrap">
      {isImage ? (
        <a
          className="nc-image-link"
          href={url}
          target="_blank"
          rel="noreferrer"
          download={url.startsWith("data:") ? data.name : undefined}
        >
          <img src={url} alt={data.name} loading="lazy" />
        </a>
      ) : (
        <div className="nc-file">
          <span className="nc-result-mark"><FileText /></span>
          <a href={url} download={data.name} target="_blank" rel="noreferrer">
            {data.name || "下载文件"} <ArrowDownToLine />
          </a>
          {preview && (
            <button
              type="button"
              aria-expanded={open}
              onClick={() => setOpen(!open)}
            >
              {open ? "收起" : "预览"}
            </button>
          )}
        </div>
      )}
      {open && (
        <div className="nc-file-preview">
          <TextPreview file={data} url={url} />
        </div>
      )}
    </div>
  );
}
function TaskCallbackCard({ callback }: { callback: ConversationTaskCallback }) {
  const live = useLiveTask(callback.taskId);
  const status = callback.status || live?.status;
  const tone = taskTone(status);
  const statusLabel = status ? taskLabels[status] || callback.statusLabel : callback.statusLabel;
  const title = live?.title || callback.taskId;
  const detail =
    callback.reason ||
    (status === "WAITING" ? formatWaitingReason(live?.waitingReason) : "") ||
    (live?.currentProgress && live.currentProgress !== statusLabel ? live.currentProgress : "");
  return (
    <div className="nc-task-callback" data-tone={tone} role="note">
      <div className="nc-task-callback-head">
        <span className="nc-task-callback-label">
          <BellRing aria-hidden />
          任务回调
        </span>
        <span className="nc-task-badge" data-tone={tone}>
          <i className="nc-task-dot" aria-hidden />
          {statusLabel}
        </span>
        <a className="nc-task-callback-link" href={taskHref(callback.taskId)}>
          <span>{title}</span>
          <ArrowUpRight aria-hidden />
        </a>
      </div>
      {(detail || live?.workerName) && (
        <div className="nc-task-callback-sub">
          {detail && <span>{detail}</span>}
          {live?.workerName && (
            <span className="nc-task-worker">
              <i
                className={`nc-worker-dot${live.workerOnline === true ? " is-on" : live.workerOnline === false ? " is-off" : ""}`}
                aria-hidden
              />
              {live.workerName}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function HintPart({
  data,
}: DataMessagePartProps<{ source: string; text: string; taskCallback?: ConversationTaskCallback }>) {
  if (data.taskCallback) {
    return <TaskCallbackCard callback={data.taskCallback} />;
  }
  return (
    <details className="nc-note">
      <summary>
        <MessageSquareText />
        {data.source || "协作消息"}
        <ChevronRight />
      </summary>
      <p>{data.text}</p>
    </details>
  );
}
function ReasoningPart({ text }: ReasoningMessagePartProps) {
  return (
    <details className="nc-note nc-reasoning">
      <summary>
        <Sparkles className="nc-note-icon" aria-hidden />
        <span>思考记录</span>
        <ChevronRight />
      </summary>
      <p>{text}</p>
    </details>
  );
}
const groupFinishedTools: MessagePrimitive.GroupedParts.Props<"group-tools">["groupBy"] =
  (part) => {
    if (part.type !== "tool-call" || part.isError) return null;
    const name = shortToolName(part.toolName);
    if (name.startsWith("contact_")) return null;
    const state = (part.result as ConversationToolResult | undefined)?.state;
    return state === "success" || state === "finished" ? ["group-tools"] : null;
  };

function toolFriendlyLabel(toolName: string): string {
  const short = shortToolName(toolName);
  const taskLabel = taskToolLabel(toolName);
  if (taskLabel) return taskLabel;
  if (short === "library_search") return "搜索资料";
  if (short === "library_get") return "读取资料";
  return short;
}

function FinishedTools({
  count,
  indices,
  children,
}: {
  count: number;
  indices: readonly number[];
  children: ReactNode;
}) {
  const [open, setOpen] = useState(false);
  const preview = useAuiState((s) => {
    const labels: string[] = [];
    for (const idx of indices) {
      const part = s.message.parts[idx];
      if (part?.type === "tool-call") {
        const label = toolFriendlyLabel(part.toolName);
        if (!labels.includes(label)) labels.push(label);
      }
    }
    return labels.slice(0, 3).join("、");
  });
  return (
    <details className="nc-tool-group" onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary>
        <ListChecks aria-hidden />
        <span className="nc-tool-name">已完成 {count} 项操作</span>
        {preview && <span className="nc-tool-summary" title={preview}>{preview}</span>}
        <ChevronRight aria-hidden />
      </summary>
      {open && <div className="nc-tool-group-body">{children}</div>}
    </details>
  );
}

function renderPart({ part, children }: MessagePrimitive.GroupedParts.RenderInfo<"group-tools">) {
  switch (part.type) {
    case "group-tools":
      return part.indices.length > 1
        ? <FinishedTools count={part.indices.length} indices={part.indices}>{children}</FinishedTools>
        : children;
    case "text":
      return <MarkdownPart {...part} />;
    case "reasoning":
      return <ReasoningPart {...part} />;
    case "tool-call":
      return <ToolPart {...part} />;
    case "data":
      if (part.name === "nexus-file") return <FilePartView {...part} />;
      if (part.name === "nexus-hint") return <HintPart {...part} />;
      if (part.name === "nexus-task") return <TaskLink data={part.data} />;
  }
  return null;
}

const MessageParts = memo(function MessageParts() {
  const parts = useAuiState((s) => s.message.parts);
  const toolTasks = useMemo(() => {
    const byId = new Map<string, ConversationTaskRef>();
    for (const part of parts) {
      if (part.type !== "tool-call") continue;
      const result = part.result as ConversationToolResult | undefined;
      if (!result?.task?.taskId) continue;
      const prev = byId.get(result.task.taskId);
      byId.set(result.task.taskId, {
        ...prev,
        ...result.task,
        text: result.task.text || prev?.text,
        workerName: result.task.workerName || prev?.workerName,
      });
    }
    return [...byId.values()];
  }, [parts]);
  return (
    <>
      <MessagePrimitive.GroupedParts groupBy={groupFinishedTools} indicator="never">
        {renderPart}
      </MessagePrimitive.GroupedParts>
      {toolTasks.length > 0 && (
        <div className="nc-task-stack">
          {toolTasks.map((item) => (
            <TaskLink key={item.taskId} data={item} />
          ))}
        </div>
      )}
    </>
  );
});

export const ConversationMessage = memo(function ConversationMessage() {
  const meta = useAuiState((s) => s.message.metadata.custom as MessageMeta);
  const created = useAuiState((s) => s.message.createdAt);
  const copyable = useAuiState((s) =>
    s.message.parts.some((p) => p.type === "text" && p.text.length > 0),
  );
  const hasSystemTask = useAuiState((s) =>
    s.message.parts.some((p) => p.type === "data" && p.name === "nexus-task"),
  );
  const copied = useAuiState((s) => s.message.isCopied);
  const failure = useAuiState((s) => {
    const status = s.message.status;
    return status?.type === "incomplete" && status.reason === "error" && typeof status.error === "string"
      ? status.error : "";
  });
  const isLast = useAuiState((s) => {
    const messages = s.thread.messages;
    const tail = messages.at(-1);
    // 等待时 assistant-ui 会在已发送消息后插入匿名占位消息。
    const last = tail?.metadata.custom.senderKind ? tail : messages.at(-2);
    return last?.id === s.message.id;
  });
  const role = useAuiState((s) => s.message.role);
  const continuation = useAuiState((s) => {
    const current = s.message;
    const previous = s.thread.messages[current.index - 1];
    const sender = current.metadata.custom as MessageMeta;
    if (!previous || !sender.senderId || sender.self || sender.senderKind === "system") return false;
    const lastSender = previous.metadata.custom as MessageMeta;
    return (
      lastSender?.senderId === sender.senderId && lastSender.senderKind === sender.senderKind &&
      current.createdAt.getTime() - previous.createdAt.getTime() < 5 * 60_000
    );
  });
  // assistant-ui 在运行期间可临时插入占位消息；工作状态由 Nexus 的回合事实提供。
  if (!meta.senderKind) return null;
  const name = meta.self
    ? "你"
    : meta.senderName || (role === "assistant" ? "数字员工" : "成员");
  const system = meta.senderKind === "system";
  const elapsed =
    meta.finishedAt &&
    created &&
    (Date.parse(meta.finishedAt) - created.getTime()) / 1000;
  const details = [
    typeof elapsed === "number" &&
      elapsed >= 0 &&
      `用时 ${elapsed.toFixed(1)} 秒`,
    meta.usage &&
      `输入 ${meta.usage.input_tokens ?? "—"} · 输出 ${meta.usage.output_tokens ?? "—"} tokens`,
  ]
    .filter(Boolean)
    .join(" · ");
  const delivery = isLast && meta.delivery
    ? { sending: "正在发送", sent: "已发送", failed: "发送失败" }[meta.delivery]
    : undefined;
  return (
    <MessagePrimitive.Root
      className="nc-message"
      data-kind={system ? "system" : meta.self ? "self" : "speaker"}
      data-system-card={(system && hasSystemTask) || undefined}
      data-sender={meta.senderId}
      data-continuation={continuation || undefined}
      data-delivery={delivery ? meta.delivery : undefined}
    >
      {!system && !meta.self && !continuation && (
        <div className="nc-message-avatar">
          <PersonMark avatar={meta.avatar} name={name} />
        </div>
      )}
      {!system && (
        <header className="nc-message-meta">
          <strong className={meta.self || continuation ? "nc-sr-only" : undefined}>{name}</strong>
          {created && (
            <time
              dateTime={created.toISOString()}
              title={details || created.toLocaleString("zh-CN")}
              aria-label={[timeFormat.format(created), details]
                .filter(Boolean)
                .join("，")}
            >
              {timeFormat.format(created)}
            </time>
          )}
          {copyable && (
            <ActionBarPrimitive.Copy
              className="nc-copy"
              aria-label={copied ? "已复制" : "复制消息"}
              title="复制消息"
            >
              {copied ? <Check /> : <Copy />}
            </ActionBarPrimitive.Copy>
          )}
        </header>
      )}
      <div className="nc-message-content">
        <MessageParts />
      </div>
      {delivery && <small className="nc-delivery" role="status">{delivery}</small>}
      {isLast && meta.finishedReason === "completed" && (
        <small className="nc-end-note"><Check aria-hidden /> 已完成</small>
      )}
      {meta.finishedReason && meta.finishedReason !== "completed" && (
        <small className="nc-end-note is-error"><CircleAlert aria-hidden />
          {meta.finishedReason === "interrupted" ? "回复已中断" : failure || "回复未完成"}
        </small>
      )}
    </MessagePrimitive.Root>
  );
});
