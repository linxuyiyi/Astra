import assert from 'node:assert/strict';
import { test } from 'node:test';

import { draftKey, readDraft, writeDraft } from '../src/nexus/drafts.ts';

function storage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => { values.set(key, value); },
    removeItem: (key: string) => { values.delete(key); },
  };
}

test('私聊和群聊的同名 ID 不共享草稿', () => {
  const store = storage();
  const contact = draftKey('?conversation=same');
  const group = draftKey('?channel=same');
  writeDraft(store, contact, { text: '私聊未发送', files: [] });
  writeDraft(store, group, { text: '群聊未发送', files: [] });
  assert.equal(readDraft(store, contact).text, '私聊未发送');
  assert.equal(readDraft(store, group).text, '群聊未发送');
  writeDraft(store, contact, { text: '', files: [] });
  assert.equal(store.getItem(contact), null);
  assert.equal(readDraft(store, group).text, '群聊未发送');
});

test('切换可恢复已上传附件，但不恢复未完成上传', () => {
  const store = storage();
  const block = { type: 'data' as const, id: 'a', created_at: '', name: 'note.txt', source: { type: 'url' as const, url: '/api/v1/attachments/a', media_type: 'text/plain' } };
  writeDraft(store, 'one', { text: '', files: [
    { name: 'note.txt', status: 'done', block },
    { name: 'pending.txt', status: 'processing', block: null },
  ] });
  assert.deepEqual(readDraft(store, 'one').files, [{ name: 'note.txt', status: 'done', block }]);
});

test('损坏或不可访问的存储不阻塞输入', () => {
  const store = storage();
  store.setItem('bad', '{');
  assert.deepEqual(readDraft(store, 'bad'), { text: '', files: [] });
  const denied = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); }, removeItem() { throw new Error('denied'); } };
  assert.deepEqual(readDraft(denied, 'one'), { text: '', files: [] });
  assert.doesNotThrow(() => writeDraft(denied, 'one', { text: '草稿', files: [] }));
});
