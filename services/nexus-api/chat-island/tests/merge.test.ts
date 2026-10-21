import assert from "node:assert/strict";
import { test } from "node:test";

import { mergeHistory, mergeHumans } from "../src/nexus/merge.ts";

type Msg = { id: string; role?: string; content?: unknown[] };

const user = (id: string): Msg => ({ id, role: "user", content: [] });
const reply = (id: string): Msg => ({ id, role: "assistant", content: [] });

test("历史已包含刚发送的消息时不重复", () => {
  const history = [user("msg-server"), reply("reply-1")];
  const merged = mergeHistory(history, [user("local-1")], (id) => (id === "local-1" ? "msg-server" : id));
  assert.deepEqual(merged.map((item) => item.id), ["msg-server", "reply-1"]);
});

test("历史尚未落盘时保留乐观气泡", () => {
  const merged = mergeHistory([], [user("local-1")], (id) => id);
  assert.deepEqual(merged.map((item) => item.id), ["local-1"]);
});

test("首轮回复在连流前完成时，历史补回答案并保留用户气泡", () => {
  const history = [reply("reply-1")];
  const merged = mergeHistory(history, [user("local-1")], (id) => id);
  assert.deepEqual(merged.map((item) => item.id), ["reply-1", "local-1"]);
  assert.equal(merged[0].role, "assistant");
});

test("本地消息已落盘但 id 不同：用服务端 id 替换，不产生第二条", () => {
  const history = [user("msg-server")];
  const merged = mergeHistory(history, [{ id: "local-1", role: "user", content: [] }], () => "msg-server");
  assert.deepEqual(merged.map((item) => item.id), ["msg-server"]);
});

test("群事件按 id 合并，已有对象不被覆盖", () => {
  const first = { id: "m1", role: "user", content: [{ type: "text", text: "一" }] };
  const merged = mergeHumans([first], [first, { id: "m2", role: "user", content: [] }]);
  assert.deepEqual(merged.map((item) => item.id), ["m1", "m2"]);
  assert.equal(merged[0], first);
});
