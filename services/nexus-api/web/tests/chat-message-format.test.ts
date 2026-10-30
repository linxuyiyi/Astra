import assert from "node:assert/strict";
import test from "node:test";
import { isAssistantMessageRunning } from "../src/features/chat/messageFormat.ts";

test("real native user messages with null finished_at cannot mask the pending reply or show an attachment-only spinner", () => {
  assert.equal(isAssistantMessageRunning({ sender: "user", sender_type: "human", finished_at: null }), false);
  assert.equal(isAssistantMessageRunning({ sender: "", sender_type: "system", finished_at: null }), false);
  assert.equal(isAssistantMessageRunning({ sender: "employee-one", sender_type: "employee", finished_at: null }), true);
  assert.equal(isAssistantMessageRunning({ sender: "employee-one", sender_type: "employee", finished_at: "2026-10-30T00:00:00Z" }), false);
});
