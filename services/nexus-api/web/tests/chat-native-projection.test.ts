import assert from "node:assert/strict";
import test from "node:test";
import { reactive } from "vue";
import type { Msg } from "@agentscope-ai/agentscope/message";
import type { AgentEvent } from "@agentscope-ai/agentscope/event";
import { applyMessageEvent, projectMessage, timelineMessage } from "../src/features/chat/nativeMessage.ts";
import { mergeMessages } from "../src/features/chat/sessionState.ts";
const time = "2026-10-30T00:00:00Z";
const native: Msg = { id: "reply-1", name: "员工一", role: "assistant", content: [], metadata: {}, created_at: time, finished_at: null };
const identity = { id: "employee-one", kind: "employee" as const, name: "员工一" };

test("native history preserves thinking, failed tool results, images, usage and terminal reason", () => {
  const raw = { ...native, content: [
    { id: "t", type: "thinking", thinking: "check logs", created_at: time },
    { id: "h", type: "hint", hint: "peer context", source: "同事提示", created_at: time },
    { id: "c", type: "tool_call", name: "Read", input: "{}", state: "finished", created_at: time },
    { id: "c", type: "tool_result", name: "Read", output: "permission denied", state: "error", created_at: time },
    { id: "i", type: "data", name: "result.png", source: { type: "base64", media_type: "image/png", data: "AA==" }, created_at: time },
  ], finished_at: time, finished_reason: "error", usage: { input_tokens: 10, output_tokens: 5 } } as Msg;
  const projected = projectMessage(raw, identity);
  assert.deepEqual(projected.thinking, ["check logs"]);
  assert.equal(projected.hints?.[0].text, "peer context");
  assert.equal(projected.tools?.[0].state, "error"); assert.equal(projected.tools?.[0].output, "permission denied");
  assert.equal(projected.data?.[0].name, "result.png"); assert.equal(projected.usage?.input_tokens, 10);
  assert.equal(projected.finished_reason, "error"); assert.strictEqual(projected.native, raw);
});

test("group sender identity comes from the canonical envelope, including system and other humans", () => {
  const item = { id: "system-one", seq: 1, sender_type: "system" as const, sender_id: "", sender_name: "系统", text: "created", created_at: time, discussion_id: "", message: { ...native, id: "system-one", role: "system" as const } };
  assert.equal(timelineMessage(item).sender_type, "system");
  const human = timelineMessage({ ...item, sender_type: "human", sender_id: "other-human", sender_name: "另一位用户", message: { ...item.message, role: "user" } });
  assert.equal(human.speaker, "other-human"); assert.equal(human.sender_name, "另一位用户");
});

test("replayed group events use the SDK exactly once and old history cannot overwrite a newer terminal message", () => {
  const start = { type: "TEXT_BLOCK_START", id: "1-0", reply_id: "reply-1", block_id: "b", created_at: time } as AgentEvent;
  let message = applyMessageEvent(projectMessage(structuredClone(native), identity), start, 1);
  const delta = { type: "TEXT_BLOCK_DELTA", id: "2-0", reply_id: "reply-1", block_id: "b", delta: "hello", created_at: time } as AgentEvent;
  message = applyMessageEvent(message, delta, 2);
  assert.equal(applyMessageEvent(message, delta, 2).text, "hello");
  const completed = applyMessageEvent(message, { type: "REPLY_END", id: "3-0", reply_id: "reply-1", finished_reason: "completed", created_at: time } as AgentEvent, 3);
  assert.equal(mergeMessages([completed], [message])[0].finished_at, time);
  assert.equal(mergeMessages([completed], [message])[0].text, "hello");
});

test("SDK events can be applied to the Vue reactive messages used by the real chat window", () => {
  const message = reactive(projectMessage(structuredClone(native), identity));
  const event = { type: "TEXT_BLOCK_START", id: "4-0", reply_id: "reply-1", block_id: "b", created_at: time } as AgentEvent;
  const result = applyMessageEvent(message, event, 4);
  assert.equal(result.native?.content[0].type, "text");
});
