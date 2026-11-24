import type { UploadedFile } from "./attachments";

export interface ChatDraft {
  text: string;
  files: UploadedFile[];
}
type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
export function draftKey(
  viewer: string,
  kind: "private" | "group",
  id: string,
) {
  return `nexus:conversation-draft:${encodeURIComponent(viewer)}:${kind}:${encodeURIComponent(id)}`;
}
export function readDraft(storage: DraftStorage, key: string): ChatDraft {
  try {
    const draft = JSON.parse(storage.getItem(key) || "null");
    if (typeof draft?.text === "string" && Array.isArray(draft.files)) {
      return {
        text: draft.text,
        files: draft.files.filter(
          (f: UploadedFile) =>
            f &&
            /^att_[a-f0-9]{32}$/.test(f.id) &&
            typeof f.name === "string" &&
            typeof f.type === "string",
        ),
      };
    }
  } catch {
    /* 禁用存储或草稿损坏时仍可输入。 */
  }
  return { text: "", files: [] };
}
export function writeDraft(
  storage: DraftStorage,
  key: string,
  draft: ChatDraft,
) {
  try {
    if (draft.text || draft.files.length)
      storage.setItem(key, JSON.stringify(draft));
    else storage.removeItem(key);
  } catch {
    /* 存储不可写时保留当前编辑内容。 */
  }
}
