import {
  memo,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type RefObject,
} from "react";
import {
  AttachmentPrimitive,
  ComposerPrimitive,
  type AssistantRuntime,
  useAui,
  useAuiEvent,
  useAuiState,
} from "@assistant-ui/react";
import { ArrowUp, AtSign, FileText, Paperclip, Square, X } from "lucide-react";
import { attachmentUrl } from "../nexus/attachments";
import { readDraft, writeDraft } from "../nexus/drafts";
import {
  insertMention,
  mentionCandidates,
  mentionTrigger,
  type MentionCandidate,
} from "./mentions";
import { PersonMark } from "./MessageContent";
import { VoiceInput } from "./VoiceInput";

function useSavedDraft(
  composer: AssistantRuntime["thread"]["composer"],
  key: string,
  enabled: boolean,
) {
  useEffect(() => {
    if (!enabled) return;
    let storage: Storage;
    try {
      storage = window.sessionStorage;
    } catch {
      return;
    }
    const saved = readDraft(storage, key);
    composer.setText(saved.text);
    for (const file of saved.files)
      void composer.addAttachment({
        id: file.id,
        name: file.name,
        contentType: file.type,
        type: file.type.startsWith("image/") ? "image" : "document",
        content: [],
      });
    const save = () => {
      const draft = composer.getState();
      writeDraft(storage, key, {
        text: draft.text,
        files: draft.attachments
          .filter(
            (f) =>
              f.status.type === "complete" ||
              f.status.type === "requires-action",
          )
          .map((f) => ({
            id: f.id,
            name: f.name,
            type: f.contentType || "application/octet-stream",
          })),
      });
    };
    return composer.subscribe(save);
  }, [composer, enabled, key]);
}

function AttachmentChip() {
  const attachment = useAuiState((s) => s.attachment);
  const status = attachment.status;
  const ready = status.type === "complete" || status.type === "requires-action";
  return (
    <div
      className="nc-chip"
      data-error={status.type === "incomplete" || undefined}
    >
      {ready && attachment.type === "image" ? (
        <img src={attachmentUrl(attachment.id)} alt="" />
      ) : (
        <FileText />
      )}
      <span className="nc-chip-label">
        <span title={attachment.name}>{attachment.name}</span>
        {!ready && (
          <small>
            {status.type === "incomplete"
              ? status.message || "上传失败"
              : "正在上传…"}
          </small>
        )}
      </span>
      <AttachmentPrimitive.Remove aria-label={`移除 ${attachment.name}`}>
        <X />
      </AttachmentPrimitive.Remove>
    </div>
  );
}

interface ComposerProps {
  composer: AssistantRuntime["thread"]["composer"];
  inputId: string;
  input: RefObject<HTMLTextAreaElement | null>;
  draftId: string;
  group: boolean;
  members: MentionCandidate[];
  enabled: boolean;
  canSend: boolean;
  canStop: boolean;
  phase: string;
  title: string;
}

export const ConversationComposer = memo(function ConversationComposer({
  composer,
  inputId,
  input,
  draftId,
  group,
  members,
  enabled,
  canSend,
  canStop,
  phase,
  title,
}: ComposerProps) {
  useSavedDraft(composer, draftId, enabled);
  const aui = useAui();
  const nextSelection = useRef<{ text: string; caret: number } | null>(null);
  // 私聊普通输入只更新框架的 Input；群聊还要根据草稿计算 @ 候选。
  const text = useAuiState((s) => group || nextSelection.current ? s.composer.text : "");
  const canSubmit = useAuiState(
    (s) =>
      s.composer.canSend &&
      !s.composer.attachments.some(
        (f) => f.status.type === "running" || f.status.type === "incomplete",
      ),
  );
  const running = useAuiState((s) => s.thread.isRunning);
  useLayoutEffect(() => {
    const selection = nextSelection.current;
    if (!selection || selection.text !== text) return;
    nextSelection.current = null;
    input.current?.focus({ preventScroll: true });
    input.current?.setSelectionRange(selection.caret, selection.caret);
  }, [input, text]);
  const [caret, setCaret] = useState<number | null>(null);
  const [index, setIndex] = useState(0);
  const [error, setError] = useState("");
  const listId = useId();
  const trigger = group && caret !== null ? mentionTrigger(text, caret) : null;
  const matches = useMemo(
    () =>
      trigger ? mentionCandidates(members, trigger.query).slice(0, 8) : [],
    [members, trigger?.query, !!trigger],
  );
  const selected = Math.min(index, matches.length - 1);
  const sendable = canSend && canSubmit;
  const showStop = canStop || phase === "stopping";
  const showSend = canSend || !showStop;
  useAuiEvent("composer.attachmentAddError", ({ message, attachmentId }) => {
    if (!attachmentId) setError(message);
  });
  useEffect(() => {
    const shortcut = (event: globalThis.KeyboardEvent) => {
      if (
        (event.metaKey || event.ctrlKey) &&
        !event.altKey &&
        !event.shiftKey &&
        event.key.toLowerCase() === "k" &&
        !document.querySelector("dialog[open], .el-overlay-dialog")
      ) {
        event.preventDefault();
        input.current?.focus();
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [input]);
  const choose = (member: MentionCandidate) => {
    if (!trigger || caret === null) return;
    const next = insertMention(text, trigger, member.name, caret);
    nextSelection.current = next;
    aui.composer.setText(next.text);
    setCaret(null);
    setIndex(0);
  };
  const startMention = () => {
    const start = input.current?.selectionStart ?? text.length;
    const end = input.current?.selectionEnd ?? start;
    const prefix = text.slice(0, start);
    const token = mentionTrigger(prefix + "@", start + 1) ? "@" : " @";
    const cursor = start + token.length;
    nextSelection.current = { text: prefix + token + text.slice(end), caret: cursor };
    aui.composer.setText(nextSelection.current.text);
    setCaret(cursor);
    setIndex(0);
  };
  const onKey = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return;
    if (
      matches.length &&
      ["ArrowDown", "ArrowUp", "Tab", "Enter", "Escape"].includes(event.key)
    ) {
      event.preventDefault();
      if (event.key === "Escape") setCaret(null);
      else if (event.key === "Tab" || event.key === "Enter")
        choose(matches[selected]!);
      else
        setIndex(
          (selected + matches.length + (event.key === "ArrowDown" ? 1 : -1)) %
            matches.length,
        );
      return;
    }
    // Nexus 群讨论可在运行中继续发言；assistant-ui 的默认 Enter 在运行中会阻止发送。
    if (event.key === "Enter" && !event.shiftKey && (running || !sendable)) {
      event.preventDefault();
      if (sendable) aui.composer.send();
    }
  };
  return (
    <div className="nc-compose-shell">
      {error && (
        <p className="nc-error" role="alert">
          {error}
          <button type="button" onClick={() => setError("")}>
            关闭
          </button>
        </p>
      )}
      <ComposerPrimitive.Root
        className="nc-composer"
        onSubmit={(e) => {
          if (!sendable) e.preventDefault();
        }}
      >
        {matches.length > 0 && (
          <div
            id={listId}
            className="nc-mentions"
            role="listbox"
            aria-label="选择数字员工"
          >
            {matches.map((member, i) => (
              <button
                key={member.id}
                type="button"
                role="option"
                id={listId + i}
                aria-selected={i === selected}
                data-testid={`compose-mention-${member.id}`}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => choose(member)}
                onMouseEnter={() => setIndex(i)}
              >
                <PersonMark avatar={member.avatar} name={member.name} />
                {member.name}
              </button>
            ))}
          </div>
        )}
        <div className="nc-chips">
          <ComposerPrimitive.Attachments>
            {() => <AttachmentChip />}
          </ComposerPrimitive.Attachments>
        </div>
        <ComposerPrimitive.Input
          ref={input}
          id={inputId}
          minRows={1}
          maxRows={7}
          maxLength={64000}
          data-testid="compose-input"
          aria-label="消息输入"
          placeholder={
            group
              ? "给群组发消息"
              : `给${title || "数字员工"}发消息`
          }
          disabled={!enabled}
          cancelOnEscape={false}
          unstable_focusOnRunStart={false}
          unstable_focusOnScrollToBottom={false}
          onKeyDown={onKey}
          aria-controls={matches.length ? listId : undefined}
          aria-activedescendant={matches.length ? listId + selected : undefined}
          onSelect={(e) => {
            if (group) setCaret(e.currentTarget.selectionStart);
          }}
          onChange={(e) => {
            setError("");
            if (group) {
              setCaret(e.currentTarget.selectionStart);
              setIndex(0);
            }
          }}
          onBlur={() => setCaret(null)}
        />
        <div className="nc-compose-actions">
          <ComposerPrimitive.AddAttachment
            className="nc-icon"
            title="添加附件"
            aria-label="添加附件"
            data-testid="compose-attach"
          >
            <Paperclip />
          </ComposerPrimitive.AddAttachment>
          {group && (
            <button
              className="nc-icon"
              type="button"
              aria-label="提及数字员工"
              title="提及数字员工"
              disabled={!enabled || !members.length}
              onMouseDown={(event) => event.preventDefault()}
              onClick={startMention}
            >
              <AtSign />
            </button>
          )}
          <VoiceInput
            disabled={!enabled}
            sessionKey={draftId}
            onText={(value) => {
              const previous = aui.composer.getState().text;
              const start = input.current?.selectionStart ?? previous.length;
              const end = input.current?.selectionEnd ?? start;
              const next =
                previous.slice(0, start) + value + previous.slice(end);
              nextSelection.current = {
                text: next,
                caret: start + value.length,
              };
              aui.composer.setText(next);
            }}
          />
          <span className="nc-spacer" />
          {showStop && (
            <ComposerPrimitive.Cancel
              className="nc-stop"
              disabled={!canStop}
              data-primary={!showSend || undefined}
              aria-label={group ? "停止讨论" : "停止回复"}
              title={group ? "停止讨论" : "停止回复"}
            >
              <Square />
              {phase === "stopping" ? "正在停止" : "停止"}
            </ComposerPrimitive.Cancel>
          )}
          {showSend && running && (
            <button
              className="nc-send"
              type="button"
              aria-label="发送消息"
              title="发送消息"
              disabled={!sendable}
              onClick={() => aui.composer.send()}
            >
              <ArrowUp />
            </button>
          )}
          {showSend && !running && (
            <ComposerPrimitive.Send
              className="nc-send"
              aria-label="发送消息"
              title="发送消息"
              disabled={!sendable}
            >
              <ArrowUp />
            </ComposerPrimitive.Send>
          )}
        </div>
      </ComposerPrimitive.Root>
    </div>
  );
});
