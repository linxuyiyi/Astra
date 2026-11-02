import DOMPurify from "dompurify";
import { marked } from "marked";

marked.setOptions({ gfm: true, breaks: true });

function escapeAttr(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** 仅代码块：横滑容器 + 右上 Copy。标题/列表/引用/表格由并行刀负责。 */
marked.use({
  renderer: {
    code({ text, lang, escaped }: { text: string; lang?: string; escaped?: boolean }) {
      const language = (lang || "").trim();
      const body = escaped ? text : escapeHtml(text);
      const className = language
        ? ` class="language-${escapeAttr(language)}"`
        : "";
      const langAttr = language ? ` data-lang="${escapeAttr(language)}"` : "";
      return (
        `<div class="md-codeblock" data-testid="md-codeblock"${langAttr}>` +
        `<button type="button" class="md-codeblock__copy" data-md-copy aria-label="复制代码">Copy</button>` +
        `<pre class="md-codeblock__pre"><code${className}>${body}</code></pre>` +
        `</div>\n`
      );
    },
  },
});

export function renderMarkdown(source: string): string {
  const html = marked.parse(source || "", { async: false }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ["data-md-copy", "data-lang", "data-testid", "aria-label"],
    ADD_TAGS: ["button"],
  });
}

export function previewLines(text: string, limit = 4): string {
  const lines = text.split("\n");
  if (lines.length <= limit) return text;
  return `${lines.slice(0, limit).join("\n")}\n…`;
}
