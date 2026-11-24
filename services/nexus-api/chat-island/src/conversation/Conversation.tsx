import { memo, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessageNotSentError,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type AssistantRuntime,
  type ThreadMessageLike,
} from "@assistant-ui/react";
import {
  ArrowDown,
  ChevronUp,
  CirclePause,
  Clock3,
  MessageSquare,
  UsersRound,
  Wrench,
} from "lucide-react";
import type { ConversationProps } from "../../../shared/conversation";
import { nexusAttachmentAdapter } from "../nexus/attachments";
import { draftKey } from "../nexus/drafts";
import { useConversation } from "../nexus/useConversation";
import { ConversationMessage, PersonMark } from "./MessageContent";
import { ConversationComposer } from "./Composer";

const convertMessage = (message: ThreadMessageLike) => message;
const rows = { Message: ConversationMessage };
const MessageList = memo(() => <ThreadPrimitive.Messages components={rows} />);
const phaseLabels: Record<string, string> = {
  waiting: "正在等待响应",
  streaming: "正在输出回复",
  tool: "正在调用工具",
  approval: "正在等待人工操作",
  syncing: "正在同步回复",
  stopping: "正在停止回复",
};
const phaseIcons = { tool: Wrench, approval: CirclePause, streaming: MessageSquare };

function summary(message: ThreadMessageLike) {
  const firstLine = (text: string) => text.trim().split("\n", 1)[0]
    .replace(/^(?:#{1,6}|>|[-+*]|\d+\.)\s+/, "").slice(0, 100);
  if (typeof message.content === "string") return firstLine(message.content);
  for (const part of message.content) {
    if (part.type === "text" && part.text.trim()) return firstLine(part.text);
    if (part.type === "tool-call") return part.toolName;
    if (part.type === "data" && part.name === "nexus-file") return "附件";
  }
  return "";
}

export function Conversation(props: ConversationProps) {
  const { kind, id, viewerId, title, enabled, onMembersChanged, onActivity } =
    props;
  const chat = useConversation(kind, id, onMembersChanged);
  const runtimeRef = useRef<AssistantRuntime | null>(null);
  const inputId = useId();
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const jump = useRef<HTMLButtonElement>(null);
  const [submission, setSubmission] = useState(0);
  useLayoutEffect(() => {
    if (submission) jump.current?.click();
  }, [submission]);
  const scrollToSubmission = () => setSubmission((count) => count + 1);
  const uploads = useMemo(() => new AbortController(), [kind, id, viewerId]);
  useEffect(() => () => uploads.abort(), [uploads]);
  const adapters = useMemo(
    () => ({
      attachments: nexusAttachmentAdapter(
        kind === "group" ? { channel_id: id } : { conversation_id: id },
        () =>
          runtimeRef.current?.thread.composer
            .getState()
            .attachments.map((file) => file.id) || [],
        uploads.signal,
      ),
    }),
    [kind, id, uploads],
  );
  const busy = chat.phase !== "idle";
  const canType = enabled && chat.canType;
  const runtime = useExternalStoreRuntime({
    messages: chat.messages,
    convertMessage,
    isLoading: chat.loading,
    isRunning: busy,
    isDisabled: !canType,
    isSendDisabled: !chat.canSend || !enabled,
    adapters,
    hasEarlier: chat.hasOlder,
    onLoadEarlier: chat.loadOlder,
    onNew: async (message) => {
      const request = chat.send(
        message.content
          .filter((c) => c.type === "text")
          .map((c) => c.text)
          .join("\n"),
        message.attachments?.map((file) => ({
          id: file.id,
          name: file.name,
          type: file.contentType || "application/octet-stream",
        })) ?? [],
      );
      scrollToSubmission();
      const outcome = await request;
      if (outcome === "rejected") throw new MessageNotSentError("消息未送达");
    },
    onCancel: chat.canStop ? chat.stop : undefined,
  });
  runtimeRef.current = runtime;

  const latest = chat.messages.at(-1);
  const latestId = latest?.id;
  const settled = !busy && !chat.sending && !chat.loading;
  const latestRef = useRef(latest);
  latestRef.current = latest;
  useEffect(() => {
    const current = latestRef.current;
    if (current && (settled || current.role === "user")) {
      const value = summary(current);
      if (value) onActivity?.(value);
    }
  }, [latestId, settled, onActivity]);
  const name = chat.title || title;
  const phase = chat.sending ? "sending" : chat.phase;
  const PhaseIcon = phaseIcons[phase as keyof typeof phaseIcons] || Clock3;
  const progress = chat.sending ? "正在发送消息…" : phaseLabels[chat.phase];
  const worker = ["waiting", "streaming", "tool", "approval"].includes(phase) ? chat.activeEmployee : "";
  const empty = settled && !chat.error && !chat.messages.length;
  const draftId = draftKey(viewerId, kind, id);
  const restoreDraft = () => {
    const pending = chat.restorePendingDraft();
    if (!pending) return;
    const composer = runtime.thread.composer;
    const current = composer.getState();
    composer.setText([pending.text, current.text].filter(Boolean).join("\n"));
    const ids = new Set(current.attachments.map((file) => file.id));
    for (const file of pending.files) {
      if (!ids.has(file.id)) void composer.addAttachment({
        id: file.id, name: file.name, contentType: file.type,
        type: file.type.startsWith("image/") ? "image" : "document", content: [],
      });
    }
    inputRef.current?.focus({ preventScroll: true });
  };
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="nc-conversation" data-empty={empty || undefined}>
        {canType && <a href={`#${inputId}`} className="nc-skip-input" onClick={(event) => {
          event.preventDefault();
          inputRef.current?.focus({ preventScroll: true });
        }}>跳到消息输入</a>}
        <ComposerPrimitive.AttachmentDropzone className="nc-drop">
          <ThreadPrimitive.Viewport
            className="nc-viewport"
            data-testid="chat-messages"
            scrollToBottomOnRunStart={false}
          >
            <div className="nc-page">
              {chat.hasOlder && (
                <ThreadPrimitive.LoadEarlier
                  className="nc-older"
                  data-testid="chat-load-older"
                >
                  <ChevronUp />
                  {chat.loadingOlder ? "正在读取…" : "加载更早消息"}
                </ThreadPrimitive.LoadEarlier>
              )}
              {chat.loading && (
                <p className="nc-loading" role="status">
                  正在读取会话…
                </p>
              )}
              {empty && (
                <div className="nc-empty" data-testid="chat-empty">
                  <div className="nc-empty-mark">
                    {kind === "group" ? (
                      <UsersRound />
                    ) : props.avatar ? (
                      <PersonMark avatar={props.avatar} name={name} />
                    ) : (
                      <MessageSquare />
                    )}
                  </div>
                  <h3>
                    {kind === "group" ? name : `与${name}对话`}
                  </h3>
                  <p>
                    {kind === "group"
                      ? "输入 @ 邀请指定员工参与讨论"
                      : "发送消息或添加文件，开始协作"}
                  </p>
                </div>
              )}
              <MessageList />
              {(chat.sending || busy) && (
                <div
                  className="nc-work"
                  role="status"
                  data-testid="streaming-placeholder"
                  data-phase={phase}
                >
                  <PhaseIcon aria-hidden />
                  {worker ? `${worker} · ${progress}` : progress}
                </div>
              )}
            </div>
            <div className="nc-jump-wrap">
              <ThreadPrimitive.ScrollToBottom
                ref={jump}
                behavior="instant"
                className="nc-jump"
                aria-label="回到最新消息"
              >
                <ArrowDown />
                最新消息
              </ThreadPrimitive.ScrollToBottom>
            </div>
          </ThreadPrimitive.Viewport>
          <div className="nc-bottom">
            <div className="nc-bottom-inner">
              {chat.error && (
                <div className="nc-error" role="alert" data-testid="fail-bar">
                  <span>{chat.error}</span>
                  {chat.retryable ? (
                    <button
                      type="button"
                      data-testid="fail-retry"
                      disabled={chat.sending || (chat.retryable === "send" && !canType)}
                      onClick={async () => {
                        if (chat.retryable === "send") scrollToSubmission();
                        await chat.retry();
                      }}
                    >
                      {chat.retryable === "send" ? "重试原消息" : "重新连接"}
                    </button>
                  ) : (
                    <button type="button" onClick={chat.dismissError}>关闭</button>
                  )}
                  {chat.retryable === "send" && (
                    <button type="button" disabled={!canType || chat.sending} onClick={restoreDraft}>
                      编辑原消息
                    </button>
                  )}
                </div>
              )}
              <ConversationComposer
                composer={runtime.thread.composer}
                inputId={inputId}
                input={inputRef}
                draftId={draftId}
                enabled={canType}
                title={name}
                members={chat.members}
                group={kind === "group"}
                canSend={chat.canSend && enabled}
                canStop={chat.canStop}
                phase={chat.phase}
              />
            </div>
          </div>
        </ComposerPrimitive.AttachmentDropzone>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}
