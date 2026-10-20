import type { ChatMessage } from "@/types";

export function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of current) byId.set(message.id, message);
  const position = new Map<string, number>();
  current.forEach((message, index) => position.set(message.id, index));

  // 只按消息 id 合并：id 是服务端回显与本地乐观消息的唯一契约。
  // 曾经按「相同发送人 + 相同文本 + 30s 窗口」再做一层去重，会把用户连续发送的
  // 两条相同文字折叠成一条；这不是去重，是掩盖 id 不一致的 bug。
  for (const message of incoming) {
    const previous = byId.get(message.id);
    byId.set(message.id, {
      ...previous,
      ...message,
      attachments: message.attachments?.length ? message.attachments : previous?.attachments ?? [],
    });
  }

  incoming.forEach((message, index) => {
    if (!position.has(message.id)) position.set(message.id, current.length + index);
  });
  return [...byId.values()].sort((left, right) => {
    const leftTime = Date.parse(left.created_at || "");
    const rightTime = Date.parse(right.created_at || "");
    if (Number.isFinite(leftTime) && Number.isFinite(rightTime) && leftTime !== rightTime) {
      return leftTime - rightTime;
    }
    return (position.get(left.id) ?? 0) - (position.get(right.id) ?? 0);
  });
}

export function streamCursor(current: string, eventId: unknown): string {
  return typeof eventId === "string" && /^\d+-\d+$/.test(eventId) ? eventId : current;
}

export function maxCursor(left: string, right: string): string {
  if (!left) return right;
  if (!right) return left;
  const parse = (value: string) => value.split("-").map((part) => Number(part) || 0);
  const [leftReply, leftIndex] = parse(left);
  const [rightReply, rightIndex] = parse(right);
  if (rightReply !== leftReply) return rightReply > leftReply ? right : left;
  return rightIndex >= leftIndex ? right : left;
}

export function selectionIsCurrent(
  selectedKey: string,
  expectedKey: string,
  generation: number,
  expectedGeneration: number,
  disposed: boolean,
): boolean {
  return !disposed && selectedKey === expectedKey && generation === expectedGeneration;
}

export function sequenceIsCurrent(sequence: number, latest: number, aborted: boolean): boolean {
  return !aborted && sequence === latest;
}

export function sameIds(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}
