/**
 * 附件上传：走 Nexus 的 /api/v1/attachments，保留 owner 归属与大小限制。
 *
 * 上游 TextInput 的 fileProcessor 只接受一个 ContentBlock。这里用 DataBlock 的
 * URL source 携带 Nexus 附件地址，发送时再从 URL 里取回 attachment id，
 * 不在浏览器里做 base64 传递。
 */
import type { DataBlock } from "@agentscope-ai/agentscope/message";

import { nexusFetch } from "./http";

export const MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024;

interface AttachmentMeta {
  id: string;
  name: string;
  type: string;
  size: number;
}

export async function uploadAttachment(file: File): Promise<DataBlock> {
  if (!file.size) throw new Error("不能添加空文件");
  if (file.size > MAX_ATTACHMENT_BYTES) throw new Error("单个附件不能超过 20 MB");

  const form = new FormData();
  form.append("file", file, file.name || "未命名文件");
  const response = await nexusFetch("/api/v1/attachments", { method: "POST", body: form });
  if (!response.ok) {
    let detail = response.statusText || "附件上传失败";
    try {
      const payload = (await response.json()) as { detail?: string };
      if (payload.detail) detail = payload.detail;
    } catch {
      // 保留状态文本
    }
    throw new Error(detail);
  }
  const meta = (await response.json()) as AttachmentMeta;
  return {
    type: "data",
    id: `att-${meta.id}`,
    name: meta.name,
    created_at: new Date().toISOString(),
    source: {
      type: "url",
      url: `/api/v1/attachments/${meta.id}`,
      media_type: meta.type,
    },
  };
}
