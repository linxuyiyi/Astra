import { appendEvent } from "@agentscope-ai/agentscope/message";
import type { ContentBlock } from "@agentscope-ai/agentscope/message";
import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ASMessageBubble } from "@/components/chat/ASMessageBubble";
import { TextInput } from "@/components/chat/TextInput";
import { attachmentIdFromBlock, uploadAttachment } from "./attachments";
import { type GroupChannel, type GroupItem, interruptGroup, loadGroupTimeline, sendGroupMessage, streamChannelEvents } from "./group";
import { nexusFetch } from "./http";
import "./group.css";
import { EmployeeAvatar } from "./EmployeeAvatar";

function merge(previous: GroupItem[], incoming: GroupItem[]): GroupItem[] {
  const result = new Map(previous.map(item => [item.id, item]));
  for (const raw of incoming) {
    const metadata = (raw.message.metadata ?? {}) as Record<string, unknown>;
    let message = structuredClone(raw.message);
    for (const event of (metadata.stream_events ?? []) as never[]) message = appendEvent(message, event);
    delete (message.metadata as Record<string, unknown>).stream_events;
    const previous = result.get(raw.id);
    const oldCursor = Number((previous?.message.metadata as Record<string, unknown> | undefined)?.stream_cursor ?? 0);
    if (Number(metadata.stream_cursor ?? 0) < oldCursor) continue;
    result.set(raw.id, { ...raw, message });
  }
  return [...result.values()].sort((a, b) => a.seq - b.seq);
}

export function GroupApp({ channelId }: { channelId: string }) {
  const [channel, setChannel] = useState<GroupChannel | null>(null);
  const [items, setItems] = useState<GroupItem[]>([]);
  const [before, setBefore] = useState(0);
  const [loading, setLoading] = useState(true);
  const [olderLoading, setOlderLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [state, setState] = useState("idle");
  const [revoked, setRevoked] = useState(false);
  const [sending, setSending] = useState(false);
  const [failedDraft, setFailedDraft] = useState<{ text: string; attachments: string[]; key: string } | null>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const followLatest = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let cursor = 0;
    setItems([]); setLoading(true); setError(""); setRevoked(false);
    const refresh = async () => {
      const timeline = await loadGroupTimeline(channelId);
      if (!active) return;
      setChannel(timeline.channel);
      setItems(previous => merge(previous, timeline.items));
      setBefore(timeline.before);
      setState(timeline.channel.discussion?.state ?? "idle");
      return timeline;
    };
    const wait = () => new Promise<void>(resolve => {
      const timer = setTimeout(resolve, 1200);
      controller.signal.addEventListener("abort", () => { clearTimeout(timer); resolve(); }, { once: true });
    });
    void (async () => {
      const timeline = await refresh();
      if (!timeline || !active) return;
      cursor = timeline.event_cursor;
      setLoading(false);
      while (active) {
        try {
          for await (const event of streamChannelEvents(channelId, controller.signal, cursor)) {
            if (!active) return;
            if (event.type === "revoked") {
              setRevoked(true); setItems([]); setError("你已退出群组或登录已失效"); return;
            }
            cursor = Math.max(cursor, event.cursor);
            if (event.type === "message" && event.item) {
              const item = event.item;
              setItems(previous => merge(previous, [item]));
            }
            if (event.type === "delta" && event.event) {
              setItems(previous => previous.map(item => item.id === event.message_id
                ? Number((item.message.metadata as Record<string, unknown> | undefined)?.stream_cursor ?? 0) >= event.cursor ? item
                  : { ...item, message: { ...appendEvent(structuredClone(item.message), event.event as never),
                      metadata: { ...item.message.metadata, stream_cursor: event.cursor } } } : item));
            }
            if (event.type === "discussion") {
              setState(event.state ?? "idle");
              if (event.detail) setNotice(event.detail);
            }
            if (event.type === "members") {
              await refresh();
              window.parent.postMessage({ type: "nexus-group-members", channelId }, location.origin);
            }
          }
        } catch (reason) {
          if (!active) return;
          setError(reason instanceof Error ? reason.message : "实时连接中断，正在重连");
        }
        if (!active) return;
        await wait();
        if (active) setError("");
      }
    })().catch(reason => { if (active) { setLoading(false); setError(String(reason)); } });
    return () => { active = false; controller.abort(); };
  }, [channelId]);

  useLayoutEffect(() => {
    const node = scroller.current;
    if (node && followLatest.current) node.scrollTop = node.scrollHeight;
  }, [items]);

  useEffect(() => {
    const latest = items.at(-1)?.seq;
    if (latest && !revoked) void nexusFetch(`/api/v1/channels/${channelId}/read`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seq: latest }),
    });
  }, [items.at(-1)?.seq, channelId, revoked]);

  async function sendDraft(draft: { text: string; attachments: string[]; key: string }) {
    setSending(true); setError("");
    try {
      const result = await sendGroupMessage(channelId, draft.text, draft.attachments, draft.key);
      setItems(previous => merge(previous, [result.message])); setFailedDraft(null);
      followLatest.current = true; setAtBottom(true);
    } catch (reason) {
      setFailedDraft(draft); setError(reason instanceof Error ? reason.message : "发送失败");
    } finally { setSending(false); }
  }
  function handleSend(blocks: ContentBlock[]) {
    const text = blocks.filter(b => b.type === "text").map(b => (b as { text?: string }).text ?? "").join("");
    const attachments = blocks.filter(b => b.type === "data").map(b => attachmentIdFromBlock(b as { source?: { url?: string } })).filter((x): x is string => Boolean(x));
    if (!text.trim() && !attachments.length) return;
    void sendDraft({ text, attachments, key: crypto.randomUUID() });
  }
  async function older() {
    setOlderLoading(true);
    const node = scroller.current;
    const height = node?.scrollHeight ?? 0;
    followLatest.current = false;
    try {
      const page = await loadGroupTimeline(channelId, before);
      setItems(previous => merge(previous, page.items)); setBefore(page.before);
      requestAnimationFrame(() => { if (node) node.scrollTop += node.scrollHeight - height; });
    } catch (reason) { setError(String(reason)); }
    finally { setOlderLoading(false); }
  }
  const busy = state === "queued" || state === "running";
  return <div className="group-chat flex h-full min-h-0 flex-col">
    <div className="flex items-center justify-between border-b px-4 py-2 text-xs text-muted-foreground" role="status">
      <span>{busy ? "数字员工正在讨论，新消息可以调整讨论" : "群组交流"}</span>
      {busy && <button className="rounded border px-2 py-1" onClick={() => void interruptGroup(channelId).catch(reason => setError(String(reason)))}>停止讨论</button>}
    </div>
    {notice && <div className="mx-4 mt-2 text-xs text-amber-700">{notice}<button className="ml-3 underline" onClick={() => setNotice("")}>关闭</button></div>}
    {error && <div role="alert" className="mx-4 mt-2 text-sm text-destructive">{error}</div>}
    {failedDraft && <div className="mx-4 mt-2 text-sm">未发送：{failedDraft.text.slice(0, 120)} <button className="underline" disabled={sending} onClick={() => void sendDraft(failedDraft)}>重试发送</button></div>}
    <div ref={scroller} className="flex-1 overflow-y-auto px-4 py-4" onScroll={() => {
      const node = scroller.current; if (!node) return;
      const follow = node.scrollHeight - node.scrollTop - node.clientHeight < 48;
      followLatest.current = follow; setAtBottom(follow);
    }}>
      <div className="mx-auto flex max-w-[48rem] flex-col gap-4">
        {before > 0 && <button className="text-xs text-muted-foreground underline" disabled={olderLoading} onClick={() => void older()}>{olderLoading ? "正在加载…" : "加载更早消息"}</button>}
        {loading && <div className="text-sm text-muted-foreground">正在读取群组…</div>}
        {!loading && !items.length && !revoked && <div className="text-sm text-muted-foreground">向大家打个招呼，或直接询问相关岗位的工作。</div>}
        {items.map(item => {
          if (item.sender_type === "system") {
            const taskId = (item.message.metadata as Record<string, unknown> | undefined)?.task_id;
            return <div key={item.id} className="text-center text-xs text-muted-foreground" data-kind="system">
              {item.text}
              {typeof taskId === "string" && <a className="ml-2 underline" target="_parent" href={`/tasks/${encodeURIComponent(taskId)}`}>查看任务</a>}
            </div>;
          }
          const self = item.sender_type === "human" && item.sender_id === channel?.viewer_id;
          const who = channel?.members.find(m => m.member_id === item.sender_id);
          return <div key={item.id} className="group-message" data-self={String(self)} data-sender={item.sender_id} data-seq={item.seq}>
            <div className="group-message-avatar" aria-hidden="true">{item.sender_type === "employee" ? <EmployeeAvatar avatar={who?.avatar ?? ""} /> : who?.avatar || "👤"}</div>
            <div className="group-message-body">
              <div className="group-message-label"><span>{item.sender_name}{item.sender_type === "employee" ? " · 数字员工" : ""}</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}</time></div>
              <ASMessageBubble message={item.message} onUserConfirm={async () => undefined} />
            </div>
          </div>;
        })}
      </div>
    </div>
    {!atBottom && <button className="mx-auto mb-2 rounded-full border px-3 py-1 text-xs" onClick={() => {
      followLatest.current = true; setAtBottom(true); if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
    }}>回到最新消息</button>}
    <div className="px-4 pb-4"><div className="mx-auto w-full max-w-[48rem]">
      <TextInput onSend={handleSend} disabled={loading || revoked || sending} phase="idle"
        allowedInputTypes={["image/*", "application/pdf", "text/*"]}
        fileProcessor={async file => uploadAttachment(file)} />
    </div></div>
  </div>;
}
