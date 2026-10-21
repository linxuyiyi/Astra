import assert from 'node:assert/strict';
import { test } from 'node:test';

import { compareTimelineItems } from '../src/nexus/timeline.ts';

process.env.TZ = 'Asia/Shanghai';

test('带时区的人类消息、本地时间的历史回复、UTC 流式回复按实际先后排列', () => {
  const rows = [
    { key: 'new-reply', msg: { created_at: '2026-10-21T06:28:07.000Z' } },
    { key: 'old-reply', msg: { created_at: '2026-10-21T14:22:37.228480' } },
    { key: 'new-user', msg: { created_at: '2026-10-21T06:28:06.766670+00:00' } },
    { key: 'old-user', msg: { created_at: '2026-10-21T06:22:37.047177+00:00' } },
  ];
  assert.deepEqual(rows.sort(compareTimelineItems).map((row) => row.key), ['old-user', 'old-reply', 'new-user', 'new-reply']);
});

test('同一时刻保持确定顺序', () => {
  const a = { key: 'a', msg: { created_at: '2026-10-21T14:22:37' } };
  const b = { key: 'b', msg: { created_at: '2026-10-21T06:22:37Z' } };
  assert.ok(compareTimelineItems(a, b) < 0);
});
