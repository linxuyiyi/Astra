import assert from "node:assert/strict";
import test from "node:test";
import { GroupChatAdapter, PrivateChatAdapter } from "../src/features/chat/adapters.ts";
import { readEventStream } from "../src/api.ts";
import type { ChatAdapterEvent } from "../src/features/chat/types.ts";
import type { Channel, Employee } from "../src/types.ts";
const time = "2026-10-30T00:00:00Z";
Object.defineProperty(globalThis, "window", { value: globalThis, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: { getItem: () => "owner" }, configurable: true });
const channel = { id: "group-one", name: "验收群", members: [], discussion: null, viewer_id: "owner" } as unknown as Channel;
const native = { id: "reply-one", name: "员工一", role: "assistant", content: [], metadata: {}, created_at: time, finished_at: null };
const item = { id: "reply-one", seq: 2, sender_type: "employee", sender_id: "employee-one", sender_name: "员工一", created_at: time, text: "", discussion_id: "discussion-one", message: native };
function eventStream(events: Array<{ id?: string; body: object }>, signal?: AbortSignal | null) {
  return new Response(new ReadableStream({ start(controller) {
    for (const event of events) controller.enqueue(new TextEncoder().encode(`${event.id ? `id: ${event.id}\r\n` : ""}data: ${JSON.stringify(event.body)}\r\n\r\n`));
    signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")), { once: true });
  } }), { headers: { "Content-Type": "text/event-stream" } });
}

async function withFetch(callback: (url: string, init?: RequestInit) => Promise<Response> | Response, action: () => Promise<void>) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => callback(String(url), init);
  try { await action(); } finally { globalThis.fetch = original; }
}

test("SSE replay cursor remains separate from a discussion's semantic id", async () => {
  const abort = new AbortController();
  await withFetch((_url, init) => eventStream([{ id: "42", body: { type: "discussion", id: "discussion-one", state: "running" } }], init?.signal), async () => {
    await readEventStream("/events", abort.signal, event => {
      assert.equal(event.id, "discussion-one"); assert.equal(event.event_cursor, "42"); abort.abort();
    }).catch(error => { assert.equal(error.name, "AbortError"); });
  });
});

test("group send uses the canonical idempotency contract and does not require the removed triggered field", async () => {
  await withFetch((_url, init) => {
    const body = JSON.parse(String(init?.body));
    assert.equal(body.client_id, "stable-key-123");
    return Response.json({ message: { ...item, sender_type: "human", sender_id: "owner", sender_name: "你" }, participants: [], discussion_id: "", duplicate: false });
  }, async () => {
    const result = await new GroupChatAdapter(channel, []).send("note", [], new AbortController().signal, "stable-key-123");
    assert.equal(result.message.speaker, "owner"); assert.equal(result.activity, "idle");
  });
});

test("group follow consumes one ordered channel stream, preserves streamed content and handles id-less revocation", async () => {
  const abort = new AbortController();
  const emitted: ChatAdapterEvent[] = [];
  const calls: string[] = [];
  const events = [
    { id: "2", body: { type: "message", item } },
    { id: "3", body: { type: "delta", message_id: "reply-one", event: { type: "REPLY_START", reply_id: "reply-one", created_at: time } } },
    { id: "4", body: { type: "delta", message_id: "reply-one", event: { type: "TEXT_BLOCK_START", reply_id: "reply-one", block_id: "block-one", created_at: time } } },
    { id: "5", body: { type: "delta", message_id: "reply-one", event: { type: "TEXT_BLOCK_DELTA", reply_id: "reply-one", block_id: "block-one", delta: "hello", created_at: time } } },
    { id: "6", body: { type: "discussion", id: "discussion-one", state: "completed" } },
    { body: { type: "revoked" } },
  ];
  await withFetch((url, init) => {
    calls.push(url);
    if (url.includes("/messages")) return Response.json({ channel, items: [], before: 0, event_cursor: 1 });
    if (url.endsWith("/read")) return Response.json({ ok: true });
    return eventStream(events, init?.signal);
  }, async () => {
    const adapter = new GroupChatAdapter(channel, []);
    await adapter.follow(abort.signal, event => {
      emitted.push(event);
      if (event.type === "unavailable") abort.abort();
    });
    const messages = emitted.filter(event => event.type === "messages").flatMap(event => event.messages);
    assert.equal(messages.at(-1)?.text, "hello");
    assert.equal(emitted.some(event => event.type === "activity" && event.phase === "idle"), true);
    assert.equal(adapter.canSend, false);
    assert.equal(calls.some(url => url.includes("/members/")), false);
    assert.equal(calls.filter(url => url.includes("/events")).length, 1);
    assert.equal(calls.filter(url => url.endsWith("/read")).length, 1);
  });
});

test("native private history retains running state, and the stream accepts more than one reply", async () => {
  const abort = new AbortController();
  let replies = 0;
  const emitted: ChatAdapterEvent[] = [];
  const events: Array<{ id: string; body: object }> = [];
  let cursor = 2;
  for (const reply of ["one", "two"]) {
    const base = { reply_id: reply, created_at: time };
    for (const event of [
      { type: "REPLY_START", name: "员工一", ...base },
      { type: "TEXT_BLOCK_START", block_id: "b", ...base },
      { type: "TEXT_BLOCK_DELTA", block_id: "b", delta: reply, ...base },
      { type: "REPLY_END", finished_reason: "completed", ...base },
    ]) events.push({ id: `${cursor++}-0`, body: event });
  }
  await withFetch((url, init) => {
    if (url.includes("/stream")) return eventStream(events, init?.signal);
    if (url.endsWith("/messages")) return Response.json({ messages: [], is_running: false, event_cursor: "1-0" });
    return Response.json({ agentscope_session_id: "session-one" });
  }, async () => {
    const adapter = new PrivateChatAdapter({ id: "employee-one", name: "员工一", enabled: true } as Employee,
      { id: "conversation-one", agentscope_session_id: "session-one", name: "私聊", employee_id: "employee-one" });
    await adapter.follow(abort.signal, event => {
      emitted.push(event);
      if (event.type === "finished" && ++replies === 2) abort.abort();
    });
    const messages = emitted.filter(event => event.type === "messages").flatMap(event => event.messages);
    assert.equal(messages.some(message => message.id === "one" && message.text === "one"), true);
    assert.equal(messages.some(message => message.id === "two" && message.text === "two"), true);
    assert.equal(replies, 2);
  });
});

test("private peer replies retain their authoritative employee identity", async () => {
  await withFetch(url => url.endsWith("/messages")
    ? Response.json({ messages: [{ ...native, metadata: { peer_employee_id: "employee-peer" } }], is_running: false, event_cursor: "" })
    : Response.json({ agentscope_session_id: "session-one" }), async () => {
    const adapter = new PrivateChatAdapter({ id: "employee-one", name: "员工一", enabled: true } as Employee,
      { id: "conversation-one", agentscope_session_id: "session-one", name: "私聊", employee_id: "employee-one" });
    assert.equal((await adapter.load())[0].speaker, "employee-peer");
  });
});

test("an id-less upstream failure is displayed even after a saved replay cursor", async () => {
  const abort = new AbortController();
  let notice = "";
  await withFetch((url, init) => {
    if (url.includes("/stream")) return eventStream([{ body: { type: "NEXUS_UPSTREAM_ERROR", detail: "upstream unavailable" } }], init?.signal);
    if (url.endsWith("/messages")) return Response.json({ messages: [], is_running: false, event_cursor: "5-0" });
    return Response.json({ agentscope_session_id: "session-one" });
  }, async () => {
    const adapter = new PrivateChatAdapter({ id: "employee-one", name: "员工一", enabled: true } as Employee,
      { id: "conversation-one", agentscope_session_id: "session-one", name: "私聊", employee_id: "employee-one" });
    await adapter.follow(abort.signal, event => {
      if (event.type === "notice") { notice = event.text; abort.abort(); }
    });
    assert.equal(notice, "upstream unavailable");
  });
});

test("a failed event consumer cancels its response body before reconnecting", async () => {
  let canceled = false;
  await withFetch(() => new Response(new ReadableStream({
    start(controller) { controller.enqueue(new TextEncoder().encode('data: {"type":"message"}\n\n')); },
    cancel() { canceled = true; },
  })), async () => {
    await assert.rejects(readEventStream("/events", new AbortController().signal, () => { throw new Error("consumer failed"); }), /consumer failed/);
    assert.equal(canceled, true);
  });
});

test("a delayed private history snapshot cannot erase a newer streamed delta", async () => {
  const abort = new AbortController();
  let streamReady!: () => void, applied!: () => void, snapshotStarted!: () => void, finishSnapshot!: (response: Response) => void;
  const ready = new Promise<void>(resolve => { streamReady = resolve; });
  const deltaApplied = new Promise<void>(resolve => { applied = resolve; });
  const snapshotPending = new Promise<void>(resolve => { snapshotStarted = resolve; });
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let reads = 0;
  const snapshot = { messages: [{ ...native, content: [{ type: "text", id: "b", text: "first", created_at: time }] }], is_running: true, event_cursor: "1-0" };
  await withFetch((url, init) => {
    if (url.includes("/stream")) return new Response(new ReadableStream({ start(value) {
      controller = value; streamReady();
      init?.signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")), { once: true });
    } }));
    if (url.endsWith("/messages")) return ++reads === 1 ? Response.json(snapshot) : new Promise(resolve => { finishSnapshot = resolve; snapshotStarted(); });
    return Response.json({ agentscope_session_id: "session-one" });
  }, async () => {
    const adapter = new PrivateChatAdapter({ id: "employee-one", name: "员工一", enabled: true } as Employee,
      { id: "conversation-one", agentscope_session_id: "session-one", name: "私聊", employee_id: "employee-one" });
    const following = adapter.follow(abort.signal, event => {
      if (event.type === "messages" && event.messages.some(message => message.text === "first second")) applied();
    });
    try {
      await ready;
      const loading = adapter.load();
      // 让旧历史请求先发出，再送入一个更新的 SSE delta。
      await snapshotPending;
      controller.enqueue(new TextEncoder().encode('id: 2-0\ndata: {"type":"TEXT_BLOCK_DELTA","reply_id":"reply-one","block_id":"b","delta":" second","created_at":"2026-10-30T00:00:00Z"}\n\n'));
      await deltaApplied;
      finishSnapshot(Response.json(snapshot));
      assert.equal((await loading).find(message => message.id === "reply-one")?.text, "first second");
    } finally { abort.abort(); await following; }
  });
});
