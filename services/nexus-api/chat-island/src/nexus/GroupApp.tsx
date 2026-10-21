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
import { mergeHumans as mergeHumanMessages } from "./merge";
import {
  type GroupChannel,
  type GroupStream,
  interruptMember,
  loadGroupTimeline,
  sendGroupMessage,
  streamChannelEvents,
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

    // 已订阅的成员 Session：key 与时间线一致（员工 id 优先）。
    const subscribed = new Map<string, string>();

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

    const subscribe = (member: GroupStream) => {
      const key = member.employee_id || member.session_id;
      if (subscribed.has(key)) return;
      subscribed.set(key, member.session_id);
      setSessions((previous) => ({
        ...previous,
        [key]: {
          sessionId: member.session_id,
          employeeId: member.employee_id,
          label: member.label,
          cursor: member.event_cursor ?? "",
          msgs: member.messages ?? [],
        },
      }));
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
    };

    const appendHuman = (message: Msg) =>
      setHumans((previous) =>
        previous.some((item) => item.id === message.id) ? previous : [...previous, message],
      );

    const mergeHumans = (messages: Msg[]) =>
      setHumans((previous) => mergeHumanMessages(previous, messages));

    const wait = (ms: number) =>
      new Promise<void>((resolve) => {
        let timer: ReturnType<typeof setTimeout>;
        const done = () => {
          clearTimeout(timer);
          controller.signal.removeEventListener("abort", done);
          resolve();
        };
        timer = setTimeout(done, ms);
        controller.signal.addEventListener("abort", done, { once: true });
      });

    // 别人 @ 出新员工时会新建 Session；只增量接入新的会话，不打断已有的流。
    // 同时按 id 合并人类消息，用于补上断线期间漏掉的记录。
    const adopt = async () => {
      const timeline = await loadGroupTimeline(channelId);
      if (!active) return;
      setChannel(timeline.channel);
      const human = timeline.streams.find((item) => item.session_id === timeline.channel.id);
      if (human?.messages?.length) mergeHumans(human.messages);
      const known = new Set(subscribed.values());
      for (const stream of timeline.streams) {
        if (stream.session_id === timeline.channel.id || known.has(stream.session_id)) continue;
        subscribe(stream);
      }
    };

    (async () => {
      const timeline = await loadGroupTimeline(channelId);
      if (!active) return;
      setChannel(timeline.channel);
      const human = timeline.streams.find((item) => item.session_id === timeline.channel.id);
      setHumans(human?.messages ?? []);
      setStreaming({});
      for (const member of timeline.streams) {
        if (member.session_id === timeline.channel.id) continue;
        subscribe(member);
      }
      setLoading(false);

      // 人类消息事件：补上其他成员发出的消息，以及刚建出来的成员 Session。
      // 连接会断：有限退避重连，重连后先从持久时间线补缺口（只重连实时流
      // 会丢掉断线期间的消息）。
      void (async () => {
        let delay = 1000;
        while (active) {
          try {
            for await (const event of streamChannelEvents(channelId, controller.signal)) {
              if (!active) return;
              delay = 1000;
              if (event.message) appendHuman(event.message);
              const fresh = (event.sessions ?? []).filter(
                (item) => item.session_id && item.employee_id && !subscribed.has(item.employee_id),
              );
              if (fresh.length) await adopt();
            }
          } catch {
            // 断线或流被中断：下面补读并重连。
          }
          if (!active) return;
          await adopt().catch(() => undefined);
          if (!active) return;
          await wait(delay);
          delay = Math.min(delay * 2, 10000);
        }
      })().catch(() => undefined);
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
      const result = await sendGroupMessage(channelId, text, attachmentIds);
      // 投递结果是 200 响应里的业务字段：被 @ 的成员可能不接受（忙碌、禁用、
      // 或尚未创建），必须把后端原因显示出来，而不是当作发送成功。
      const details = [
        ...new Set(
          [result.record, ...result.triggered]
            .filter((item) => item.accepted === false && item.detail)
            .map((item) => String(item.detail)),
        ),
      ];
      setNotice(details.join("；"));
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
