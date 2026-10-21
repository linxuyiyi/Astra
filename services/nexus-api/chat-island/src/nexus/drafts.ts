import type { ContentBlock } from '@agentscope-ai/agentscope/message';

export interface DraftFile {
  name: string;
  status: 'processing' | 'done';
  block: ContentBlock | null;
}

export interface ChatDraft {
  text: string;
  files: DraftFile[];
}

type DraftStorage = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function draftKey(search: string): string {
  const params = new URLSearchParams(search);
  const channel = params.get('channel');
  const conversation = params.get('conversation');
  return channel ? `nexus:chat-draft:channel:${channel}`
    : conversation ? `nexus:chat-draft:conversation:${conversation}` : '';
}

export function readDraft(storage: DraftStorage | null, key: string): ChatDraft {
  const empty: ChatDraft = { text: '', files: [] };
  if (!key || !storage) return empty;
  try {
    const value = JSON.parse(storage.getItem(key) || 'null') as ChatDraft | null;
    if (!value || typeof value.text !== 'string' || !Array.isArray(value.files)) return empty;
    return {
      text: value.text,
      files: value.files.filter((file) => file && typeof file.name === 'string'
        && file.status === 'done' && file.block?.type === 'data'),
    };
  } catch {
    return empty;
  }
}

export function writeDraft(storage: DraftStorage | null, key: string, draft: ChatDraft): void {
  if (!key || !storage) return;
  const files = draft.files.filter((file) => file.status === 'done' && file.block);
  try {
    if (!draft.text && !files.length) storage.removeItem(key);
    else storage.setItem(key, JSON.stringify({ text: draft.text, files }));
  } catch {
    // 浏览器禁用或耗尽会话存储时，仍允许正常输入和发送。
  }
}
