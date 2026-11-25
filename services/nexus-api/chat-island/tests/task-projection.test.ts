import assert from "node:assert/strict";
import test from "node:test";
import type { Msg } from "@agentscope-ai/agentscope/message";
import {
  TASK_TOOL_LABELS,
  parseTaskWakeHint,
  projectEntry,
  taskToolLabel,
  type ConversationToolResult,
} from "../src/nexus/conversationMessage.ts";

test("all nexus_task_* tools map to concise Chinese labels with or without MCP prefix", () => {
  const expected = [
    "nexus_task_create",
    "nexus_task_categories",
    "nexus_task_get",
    "nexus_task_list",
    "nexus_task_update",
    "nexus_task_assign",
    "nexus_task_start",
    "nexus_task_pause",
    "nexus_task_resume",
    "nexus_task_terminate",
    "nexus_task_retry",
    "nexus_task_logs",
    "nexus_task_result",
    "nexus_task_summary",
    "nexus_task_intervene",
    "nexus_task_assistance",
    "nexus_task_reply",
    "nexus_task_input",
    "nexus_task_confirm",
  ];
  for (const name of expected) {
    assert.ok(TASK_TOOL_LABELS[name], `missing label for ${name}`);
    assert.equal(taskToolLabel(name), TASK_TOOL_LABELS[name]);
    assert.equal(taskToolLabel(`mcp__nexus_task__${name}`), TASK_TOOL_LABELS[name]);
  }
  assert.equal(taskToolLabel("unknown_tool"), undefined);
});

test("parseTaskWakeHint parses nexus-task wake hints and ignores peer hints", () => {
  const waiting = parseTaskWakeHint(
    "nexus-task",
    "任务 T-20261125-ABCD1234 状态变为等待人工处理（事件 42）。原因：需要确认是否执行数据库迁移。请先调用 nexus_task_get 查询最新状态，再决定是否处理。",
  );
  assert.deepEqual(waiting && {
    taskId: waiting.taskId,
    status: waiting.status,
    statusLabel: waiting.statusLabel,
    eventId: waiting.eventId,
    reason: waiting.reason,
  }, {
    taskId: "T-20261125-ABCD1234",
    status: "WAITING",
    statusLabel: "待介入",
    eventId: "42",
    reason: "需要确认是否执行数据库迁移",
  });

  const done = parseTaskWakeHint(
    "nexus-task",
    "任务 T-20261125-DONE9999 状态变为已完成（事件 88）。请先调用 nexus_task_get 查询最新状态。",
  );
  assert.equal(done?.status, "SUCCEEDED");
  assert.equal(done?.statusLabel, "已完成");
  assert.equal(done?.reason, undefined);

  assert.equal(parseTaskWakeHint("星河", "任务 T-20261125-ABCD1234 状态变为已完成。"), undefined);
});

test("projectEntry enriches nexus_task tool results and attaches taskCallback on nexus-task hints", () => {
  const native: Msg = {
    id: "m1",
    role: "assistant",
    name: "星河",
    created_at: "2026-11-25T08:30:00Z",
    finished_at: "2026-11-25T08:30:05Z",
    finished_reason: "completed",
    metadata: {},
    content: [
      {
        type: "hint",
        id: "h1",
        source: "nexus-task",
        hint: "任务 T-20261125-ABCD1234 状态变为已完成（事件 19）。请先调用 nexus_task_get 查询最新状态。",
      },
      {
        type: "tool_call",
        id: "tc1",
        name: "mcp__nexus_task__nexus_task_categories",
        state: "finished",
        input: "{}",
      },
      {
        type: "tool_result",
        id: "tc1",
        name: "mcp__nexus_task__nexus_task_categories",
        state: "success",
        output: JSON.stringify({ ok: true, categories: [{ id: "infra" }, { id: "qa" }] }),
      },
      {
        type: "tool_call",
        id: "tc2",
        name: "mcp__nexus_task__nexus_task_create",
        state: "finished",
        input: '{"title":"优化连接池"}',
      },
      {
        type: "tool_result",
        id: "tc2",
        name: "mcp__nexus_task__nexus_task_create",
        state: "success",
        output: JSON.stringify({
          ok: true,
          task: {
            id: "T-20261125-ABCD1234",
            title: "优化连接池",
            status: "RUNNING",
            worker_name: "上海 Worker 01",
            current_progress: "正在运行回归测试",
          },
        }),
      },
    ],
  };

  const projected = projectEntry({
    native,
    sender: {
      senderId: "emp_1",
      senderName: "星河",
      senderKind: "employee",
      avatar: "robot-v1-0",
      self: false,
    },
  });
  assert.ok(Array.isArray(projected.content));

  const hintPart = projected.content.find((p) => p.type === "data" && p.name === "nexus-hint");
  assert.ok(hintPart && hintPart.type === "data");
  assert.equal((hintPart.data as { taskCallback?: { taskId: string; status: string } }).taskCallback?.taskId, "T-20261125-ABCD1234");
  assert.equal((hintPart.data as { taskCallback?: { status: string } }).taskCallback?.status, "SUCCEEDED");

  const catTool = projected.content.find((p) => p.type === "tool-call" && p.toolCallId === "tc1");
  assert.ok(catTool && catTool.type === "tool-call");
  assert.equal((catTool.result as ConversationToolResult).summary, "2 个可用分类");

  const createTool = projected.content.find((p) => p.type === "tool-call" && p.toolCallId === "tc2");
  assert.ok(createTool && createTool.type === "tool-call");
  const createRes = createTool.result as ConversationToolResult;
  assert.equal(createRes.summary, "优化连接池 · 执行中");
  assert.deepEqual(createRes.task, {
    taskId: "T-20261125-ABCD1234",
    status: "RUNNING",
    text: "优化连接池",
    workerName: "上海 Worker 01",
    progress: "正在运行回归测试",
    waitingReason: undefined,
  });
});
