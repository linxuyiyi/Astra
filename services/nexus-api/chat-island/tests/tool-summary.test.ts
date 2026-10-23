import assert from 'node:assert/strict';
import { test } from 'node:test';
import { summarizeToolGroup } from '../src/components/chat/tool-summary.ts';

const t = (key: string, params?: Record<string, unknown>) => `${key}:${params?.count}`;
const pair = (state: string) => ({
  call: { id: 'write', name: 'Write', type: 'tool_call', input: '{}', state: 'finished', created_at: '' },
  result: { id: 'write', name: 'Write', type: 'tool_result', state, output: 'ToolNotFoundError', created_at: '', finished_at: null },
});

test('失败的 Write 不得汇总为成功编辑文件', () => {
  const summary = summarizeToolGroup([pair('error')] as never, t);
  assert.equal(summary.title, 'Tool.summary.failed:1');
  assert.equal(summary.insertions, 0);
});

test('只汇总成功编辑的行数', () => {
  const summary = summarizeToolGroup([pair('error'), pair('success')] as never, t, () => ({ insertions: 3, deletions: 1 }));
  assert.equal(summary.insertions, 3);
  assert.equal(summary.deletions, 1);
  assert.match(summary.title, /edit:1/);
  assert.match(summary.title, /failed:1/);
});
