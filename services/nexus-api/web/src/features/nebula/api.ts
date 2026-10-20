import type { NebulaSnapshot } from "./types";

async function read<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = (await response.json()) as { detail?: string };
      if (typeof payload.detail === "string") detail = payload.detail;
    } catch {
      /* 保留状态文本 */
    }
    throw new Error(detail || "请求失败");
  }
  return (await response.json()) as T;
}

export async function getNebulaSnapshot(): Promise<NebulaSnapshot> {
  return read(await fetch("/api/v1/nebula/snapshot"));
}
