/**
 * Nexus 的时间带 UTC 偏移，AgentScope 原生消息使用本地 ISO 时间。
 * 与上游消息组件一样按 Date 解释时间，再比较同一时间轴；不能直接比较字符串，
 * 否则流式新消息的 UTC 时间会被排在旧的本地时间消息前面。
 */
export function compareTimelineItems(
  left: { key: string; msg: { created_at?: string } },
  right: { key: string; msg: { created_at?: string } },
): number {
  const difference = Date.parse(left.msg.created_at ?? '') - Date.parse(right.msg.created_at ?? '');
  return difference || left.key.localeCompare(right.key);
}
