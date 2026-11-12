import type { DataBlock } from "@agentscope-ai/agentscope/message";

import { nexusFetch } from "./http";

export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;

interface AttachmentMeta {
  id: string;
  name: string;
  type: string;
  size: number;
}

export async function uploadAttachment(
  file: File,
  scope: { conversation_id: string } | { channel_id: string },
): Promise<DataBlock> {
  if (!file.size) throw new Error("不能添加空文件");
  if (file.size > MAX_ATTACHMENT_BYTES) throw new Error("单个附件不能超过 20 MB");

  const id = `att_${crypto.randomUUID().replaceAll("-", "")}`;
  const path = `/api/v1/attachments/${id}`;
  const query = new URLSearchParams({ ...scope, name: file.name || "未命名文件" });
  const response = await nexusFetch(`${path}?${query}`, { method: "PUT", body: file });
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: string } | null;
    throw new Error(payload?.detail || "附件上传失败");
  }
  const meta = (await response.json()) as AttachmentMeta;
  return {
    type: "data",
    id: meta.id,
    name: meta.name,
    created_at: new Date().toISOString(),
    source: { type: "url", url: path, media_type: meta.type },
  };
}

export function attachmentIdFromBlock(block: { id?: string }): string | null {
  return block.id && /^att_[0-9a-f]{32}$/.test(block.id) ? block.id : null;
}
