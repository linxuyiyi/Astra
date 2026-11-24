import DOMPurify from 'dompurify'
import { Marked } from 'marked'

export interface MarkdownHeading {
  id: string
  level: number
  text: string
}

export interface ParsedFrontmatter {
  meta: Array<{ key: string; value: string }>
  body: string
}

export interface RenderedMarkdown {
  html: string
  headings: MarkdownHeading[]
}

function escapeHtml(text: string): string {
  return text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

/**
 * 拆解标准 YAML Frontmatter（例如 SKILL.md 顶部的 --- ... --- 区块），
 * 供阅读视图以结构化元数据胶囊呈现，正文交由 Markdown 渲染。
 */
export function parseFrontmatter(source: string): ParsedFrontmatter {
  const normalized = (source || '').replace(/\r\n/g, '\n')
  const match = normalized.match(/^---\n([\s\S]*?)\n---\n?([\s\S]*)$/)
  if (!match) return { meta: [], body: normalized }

  const rawYaml = match[1]
  const body = match[2] ?? ''
  const meta: Array<{ key: string; value: string }> = []
  let currentKey = ''
  let currentVal: string[] = []

  const flush = () => {
    if (!currentKey) return
    const joined = currentVal.join(' ').trim().replace(/^["']|["']$/g, '')
    if (joined) meta.push({ key: currentKey, value: joined })
    currentKey = ''
    currentVal = []
  }

  for (const line of rawYaml.split('\n')) {
    const kv = line.match(/^([A-Za-z0-9_-]+)\s*:\s*(.*)$/)
    if (kv) {
      flush()
      currentKey = kv[1]
      const val = kv[2].trim()
      if (val && val !== '|' && val !== '>') currentVal.push(val)
    } else if (currentKey && /^\s+/.test(line)) {
      const trimmed = line.trim()
      if (trimmed) currentVal.push(trimmed)
    }
  }
  flush()

  return { meta, body }
}

/**
 * 安全渲染 Markdown 为受信任 HTML，同时提取标题大纲并为代码块注入语言栏与复制按钮。
 */
export function renderMarkdown(source: string): RenderedMarkdown {
  const headings: MarkdownHeading[] = []
  const slugCount = new Map<string, number>()

  const makeId = (rawText: string) => {
    const plain = rawText.replace(/<[^>]+>/g, '').trim()
    const base = plain
      .toLowerCase()
      .replace(/[^\w\u4e00-\u9fa5\s-]/g, '')
      .replace(/\s+/g, '-')
      .slice(0, 48) || 'section'
    const count = slugCount.get(base) ?? 0
    slugCount.set(base, count + 1)
    return count === 0 ? `md-${base}` : `md-${base}-${count}`
  }

  const instance = new Marked({ gfm: true, breaks: true })
  instance.use({
    renderer: {
      heading({ tokens, depth }) {
        const html = this.parser.parseInline(tokens)
        const plain = html.replace(/<[^>]+>/g, '').trim()
        const id = makeId(plain)
        if (depth >= 1 && depth <= 3 && plain) {
          headings.push({ id, level: depth, text: plain })
        }
        return `<h${depth} id="${id}" class="md-heading">${html}</h${depth}>\n`
      },
      code({ text, lang }) {
        const language = (lang || '').trim().split(/\s+/)[0]
        const label = language || 'text'
        const encoded = encodeURIComponent(text)
        return `<div class="md-code-block"><div class="md-code-block__head"><span class="md-code-block__lang">${escapeHtml(label)}</span><button type="button" class="md-code-block__copy" data-md-copy="${encoded}">复制</button></div><pre><code class="language-${escapeHtml(label)}">${escapeHtml(text)}</code></pre></div>\n`
      },
      table(token) {
        const headerCells = token.header
          .map((cell, idx) => {
            const align = token.align[idx] ? ` style="text-align:${token.align[idx]}"` : ''
            return `<th${align}>${this.parser.parseInline(cell.tokens)}</th>`
          })
          .join('')
        const bodyRows = token.rows
          .map((row) => {
            const tds = row
              .map((cell, idx) => {
                const align = token.align[idx] ? ` style="text-align:${token.align[idx]}"` : ''
                return `<td${align}>${this.parser.parseInline(cell.tokens)}</td>`
              })
              .join('')
            return `<tr>${tds}</tr>`
          })
          .join('')
        return `<div class="md-table-wrap"><table><thead><tr>${headerCells}</tr></thead><tbody>${bodyRows}</tbody></table></div>\n`
      },
      link({ href, title, tokens }) {
        const text = this.parser.parseInline(tokens)
        const safeHref = href || ''
        const isExternal = /^https?:\/\//i.test(safeHref)
        const titleAttr = title ? ` title="${escapeHtml(title)}"` : ''
        const targetAttr = isExternal ? ' target="_blank" rel="noopener noreferrer"' : ''
        return `<a href="${escapeHtml(safeHref)}"${titleAttr}${targetAttr}>${text}</a>`
      },
    },
  })

  const rawHtml = instance.parse(source || '', { async: false }) as string
  const html = DOMPurify.sanitize(rawHtml, {
    USE_PROFILES: { html: true },
    ADD_ATTR: ['target', 'rel', 'data-md-copy', 'id', 'class'],
    ADD_TAGS: ['button'],
  })

  return { html, headings }
}
