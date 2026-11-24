import type { AttachmentAdapter, PendingAttachment } from "@assistant-ui/react";
import { nexusFetch } from "./http";

export interface UploadedFile {
  id: string;
  name: string;
  type: string;
}
export type UploadScope = { conversation_id: string } | { channel_id: string };
export const attachmentUrl = (id: string) =>
  `/api/v1/attachments/${encodeURIComponent(id)}`;

export function nexusAttachmentAdapter(
  scope: UploadScope,
  currentIds: () => readonly string[],
  signal: AbortSignal,
): AttachmentAdapter {
  const adding = new Set<string>();
  return {
    accept: "*",
    async *add({ file }) {
      if (new Set([...currentIds(), ...adding]).size >= 6)
        throw new Error("每条消息最多添加 6 个附件");
      if (!file.size || file.size > 20 * 1024 * 1024)
        throw new Error("单个附件需大于 0 字节且不超过 20 MiB");
      const id = `att_${Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("")}`;
      const item = {
        id,
        file,
        name: file.name,
        type: file.type.startsWith("image/") ? "image" : "document",
        contentType: file.type,
      };
      adding.add(id);
      try {
        yield {
          ...item,
          status: { type: "running", reason: "uploading", progress: 0 },
        } satisfies PendingAttachment;
        const query = new URLSearchParams({ ...scope, name: file.name });
        const response = await nexusFetch(`${attachmentUrl(id)}?${query}`, {
          method: "PUT",
          body: file,
          signal,
        });
        const body = await response.json();
        if (!response.ok)
          throw new Error(
            typeof body.detail === "string"
              ? body.detail
              : `上传失败（HTTP ${response.status}）`,
          );
        yield {
          ...item,
          name: body.name,
          contentType: body.type,
          status: { type: "requires-action", reason: "composer-send" },
        };
      } catch (error) {
        if (signal.aborted) return;
        yield {
          ...item,
          status: {
            type: "incomplete",
            reason: "error",
            message: error instanceof Error ? error.message : "附件上传失败",
          },
        };
      } finally {
        adding.delete(id);
      }
    },
    async send(item) {
      if (item.status.type !== "requires-action")
        throw new Error("请处理未上传的附件");
      return { ...item, content: [], status: { type: "complete" } };
    },
    async remove() {
      // Nexus 回收未发送的上传，移除草稿附件无需删除服务端记录。
    },
  };
}
