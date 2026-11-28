<template>
  <div class="md-view" :class="{ 'md-view--compact': compact, 'md-view--with-toc': showTocRail }">
    <div v-if="showHeaderBar" class="md-view__bar">
      <div v-if="frontmatter.meta.length" class="md-view__fm-inline">
        <span class="md-view__fm-badge">Frontmatter</span>
        <span v-for="item in frontmatter.meta.slice(0, 3)" :key="item.key" class="md-view__fm-chip">
          <b>{{ item.key }}</b>: {{ item.value }}
        </span>
      </div>
      <span v-else class="md-view__spacer" />
      <div class="md-view__actions">
        <ElRadioGroup v-if="allowSourceToggle" v-model="viewMode" size="small" aria-label="文档查看模式">
          <ElRadioButton value="rendered">阅读</ElRadioButton>
          <ElRadioButton value="source">源码</ElRadioButton>
        </ElRadioGroup>
        <ElButton v-if="allowCopy && content" size="small" text :icon="copied ? Check : Copy" @click="copyAll">
          {{ copied ? '已复制' : '复制' }}
        </ElButton>
      </div>
    </div>

    <div v-if="frontmatter.meta.length && viewMode === 'rendered' && !showHeaderBar" class="md-view__fm-card">
      <div v-for="item in frontmatter.meta" :key="item.key" class="md-view__fm-row">
        <span class="md-view__fm-key">{{ item.key }}</span>
        <span class="md-view__fm-val">{{ item.value }}</span>
      </div>
    </div>

    <div v-if="viewMode === 'source'" class="md-view__source-wrap">
      <pre class="md-view__source">{{ content || emptyText }}</pre>
    </div>

    <div v-else-if="!effectiveBody.trim()" class="md-view__empty">{{ emptyText }}</div>

    <div v-else class="md-view__layout">
      <article
        ref="articleRef"
        class="md-prose"
        @click="onArticleClick"
        v-html="rendered.html"
      />
      <aside v-if="showTocRail" class="md-toc" aria-label="文档目录">
        <div class="md-toc__title">目录</div>
        <nav class="md-toc__list">
          <button
            v-for="heading in rendered.headings"
            :key="heading.id"
            type="button"
            class="md-toc__item"
            :class="`md-toc__l${heading.level}`"
            :title="heading.text"
            @click="scrollToHeading(heading.id)"
          >{{ heading.text }}</button>
        </nav>
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { Check, Copy } from 'lucide-vue-next'
import { computed, onBeforeUnmount, ref } from 'vue'
import { ElButton, ElRadioButton, ElRadioGroup } from 'element-plus'
import { parseFrontmatter, renderMarkdown } from '@/utils/markdown'

const props = withDefaults(
  defineProps<{
    content: string
    stripFrontmatter?: boolean
    showToc?: boolean
    allowSourceToggle?: boolean
    allowCopy?: boolean
    compact?: boolean
    emptyText?: string
  }>(),
  {
    stripFrontmatter: false,
    showToc: false,
    allowSourceToggle: false,
    allowCopy: false,
    compact: false,
    emptyText: '暂无内容',
  },
)

const viewMode = ref<'rendered' | 'source'>('rendered')
const articleRef = ref<HTMLElement | null>(null)
const copied = ref(false)
let copyTimer = 0

const frontmatter = computed(() => (
  props.stripFrontmatter
    ? parseFrontmatter(props.content || '')
    : { meta: [], body: props.content || '' }
))

const effectiveBody = computed(() => frontmatter.value.body)
const rendered = computed(() => renderMarkdown(effectiveBody.value))
const showTocRail = computed(() => props.showToc && viewMode.value === 'rendered' && rendered.value.headings.length >= 3)
const showHeaderBar = computed(() => props.allowSourceToggle || props.allowCopy)

function scrollToHeading(id: string) {
  const el = articleRef.value?.querySelector<HTMLElement>(`#${CSS.escape(id)}`)
  el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

async function onArticleClick(event: MouseEvent) {
  const target = event.target as HTMLElement | null
  const button = target?.closest<HTMLButtonElement>('[data-md-copy]')
  if (!button) return
  const raw = decodeURIComponent(button.getAttribute('data-md-copy') || '')
  try {
    await navigator.clipboard.writeText(raw)
    const prev = button.textContent
    button.textContent = '已复制'
    button.classList.add('is-copied')
    window.setTimeout(() => {
      button.textContent = prev || '复制'
      button.classList.remove('is-copied')
    }, 1500)
  } catch {
    /* 剪贴板权限拒绝时静默 */
  }
}

async function copyAll() {
  if (!props.content) return
  try {
    await navigator.clipboard.writeText(props.content)
    copied.value = true
    window.clearTimeout(copyTimer)
    copyTimer = window.setTimeout(() => { copied.value = false }, 1600)
  } catch {
    copied.value = false
  }
}

onBeforeUnmount(() => window.clearTimeout(copyTimer))
</script>

<style scoped>
.md-view { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.md-view__bar {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px;
}
.md-view__spacer { flex: 1; }
.md-view__actions { display: flex; align-items: center; gap: 6px; margin-left: auto; }

.md-view__fm-inline { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; min-width: 0; }
.md-view__fm-badge {
  padding: 1px 6px; border-radius: 4px;
  background: var(--astra-bg-elevated); color: var(--astra-text-muted);
  font-family: var(--astra-font-mono); font-size: 11px; font-weight: 600;
}
.md-view__fm-chip {
  max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding: 1px 7px; border: 1px solid var(--astra-border); border-radius: 5px;
  background: var(--astra-bg-subtle); color: var(--astra-text-secondary); font-size: 12px;
}
.md-view__fm-chip b { color: var(--astra-text-primary); font-weight: 600; }

.md-view__fm-card {
  display: grid; gap: 6px; padding: 10px 12px;
  border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-subtle);
}
.md-view__fm-row { display: grid; grid-template-columns: 96px minmax(0, 1fr); gap: 8px; font-size: 12.5px; }
.md-view__fm-key { color: var(--astra-text-muted); font-family: var(--astra-font-mono); }
.md-view__fm-val { color: var(--astra-text-primary); word-break: break-word; }

.md-view__source-wrap {
  border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle); overflow: hidden;
}
.md-view__source {
  margin: 0; padding: 14px 16px;
  color: var(--astra-text-primary);
  font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.65;
  white-space: pre-wrap; overflow-wrap: anywhere;
}
.md-view__empty {
  padding: 24px 16px; border: 1px dashed var(--astra-border); border-radius: 10px;
  background: var(--astra-bg-subtle); color: var(--astra-text-muted); font-size: 13px; text-align: center;
}

.md-view__layout { display: grid; grid-template-columns: minmax(0, 1fr); gap: 20px; align-items: start; }
.md-view--with-toc .md-view__layout { grid-template-columns: minmax(0, 1fr) 176px; }

/* ── 文档目录（TOC） ─────────────────────────────────────────────── */
.md-toc {
  position: sticky; top: 8px;
  display: grid; gap: 6px;
  padding-left: 12px;
  border-left: 1px solid var(--astra-border);
  max-height: 60vh; overflow-y: auto;
}
.md-toc__title { color: var(--astra-text-muted); font-size: 11px; font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase; }
.md-toc__list { display: grid; gap: 2px; }
.md-toc__item {
  display: block; width: 100%; padding: 3px 4px;
  border: 0; border-radius: 4px; background: transparent;
  color: var(--astra-text-secondary); font-size: 12px; line-height: 1.45;
  text-align: left; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; cursor: pointer;
}
.md-toc__item:hover { background: var(--astra-bg-subtle); color: var(--astra-text-primary); }
.md-toc__l2 { padding-left: 8px; }
.md-toc__l3 { padding-left: 18px; color: var(--astra-text-muted); font-size: 11.5px; }

/* ── 正文排版（GitHub / Codex / Notion 级精排） ─────────────────── */
.md-prose {
  min-width: 0;
  color: var(--astra-text-secondary);
  font-size: 14.5px;
  line-height: 1.78;
  word-break: break-word;
}
.md-view--compact .md-prose { font-size: 13.5px; line-height: 1.68; }

.md-prose :deep(h1),
.md-prose :deep(h2),
.md-prose :deep(h3),
.md-prose :deep(h4) {
  color: var(--astra-text-primary);
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: -0.01em;
  scroll-margin-top: 16px;
}
.md-prose :deep(h1) { margin: 22px 0 12px; font-size: 20px; padding-bottom: 8px; border-bottom: 1px solid var(--astra-border); }
.md-prose :deep(h2) { margin: 22px 0 10px; font-size: 17px; padding-bottom: 6px; border-bottom: 1px solid var(--astra-border-subtle); }
.md-prose :deep(h3) { margin: 16px 0 8px; font-size: 15px; }
.md-prose :deep(h4) { margin: 14px 0 6px; font-size: 14px; }
.md-view--compact .md-prose :deep(h1) { font-size: 17px; margin: 16px 0 8px; }
.md-view--compact .md-prose :deep(h2) { font-size: 15px; margin: 14px 0 6px; }
.md-view--compact .md-prose :deep(h3) { font-size: 14px; margin: 12px 0 5px; }

.md-prose :deep(p) { margin: 9px 0; }
.md-prose :deep(strong) { color: var(--astra-text-primary); font-weight: 600; }
.md-prose :deep(ul), .md-prose :deep(ol) { margin: 8px 0; padding-left: 22px; }
.md-prose :deep(li) { margin: 4px 0; }
.md-prose :deep(li:has(> input[type='checkbox'])) { list-style: none; margin-left: -18px; }
.md-prose :deep(li > p) { margin: 4px 0; }
.md-prose :deep(input[type='checkbox']) { margin-right: 6px; accent-color: var(--astra-primary); vertical-align: middle; }

.md-prose :deep(blockquote) {
  margin: 12px 0; padding: 10px 14px;
  border-left: 3px solid var(--astra-border-hover);
  border-radius: 0 8px 8px 0;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
}
.md-prose :deep(blockquote > p) { margin: 4px 0; }

.md-prose :deep(code) {
  padding: 1.5px 5px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-primary);
  font-family: var(--astra-font-mono);
  font-size: 0.87em;
}

.md-prose :deep(.md-code-block) {
  margin: 12px 0;
  border: 1px solid #27272a;
  border-radius: 10px;
  background: #18181b;
  overflow: hidden;
}
.md-prose :deep(.md-code-block__head) {
  display: flex; align-items: center; justify-content: space-between;
  padding: 6px 12px;
  border-bottom: 1px solid #27272a;
  background: #202024;
}
.md-prose :deep(.md-code-block__lang) {
  color: #a1a1aa;
  font-family: var(--astra-font-mono);
  font-size: 11px;
  text-transform: lowercase;
}
.md-prose :deep(.md-code-block__copy) {
  padding: 2px 8px;
  border: 1px solid #3f3f46;
  border-radius: 5px;
  background: transparent;
  color: #d4d4d8;
  font-size: 11px;
  cursor: pointer;
  transition: background-color 0.15s ease, color 0.15s ease;
}
.md-prose :deep(.md-code-block__copy:hover) { background: #2e2e33; color: #fafafa; }
.md-prose :deep(.md-code-block__copy.is-copied) { border-color: #22c55e; color: #86efac; }
.md-prose :deep(.md-code-block pre) {
  margin: 0; padding: 12px 14px; overflow-x: auto; background: transparent; border: 0;
}
.md-prose :deep(.md-code-block pre code) {
  padding: 0; border: 0; background: transparent;
  color: #f4f4f5; font-family: var(--astra-font-mono); font-size: 12.5px; line-height: 1.6;
}

.md-prose :deep(.md-table-wrap) {
  margin: 12px 0; overflow-x: auto;
  border: 1px solid var(--astra-border); border-radius: 8px;
}
.md-prose :deep(table) { width: 100%; border-collapse: collapse; font-size: 13px; }
.md-prose :deep(th), .md-prose :deep(td) {
  padding: 8px 12px; border-bottom: 1px solid var(--astra-border-subtle); text-align: left;
}
.md-prose :deep(th) {
  background: var(--astra-bg-subtle); color: var(--astra-text-primary); font-weight: 600;
  border-bottom-color: var(--astra-border);
}
.md-prose :deep(tbody tr:last-child td) { border-bottom: 0; }
.md-prose :deep(tbody tr:nth-child(even) td) { background: rgba(244, 244, 246, 0.45); }

.md-prose :deep(a) { color: var(--astra-accent-text); text-decoration: none; border-bottom: 1px solid rgba(37, 88, 181, 0.3); }
.md-prose :deep(a:hover) { color: var(--astra-accent-hover); border-bottom-color: var(--astra-accent-hover); }
.md-prose :deep(img) { max-width: 100%; height: auto; border-radius: 8px; border: 1px solid var(--astra-border); }
.md-prose :deep(hr) { margin: 20px 0; border: 0; border-top: 1px solid var(--astra-border); }
.md-prose :deep(> :first-child) { margin-top: 0 !important; }
.md-prose :deep(> :last-child) { margin-bottom: 0 !important; }

@media (min-width: 2240px) {
  .md-view--with-toc .md-view__layout { grid-template-columns: minmax(0, 1fr) 224px; gap: 28px; }
  .md-prose { font-size: 15px; line-height: 1.8; }
  .md-view--compact .md-prose { font-size: 14px; line-height: 1.7; }
  .md-prose :deep(table) { font-size: 13.5px; }
  .md-view__fm-chip { max-width: 360px; }
}

@media (max-width: 820px) {
  .md-view--with-toc .md-view__layout { grid-template-columns: minmax(0, 1fr); }
  .md-toc { display: none; }
}
</style>
