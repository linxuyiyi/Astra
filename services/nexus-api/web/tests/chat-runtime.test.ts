import assert from "node:assert/strict";
import test from "node:test";
import { ChatRuntime, createSessionState } from "../src/features/chat/ChatRuntime.ts";
import type { ChatAdapter, ChatAdapterEvent, ChatSendOutcome, ChatTarget } from "../src/features/chat/types.ts";

Object.defineProperty(globalThis, "window", { value: globalThis, configurable: true });
Object.defineProperty(globalThis, "localStorage", { value: { getItem: () => "review-owner" }, configurable: true });
const message = { id: "user-1", sender: "user", text: "hello", attachments: [], created_at: "2026-10-30T00:00:00Z" };
const target = { key: "contact:one", kind: "contact", id: "one", name: "员工一", enabled: true } as ChatTarget;
function harness() {
  let active = true;
  let emit!: (event: ChatAdapterEvent) => void;
  const signals: AbortSignal[] = [];
  const state = createSessionState(target);
  const outcome = (): ChatSendOutcome => ({ message, messages: [], activity: "idle", notice: "" });
  const adapter: ChatAdapter = {
    key: target.key, kind: "contact", title: "员工一", subtitle: "", canSend: true,
    conversationId: "one", sessionId: "session-one", hasOlder: false,
    load: async () => [], send: async () => outcome(), taskQuery: () => ({}),
    interrupt: async () => ({ activity: "idle", messages: [] }),
    follow: (signal, callback) => {
      signals.push(signal); emit = callback;
      emit({ type: "messages", messages: [] }); emit({ type: "activity", phase: "idle" });
      return new Promise(resolve => signal.addEventListener("abort", () => resolve(), { once: true }));
    },
  };
  const runtime = new ChatRuntime(state, () => active);
  runtime.bind(adapter);
  return { state, adapter, runtime, signals, outcome, emit: (event: ChatAdapterEvent) => emit(event), activate: (value: boolean) => { active = value; } };
}

test("a terminal event arriving before send resolves cannot leave the UI streaming", async () => {
  const h = harness(); h.state.draft = "hello";
  h.adapter.send = async () => { h.emit({ type: "finished" }); return { ...h.outcome(), activity: "streaming" }; };
  assert.equal(await h.runtime.send("hello", []), true);
  assert.equal(h.state.phase, "idle"); assert.equal(h.runtime.streaming, false);
  assert.equal(h.state.draft, ""); h.runtime.dispose();
});

test("sending a message with no triggered group reply stays idle instead of showing a permanent stop button", async () => {
  const h = harness();
  await h.runtime.send("just a note", []);
  assert.equal(h.state.phase, "idle"); assert.equal(h.runtime.streaming, false);
  h.runtime.dispose();
});

test("failed send keeps the draft and retry reuses the same idempotency key", async () => {
  const h = harness(); h.state.draft = "hello";
  const keys: string[] = [];
  h.adapter.send = async (_text, _ids, _signal, key) => {
    keys.push(key); if (keys.length === 1) throw new Error("503"); return h.outcome();
  };
  assert.equal(await h.runtime.send("hello", []), false);
  assert.equal(h.state.draft, "hello");
  assert.equal(await h.runtime.retryFailedSend(), true);
  assert.equal(keys[0], keys[1]); assert.equal(h.state.failedSend, null);
  h.runtime.dispose();
});

test("switching away aborts the stream, keeps the draft, and resumes exactly one stream on return", () => {
  const h = harness(); h.state.draft = "unsent";
  h.activate(false); h.runtime.suspend();
  assert.equal(h.signals[0].aborted, true); assert.equal(h.state.draft, "unsent");
  h.runtime.resume(); assert.equal(h.signals.length, 1);
  h.activate(true); h.runtime.resume(); h.runtime.resume();
  assert.equal(h.signals.length, 2); h.runtime.dispose();
});

test("a conversation binding resolving after selection changed cannot start a background subscription", () => {
  const h = harness(); h.activate(false); h.runtime.suspend();
  h.runtime.bind(h.adapter);
  assert.equal(h.signals.length, 1);
  h.activate(true); h.runtime.resume();
  assert.equal(h.signals.length, 2); h.runtime.dispose();
});

test("stop remains pending until a service terminal event, while rejected stop preserves the previous activity", async () => {
  const h = harness(); h.emit({ type: "activity", phase: "streaming" });
  h.adapter.interrupt = async () => ({ activity: "streaming", messages: [] });
  await h.runtime.stop(); assert.equal(h.state.phase, "interrupting");
  h.emit({ type: "finished" }); assert.equal(h.state.phase, "idle");
  h.emit({ type: "activity", phase: "streaming" });
  h.adapter.interrupt = async () => { throw new Error("unavailable"); };
  await h.runtime.stop(); assert.equal(h.state.phase, "streaming"); assert.equal(h.state.noticeSource, "stop");
  h.adapter.interrupt = async () => ({ activity: "idle", messages: [] });
  await h.runtime.stop(); assert.equal(h.state.phase, "idle"); assert.equal(h.state.notice, "");
  h.runtime.dispose();
});

test("successful background attachment upload remains attached to its original conversation", async () => {
  const h = harness();
  const originalFetch = globalThis.fetch;
  let finish!: (response: Response) => void;
  globalThis.fetch = async () => new Promise(resolve => { finish = resolve; });
  try {
    const uploading = h.runtime.attach([new File(["text"], "review.txt", { type: "text/plain" })]);
    assert.equal(h.state.draftPending.length, 1);
    h.activate(false);
    finish(Response.json({ id: "att-one", name: "review.txt", type: "text/plain", size: 4 }));
    await uploading;
    assert.equal(h.state.draftFiles[0].id, "att-one"); assert.equal(h.state.draftPending.length, 0);
    assert.equal(h.state.uploading, false);
  } finally { globalThis.fetch = originalFetch; h.runtime.dispose(); }
});

test("loss of group membership disables the session and aborts its stream", () => {
  const h = harness(); h.emit({ type: "unavailable", text: "你已不在该群组" });
  assert.equal(h.state.status, "error"); assert.equal(h.signals[0].aborted, true); h.runtime.dispose();
});
