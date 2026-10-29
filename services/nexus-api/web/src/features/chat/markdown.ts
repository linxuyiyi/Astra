import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

export function renderMarkdown(source: string): string {
  const html = marked.parse(source || "", { async: false }) as string;
  return DOMPurify.sanitize(html, { USE_PROFILES: { html: true } });
}

/** 工具日志超过该行数时折叠，避免长输出撑爆气泡。 */
export const LOG_COLLAPSE_LINES = 12;

export function shouldCollapse(text: string, limit = LOG_COLLAPSE_LINES): boolean {
  if (!text) return false;
  if (text.length > 800) return true;
  return text.split("\n").length > limit;
}

export function previewLines(text: string, limit = 4): string {
  const lines = text.split("\n");
  if (lines.length <= limit) return text;
  return `${lines.slice(0, limit).join("\n")}\n…`;
}
