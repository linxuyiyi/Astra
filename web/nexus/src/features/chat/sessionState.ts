import type { ChatMessage } from "@/types";

export function mergeMessages(current: ChatMessage[], incoming: ChatMessage[]): ChatMessage[] {
  const byId = new Map<string, ChatMessage>();
  for (const message of current) byId.set(message.id, message);
  for (const message of incoming) {
    const previous = byId.get(message.id);
    byId.set(message.id, {
      ...previous,
      ...message,
      attachments: message.attachments?.length ? message.attachments : previous?.attachments ?? [],
    });
  }
  const position = new Map<string, number>();
  current.forEach((message, index) => position.set(message.id, index));
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
