/**
 * Compact formatters aligned with chat-island `utils/common.ts`
 * (`formatNumber` / `formatTime`) for bubble footers.
 */

export function formatNumber(num: number): string {
  if (num < 1000) return num.toLocaleString();
  const units = [
    { value: 1e9, suffix: "B" },
    { value: 1e6, suffix: "M" },
    { value: 1e3, suffix: "k" },
  ];
  for (const { value, suffix } of units) {
    if (num >= value) {
      const formatted = num / value;
      const decimals = formatted >= 10 ? 1 : 2;
      return formatted.toFixed(decimals).replace(/\.0+$/, "") + suffix;
    }
  }
  return num.toLocaleString();
}

/**
 * Duration in seconds → compact integer units: `45s`, `2m30s`, `3h`, …
 * Matches island `formatTime` (not the datetime helper in `@/utils/format`).
 */
export function formatTime(
  seconds: number,
  options: { leadingUnitOnly?: boolean } = {},
): string {
  const total = Math.floor(seconds);
  if (total < 60) return `${total}s`;
  if (total < 3600) {
    const minutes = Math.floor(total / 60);
    const remaining = total % 60;
    if (options.leadingUnitOnly || remaining === 0) return `${minutes}m`;
    return `${minutes}m${remaining}s`;
  }
  if (total < 86400) return `${Math.floor(total / 3600)}h`;
  if (total < 2629746) return `${Math.floor(total / 86400)}d`;
  if (total < 31556952) return `${Math.floor(total / 2629746)}mo`;
  return `${Math.floor(total / 31556952)}y`;
}

export function messagePlainText(text: string | undefined | null): string {
  return (text || "").trim();
}

/** Island: explicit `finished_at === null` ⇒ running; missing ⇒ legacy finished. */
export function isMessageRunning(finishedAt: string | null | undefined): boolean {
  return finishedAt === null;
}

export function messageElapsedSeconds(opts: {
  createdAt?: string | null;
  finishedAt?: string | null;
  nowMs: number;
  running: boolean;
}): number | null {
  const startMs = Date.parse(opts.createdAt || "");
  if (!Number.isFinite(startMs)) return null;
  const endMs = opts.running
    ? opts.nowMs
    : Date.parse(opts.finishedAt || "");
  if (!opts.running && !Number.isFinite(endMs)) return null;
  return Math.max(0, ((opts.running ? opts.nowMs : endMs) - startMs) / 1000);
}
