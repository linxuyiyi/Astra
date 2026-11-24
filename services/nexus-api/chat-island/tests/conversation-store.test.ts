import assert from "node:assert/strict";
import test from "node:test";
import { setTimeout as wait } from "node:timers/promises";
import { ConversationStore } from "../src/nexus/conversationStore.ts";

const time = "2026-11-18T08:00:00.000Z";
const employee = { id: "dev", name: "开发", avatar: "avatar", domain: "研发", enabled: true };
const person = {
  member_type: "employee",
  member_id: "dev",
  enabled: true,
  name: "开发",
  avatar: "a",
  role: "member",
};
function privatePage(extra = {}) {
  return {
    messages: [],
    viewer_id: "alice",
    session_id: "native-1",
    employee,
    before: null,
    event_cursor: "42-0",
    is_running: false,
    ...extra,
  };
}
function native(id, text, role = "assistant", meta = {}) {
  return {
    id,
    name: role === "assistant" ? "开发" : "alice",
    role,
    metadata: meta,
    created_at: time,
    content: [{ type: "text", text, id: "text", created_at: time }],
    finished_at: time,
  };
}
function reply(type, id, fields = {}) {
  return { type, id, reply_id: "reply", created_at: time, ...fields };
}
function storage() {
  const entries = new Map();
  return {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, item) => entries.set(key, item),
    removeItem: (key) => entries.delete(key),
  };
}
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 3);
globalThis.cancelAnimationFrame = clearTimeout;

function browser(t, handler) {
  const local = storage();
  local.setItem("nexus_owner_id", "alice");
  for (const [key, value] of Object.entries({
    localStorage: local,
    sessionStorage: storage(),
    fetch: handler,
  })) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, key);
    Object.defineProperty(globalThis, key, { value, configurable: true, writable: true });
    t.after(() =>
      previous ? Object.defineProperty(globalThis, key, previous) : Reflect.deleteProperty(globalThis, key),
    );
  }
}
function socket() {
  let stream;
  return {
    open(signal) {
      return new Response(
        new ReadableStream({
          start(controller) {
            stream = controller;
            signal.addEventListener(
              "abort",
              () => {
                try {
                  controller.close();
                } catch {}
              },
              { once: true },
            );
          },
        }),
      );
    },
    event(data, id = "") {
      stream.enqueue(
        new TextEncoder().encode((id ? `id: ${id}\n` : "") + "data: " + JSON.stringify(data) + "\n\n"),
      );
    },
    close() {
      stream.close();
    },
  };
}
async function until(fn) {
  const limit = Date.now() + 3000;
  while (!fn()) {
    if (Date.now() >= limit) throw Error("条件未满足");
    await wait(5);
  }
}
function textOf(message) {
  return message?.content.find((p) => p.type === "text")?.text;
}

test("私聊断线按快照 checkpoint 重放，乱序与重复 UUID 只应用一次，旧消息复用引用", async (t) => {
  const saved = native("prior", "历史");
  const first = socket(),
    second = socket();
  let connections = 0;
  let persisted = false;
  browser(t, async (url, init) => {
    assert.equal(init.headers.get("x-owner-id"), "alice");
    if (url.includes("/stream?")) {
      assert.match(url, persisted ? /after=44-0$/ : /after=42-0$/);
      return (++connections === 1 ? first : second).open(init.signal);
    }
    return Response.json(persisted
      ? privatePage({ messages: [saved, native("reply", "一二")], event_cursor: "44-0" })
      : privatePage({ messages: [saved], is_running: true }));
  });
  const store = new ConversationStore("private", "a");
  const end = store.start();
  t.after(end);
  await until(() => connections === 1);
  const prior = store.snapshot().messages[0];
  const data = reply("TEXT_BLOCK_DELTA", "event-2", { block_id: "b", delta: "一" });
  const start = reply("TEXT_BLOCK_START", "event-1", { block_id: "b" });
  first.event(reply("REPLY_START", "event-0", { role: "assistant", name: "开发" }));
  first.event(data);
  first.event(start);
  first.event(data);
  await until(() => textOf(store.snapshot().messages.find((m) => m.id === "reply")) === "一");
  assert.equal(
    store.snapshot().messages.find((m) => m.id === "prior"),
    prior,
  );
  first.close();
  await until(() => connections === 2);
  second.event(start);
  second.event(data);
  second.event(reply("TEXT_BLOCK_DELTA", "event-3", { block_id: "b", delta: "二" }));
  second.event(reply("REPLY_END", "event-4", { finished_reason: "completed" }));
  persisted = true;
  await until(() => store.snapshot().phase === "idle");
  assert.equal(textOf(store.snapshot().messages.find((m) => m.id === "reply")), "一二");
  assert.equal(store.snapshot().canSend, true);
});

test("先收到完成事件再收到提交回执，状态保持完成；未知提交重试使用相同幂等键", async (t) => {
  const events = socket(),
    ids = [];
  let accept,
    connected = false;
  let persisted = false;
  browser(t, async (url, init) => {
    if (url.includes("/stream")) {
      connected = true;
      return events.open(init.signal);
    }
    if (init.method === "POST") {
      ids.push(JSON.parse(init.body).client_id);
      if (ids.length === 1) throw new Error("回执丢失");
      return new Promise((resolve) => {
        accept = resolve;
      });
    }
    return Response.json(privatePage({ messages: persisted ? [native("reply", "已完成")] : [] }));
  });
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => connected);
  assert.equal(await store.send("请处理", []), "unconfirmed");
  assert.equal(store.snapshot().retryable, "send");
  const retry = store.retry();
  await until(() => Boolean(accept));
  events.event(reply("REPLY_START", "r1", { role: "assistant" }));
  events.event(reply("REPLY_END", "e1", { finished_reason: "completed" }));
  await until(() => store.snapshot().phase === "syncing");
  assert.equal(store.snapshot().messages.some((m) => m.metadata.custom.finishedReason), false);
  persisted = true;
  accept(
    Response.json({
      agentscope: { session_id: "native-1" },
      message: { id: "m", created_at: time, attachments: [] },
    }),
  );
  await retry;
  await until(() => store.snapshot().phase === "idle");
  assert.equal(ids[0], ids[1]);
  assert.equal(store.snapshot().phase, "idle");
  assert.equal(store.snapshot().messages.filter((m) => m.id === "m").length, 1);
});

test("停止只在服务端确认空闲后结束，停止失败可重试，卸载丢弃迟到的历史页", async (t) => {
  let interrupts = 0,
    connected = 0,
    active = true,
    delayed;
  const events = socket();
  browser(t, async (url, init) => {
    if (url.includes("before="))
      return new Promise((resolve) => {
        delayed = resolve;
      });
    if (url.endsWith("/interrupt"))
      return ++interrupts === 1
        ? Response.json({ detail: "停止失败" }, { status: 503 })
        : Response.json({ accepted: true });
    if (url.includes("/stream")) {
      connected++;
      return events.open(init.signal);
    }
    return Response.json(privatePage({
      messages: active ? [] : [native("reply", "", "assistant")],
      is_running: active, before: "older",
    }));
  });
  const store = new ConversationStore("private", "a");
  const close = store.start();
  t.after(close);
  await until(() => connected === 1);
  await store.stop();
  assert.equal(store.snapshot().canStop, true);
  assert.equal(store.snapshot().error, "停止失败");
  await store.stop();
  await until(() => connected === 2);
  assert.equal(store.snapshot().phase, "stopping");
  active = false;
  events.event(reply("REPLY_END", "end", { finished_reason: "interrupted" }));
  await until(() => store.snapshot().phase === "idle");
  const older = store.loadOlder();
  await until(() => Boolean(delayed));
  close();
  delayed(Response.json(privatePage({ messages: [native("old", "迟到")] })));
  await older;
  assert.ok(!store.snapshot().messages.some((m) => m.id === "old"));
});

test("群聊通过单条流处理多员工和其他成员；同一事件重复不刷新状态，撤权清空内容", async (t) => {
  const stream = socket();
  let connected = 0,
    after = "0";
  const marks = [];
  const members = [
    person,
    { ...person, member_id: "qa", name: "测试" },
    { ...person, member_id: "offline", enabled: false },
  ];
  browser(t, async (url, init) => {
    if (url.endsWith("/read")) {
      marks.push(JSON.parse(init.body).seq);
      return Response.json({ ok: true });
    }
    if (url.includes("/events?")) {
      connected++;
      after = new URL(url, "http://localhost").searchParams.get("after");
      return stream.open(init.signal);
    }
    return Response.json({
      channel: { name: "项目组", viewer_id: "alice", members },
      items: [],
      event_cursor: 2,
      before: 0,
    });
  });
  const store = new ConversationStore("group", "group");
  let memberChanges = 0;
  store.onMembersChanged = () => {
    memberChanges++;
  };
  t.after(store.start());
  await until(() => connected === 1);
  assert.deepEqual(
    store.snapshot().members.map((m) => m.id),
    ["dev", "qa"],
  );
  assert.equal(after, "2");
  const own = native("other", "其他人", "user");
  stream.event(
    {
      type: "message",
      item: { message: own, seq: 1, sender_type: "human", sender_id: "bob", sender_name: "同事" },
    },
    "3",
  );
  stream.event({ type: "discussion", state: "running", id: "d1" }, "4");
  for (const [id, seq, eventId] of [
    ["qa", 2, 5],
    ["dev", 3, 6],
  ]) {
    const item = { message: native(id, id), seq, sender_type: "employee", sender_id: id, sender_name: id };
    stream.event({ type: "message", item }, String(eventId));
    stream.event({ type: "message", item }, String(eventId));
  }
  await until(() => store.snapshot().messages.length === 3);
  assert.equal(store.snapshot().messages[0].metadata.custom.self, false);
  assert.deepEqual(
    store.snapshot().messages.map((m) => m.metadata.custom.senderId),
    ["bob", "qa", "dev"],
  );
  assert.deepEqual(marks, [1, 2, 3]);
  assert.equal(store.snapshot().canSend, true);
  stream.event({ type: "members" }, "7");
  await until(() => memberChanges === 1 && connected === 2);
  stream.event({ type: "revoked" });
  await until(() => store.snapshot().canSend === false);
  assert.equal(store.snapshot().messages.length, 0);
});

test("绑定其他用户的会话返回 404 时禁止读写，首次加载失败可重连", async (t) => {
  let calls = 0;
  browser(t, async () =>
    ++calls === 1
      ? Response.json({ detail: "暂时离线" }, { status: 502 })
      : Response.json({ detail: "对话不存在" }, { status: 404 }),
  );
  const store = new ConversationStore("private", "other-user");
  t.after(store.start());
  await until(() => store.snapshot().retryable);
  await store.retry();
  await until(() => store.snapshot().error === "对话不存在");
  assert.equal(store.snapshot().canSend, false);
  assert.deepEqual(store.snapshot().messages, []);
});

test("刷新从消息快照恢复文件、同事、失败工具结果与真实待确认状态", async (t) => {
  const upload = native("own", "附件", "user", {
    attachments: [{ id: "att_a", name: "测试图.png", type: "image/png" }],
  });
  upload.content.push({ type: "data", source: { type: "base64", data: "eA==", media_type: "image/png" } });
  const peer = native("peer", "已经复查", "assistant", { peer_employee_id: "qa", peer_avatar: "qa.png" });
  peer.name = "测试";
  const current = native("reply", "正在检查");
  current.finished_at = null;
  current.content.push(
    { type: "hint", source: '{"label":"system","sublabel":"runtime state"}', hint: "隐藏内部提醒" },
    { type: "tool_call", id: "read", name: "Read", input: '{"path":"a"}', state: "finished" },
    { type: "tool_result", id: "read", name: "Read", output: "文件读取失败", state: "error" },
    { type: "tool_call", id: "approve", name: "review_report", input: "{}", state: "asking" },
  );
  const events = socket();
  browser(t, (url, init) =>
    url.includes("/stream")
      ? events.open(init.signal)
      : Response.json(privatePage({ messages: [upload, peer, current], is_running: false })),
  );
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => !store.snapshot().loading);
  const { messages, phase } = store.snapshot();
  assert.equal(phase, "approval");
  const tools = messages.find((m) => m.id === "reply").content;
  assert.ok(!tools.some((p) => p.name === "nexus-hint"));
  assert.equal(tools.find((p) => p.toolCallId === "read").isError, true);
  assert.equal(tools.find((p) => p.toolCallId === "read").result.output, "文件读取失败");
  const files = messages.find((m) => m.id === "own").content.filter((p) => p.name === "nexus-file");
  assert.deepEqual(
    files.map((p) => p.data.url),
    ["/api/v1/attachments/att_a"],
  );
  assert.equal(messages.find((m) => m.id === "peer").metadata.custom.avatar, "qa.png");
});

test("终态快照需要等待真实运行锁释放，长流缺帧从持久正文恢复", async (t) => {
  const stream = socket();
  let reads = 0;
  let listening = false;
  const finished = { ...native("reply", "服务端持久的完整回复"), finished_reason: "completed" };
  browser(t, (url, init) => {
    if (url.includes("/stream")) {
      listening = true;
      return stream.open(init.signal);
    }
    return Response.json(privatePage({
      messages: ++reads > 1 ? [finished] : [],
      is_running: reads <= 2,
      event_cursor: reads > 1 ? "43-0" : "42-0",
    }));
  });
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => listening);
  stream.event(reply("TEXT_BLOCK_DELTA", "remaining", { block_id: "missing-start", delta: "不完整" }));
  stream.event(reply("REPLY_END", "end", { finished_reason: "completed" }));
  await until(() => store.snapshot().phase === "syncing");
  assert.equal(store.snapshot().messages[0].metadata.custom.finishedReason, undefined);
  await until(() => reads === 2 && textOf(store.snapshot().messages[0]) === "服务端持久的完整回复");
  assert.equal(store.snapshot().canSend, false);
  await until(() => store.snapshot().canSend);
  assert.equal(reads, 3);
  assert.equal(store.snapshot().messages[0].metadata.custom.finishedReason, "completed");
});

test("直接打开已落盘但仍持锁的私聊，只有确认空闲后才恢复发送", async (t) => {
  const stream = socket();
  let reads = 0;
  browser(t, (url, init) => url.includes("/stream") ? stream.open(init.signal) :
    Response.json(privatePage({
      messages: [{ ...native("saved", "已落盘"), finished_reason: "completed" }],
      is_running: ++reads === 1,
    })));
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => store.snapshot().phase === "syncing");
  assert.equal(store.snapshot().canSend, false);
  await until(() => store.snapshot().canSend);
  assert.equal(reads, 2);
  assert.equal(textOf(store.snapshot().messages[0]), "已落盘");
});

test("取回未确认消息编辑后，原内容重发仍复用幂等键", async (t) => {
  const stream = socket();
  const sent = [];
  browser(t, (url, init) => {
    if (url.includes("/stream")) return stream.open(init.signal);
    if (init.method !== "POST") return Response.json(privatePage());
    sent.push(JSON.parse(init.body));
    return Response.json({ detail: "投递结果未知" }, { status: 503 });
  });
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => store.snapshot().canSend);
  const files = [{ id: "att_test", name: "记录.txt", type: "text/plain" }];
  assert.equal(await store.send("原稿", files), "unconfirmed");
  assert.equal(store.snapshot().canSend, false);
  assert.deepEqual(store.restorePendingDraft(), { text: "原稿", files });
  assert.equal(await store.send("原稿", files), "unconfirmed");
  assert.equal(sent[0].client_id, sent[1].client_id);
  store.restorePendingDraft();
  assert.equal(await store.send("修改后的原稿", files), "unconfirmed");
  assert.notEqual(sent[1].client_id, sent[2].client_id);
});

test("实时协作提示与有独立 block_id 的工具文件直接投影，业务失败保留原因", async (t) => {
  const stream = socket();
  let listening = false;
  browser(t, (url, init) => {
    if (!url.includes("/stream")) return Response.json(privatePage());
    listening = true;
    return stream.open(init.signal);
  });
  const store = new ConversationStore("private", "a");
  t.after(store.start());
  await until(() => listening);
  stream.event(reply("HINT_BLOCK", "hint", { block_id: "new-block", source: "同事", hint: "已核对" }));
  stream.event(reply("TOOL_CALL_START", "call", { tool_call_id: "artifact", tool_call_name: "contact_employee" }));
  stream.event(reply("TOOL_RESULT_START", "result", { tool_call_id: "artifact", tool_call_name: "contact_employee" }));
  stream.event(reply("TOOL_RESULT_TEXT_DELTA", "text", {
    tool_call_id: "artifact", delta: '{"ok":false,"error":"该员工已禁用"}',
  }));
  stream.event(reply("TOOL_RESULT_DATA_DELTA", "file", {
    tool_call_id: "artifact", block_id: "generated", media_type: "text/plain", data: "eA==",
  }));
  stream.event(reply("TOOL_RESULT_END", "done", { tool_call_id: "artifact", state: "success" }));
  await until(() => store.snapshot().messages[0]?.content.some((part) => part.isError));
  const blocks = store.snapshot().messages[0].content;
  assert.equal(blocks.find((part) => part.name === "nexus-hint").data.text, "已核对");
  assert.equal(blocks.find((part) => part.name === "nexus-file").data.url, "data:text/plain;base64,eA==");
  assert.equal(blocks.find((part) => part.type === "tool-call").result.summary, "该员工已禁用");
});

test("群聊跳过分页外增量，当前执行人和后续消息正常更新", async (t) => {
  const stream = socket();
  let listening = 0;
  const item = (id, seq) => ({
    message: native(id, "当前群消息", "user"), seq, sender_type: "human", sender_id: "bob", sender_name: "同事",
  });
  browser(t, (url, init) => {
    if (url.includes("/events")) {
      listening++;
      return stream.open(init.signal);
    }
    if (url.endsWith("/read")) return Response.json({ ok: true });
    return Response.json({
      channel: { name: "项目组", viewer_id: "alice", members: [person],
        discussion: { id: "d", state: "running", active_employee_id: "dev" } },
      items: [item("recent", 46)], before: 46, event_cursor: 200,
    });
  });
  const store = new ConversationStore("group", "a");
  t.after(store.start());
  await until(() => listening === 1);
  assert.equal(store.snapshot().activeEmployee, "开发");
  stream.event({ type: "delta", message_id: "old", seq: 30, event: reply("TEXT_BLOCK_DELTA", "old-delta") }, "201");
  stream.event({ type: "message", item: item("new", 96) }, "202");
  await until(() => store.snapshot().messages.some((msg) => msg.id === "new"));
  assert.equal(listening, 1);
  assert.equal(store.snapshot().messages.some((msg) => msg.id === "old"), false);
  assert.equal(store.snapshot().error, "");
});
