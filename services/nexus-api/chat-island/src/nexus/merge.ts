/**
 * 聊天岛的消息合并规则（纯函数，便于 node --test 覆盖）。
 *
 * 原生历史优先；本地乐观消息按“本地 id → 服务端 id”映射对账后补到末尾。
 * 绑定刚更新时历史可能还没落盘，也可能已经包含这一轮回复，两种情况都不能丢消息。
 */
import type { Msg } from "@agentscope-ai/agentscope/message";

export function mergeHistory(
  history: Msg[],
  pending: Msg[],
  resolve: (id: string) => string,
): Msg[] {
  if (pending.length === 0) return history;
  const ids = new Set(history.map((message) => message.id));
  const merged = [...history];
  for (const message of pending) {
    const resolved = resolve(message.id);
    if (ids.has(resolved)) continue;
    ids.add(resolved);
    merged.push(resolved === message.id ? message : { ...message, id: resolved });
  }
  return merged;
}

/** 按 id 合并两批消息；已有的保持原对象与顺序。 */
export function mergeHumans(existing: Msg[], incoming: Msg[]): Msg[] {
  if (incoming.length === 0) return existing;
  const merged = new Map(existing.map((message) => [message.id, message]));
  for (const message of incoming) {
    if (!merged.has(message.id)) merged.set(message.id, message);
  }
  return [...merged.values()];
}
