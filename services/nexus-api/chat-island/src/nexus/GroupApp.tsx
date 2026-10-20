/**
 * 群组聊天视图。
 *
 * 群组不是一条 AgentScope Session：人类消息在 Nexus，每位员工各有自己的 Session。
 * 这里把每条 Session 的原生消息合并成一条时间线，在气泡外层补发言人与来源，
 * 事件流按成员分别订阅，发送仍走 Nexus 的 channel 入口（@ 路由由后端解析）。
 */
import { AssistantMsg, UserMsg, appendEvent } from "@agentscope-ai/agentscope/message";
import type { ContentBlock, Msg } from "@agentscope-ai/agentscope/message";
import { useEffect, useMemo, useRef, useState } from "react";

import { ASMessageBubble } from "@/components/chat/ASMessageBubble";
import { TextInput } from "@/components/chat/TextInput";

import { attachmentIdFromBlock, uploadAttachment } from "./attachments";
import {
  type GroupChannel,
  interruptMember,
  loadGroupTimeline,
  sendGroupMessage,
  streamMember,
} from "./group";

interface SessionState {
  sessionId: string;
  employeeId: string;
  label: string;
  cursor: string;
  msgs: Msg[];
}

function eventType(event: unknown): string {
  return String((event as { type?: string } | null)?.type ?? "");
}

function eventReplyId(event: unknown): string {
  return String((event as { reply_id?: string } | null)?.reply_id ?? "");
}

export function GroupApp({ channelId }: { channelId: string }) {
  const [channel, setChannel] = useState<GroupChannel | null>(null);
  const [humans, setHumans] = useState<Msg[]>([]);
  const [sessions, setSessions] = useState<Record<string, SessionState>>({});
  const [streaming, setStreaming] = useState<Record<string, boolean>>({});
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadToken, setReloadToken] = useState(0);
  const scroller = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setLoading(true);
    setError("");

    const apply = (employeeId: string, event: unknown) => {
      const type = eventType(event);
      const replyId = eventReplyId(event);
      setSessions((previous) => {
        const current = previous[employeeId];
        if (!current) return previous;
        if (type === "REPLY_START") {
          if (!replyId || current.msgs.some((item) => item.id === replyId)) return previous;
          return {
            ...previous,
            [employeeId]: {
              ...current,
              msgs: [...current.msgs, AssistantMsg({ id: replyId, name: current.label, content: [] })],
            },
          };
        }
        if (!replyId) return previous;
        const index = current.msgs.findIndex((item) => item.id === replyId);
        if (index < 0) return previous;
        const next = [...current.msgs];
        next[index] = appendEvent({ ...next[index] }, event as never);
        return { ...previous, [employeeId]: { ...current, msgs: next } };
      });
      if (type === "REPLY_START") setStreaming((prev) => ({ ...prev, [employeeId]: true }));
      if (type === "REPLY_END") setStreaming((prev) => ({ ...prev, [employeeId]: false }));
    };

    (async () => {
      const timeline = await loadGroupTimeline(channelId);
      if (!active) return;
      setChannel(timeline.channel);
      const human = timeline.streams.find((item) => item.session_id === timeline.channel.id);
      setHumans(human?.messages ?? []);
      const members = timeline.streams.filter((item) => item.session_id !== timeline.channel.id);
      const initial: Record<string, SessionState> = {};
      for (const member of members) {
        initial[member.employee_id || member.session_id] = {
          sessionId: member.session_id,
          employeeId: member.employee_id,
          label: member.label,
          cursor: member.event_cursor ?? "",
          msgs: member.messages ?? [],
        };
      }
      setSessions(initial);
      setStreaming({});
      setLoading(false);

      for (const member of members) {
        const key = member.employee_id || member.session_id;
        void (async () => {
          for await (const event of streamMember(
            channelId,
            member.employee_id,
            controller.signal,
            member.event_cursor,
          )) {
            if (!active) return;
            apply(key, event);
          }
        })().catch(() => undefined);
      }
    })().catch((reason: unknown) => {
      if (!active) return;
      setLoading(false);
      setError(reason instanceof Error ? reason.message : "读取群组失败");
    });

    return () => {
      active = false;
      controller.abort();
    };
  }, [channelId, reloadToken]);

  const items = useMemo(() => {
    const rows: Array<{ key: string; label: string; msg: Msg }> = [];
    for (const message of humans) rows.push({ key: `human:${message.id}`, label: "我", msg: message });
    for (const session of Object.values(sessions)) {
      for (const message of session.msgs) {
        rows.push({ key: `${session.sessionId}:${message.id}`, label: session.label, msg: message });
      }
    }
    rows.sort((left, right) => {
      const byTime = String(left.msg.created_at ?? "").localeCompare(String(right.msg.created_at ?? ""));
      return byTime !== 0 ? byTime : left.key.localeCompare(right.key);
    });
    return rows;
  }, [humans, sessions]);

  const streamingIds = Object.keys(streaming).filter((key) => streaming[key]);
  const phase = streamingIds.length ? "streaming" : "idle";

  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [items.length]);

  async function handleSend(blocks: ContentBlock[]) {
    const text = blocks
      .filter((block) => block.type === "text")
      .map((block) => (block as { text?: string }).text ?? "")
      .join("");
    const attachmentIds = blocks
      .filter((block) => block.type === "data")
      .map((block) => attachmentIdFromBlock(block as { source?: { url?: string } }))
      .filter((value): value is string => Boolean(value));
    if (!text.trim() && !attachmentIds.length) return;
    setHumans((previous) => [...previous, UserMsg({ name: "user", content: blocks })]);
    try {
      await sendGroupMessage(channelId, text, attachmentIds);
      // 首次 @ 某位员工会新建他的 Session，重载一次时间线以订阅新流。
      setReloadToken((value) => value + 1);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "发送失败");
    }
  }

  async function handleInterrupt() {
    await Promise.all(
      streamingIds.map((employeeId) => interruptMember(channelId, employeeId).catch(() => undefined)),
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {channel && (
        <div className="border-b px-4 py-2 text-xs text-muted-foreground">
          {channel.name}
          {channel.description ? ` · ${channel.description}` : ""}
        </div>
      )}
      {notice && (
        <div className="mx-4 mt-2 flex items-start justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-900">
          <span>{notice}</span>
          <button type="button" className="shrink-0 underline" onClick={() => setNotice("")}>
            关闭
          </button>
        </div>
      )}
      {error && (
        <div className="mx-4 mt-2 rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive">
          {error}
        </div>
      )}

      <div ref={scroller} className="flex-1 overflow-y-auto px-4 py-4">
        <div className="mx-auto flex max-w-[48rem] flex-col gap-4">
          {loading && <div className="text-sm text-muted-foreground">正在读取群组…</div>}
          {!loading && !items.length && (
            <div className="text-sm text-muted-foreground">还没有消息，@ 一位成员开始讨论。</div>
          )}
          {items.map((item) => (
            <div key={item.key} className="flex flex-col gap-1">
              <div className="px-1 text-xs text-muted-foreground">{item.label}</div>
              <ASMessageBubble
                message={item.msg}
                onUserConfirm={async () => undefined}
              />
            </div>
          ))}
        </div>
      </div>

      <div className="px-4 pb-4">
        <div className="mx-auto w-full max-w-[48rem]">
          <TextInput
            onSend={(blocks: ContentBlock[]) => void handleSend(blocks)}
            disabled={loading}
            allowedInputTypes={["image/*", "application/pdf", "text/*"]}
            phase={phase}
            onInterrupt={() => void handleInterrupt()}
            fileProcessor={async (file: File) => {
              try {
                return await uploadAttachment(file);
              } catch (reason) {
                setNotice(reason instanceof Error ? reason.message : "附件上传失败");
                throw reason;
              }
            }}
          />
        </div>
      </div>
    </div>
  );
}
