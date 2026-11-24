import assert from "node:assert/strict";
import { test } from "node:test";
import { draftKey, readDraft, writeDraft } from "../src/nexus/drafts.ts";

test("已上传草稿依用户、会话类型与会话 ID 恢复", () => {
  const values = new Map<string, string>();
  const storage = {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => {
      values.set(key, value);
    },
    removeItem: (key: string) => {
      values.delete(key);
    },
  };
  const key = draftKey("甲", "group", "同一 ID");
  const draft = {
    text: "讨论草稿",
    files: [
      { id: "att_" + "f".repeat(32), name: "设计.md", type: "text/markdown" },
    ],
  };
  writeDraft(storage, key, draft);
  assert.deepEqual(readDraft(storage, key), draft);
  for (const other of [
    draftKey("乙", "group", "同一 ID"),
    draftKey("甲", "private", "同一 ID"),
    draftKey("甲", "group", "其他"),
  ]) {
    assert.deepEqual(readDraft(storage, other), { text: "", files: [] });
  }
  writeDraft(storage, key, { text: "", files: [] });
  assert.equal(storage.getItem(key), null);
});
