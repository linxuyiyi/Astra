<template>
  <section class="usage-summary">
    <div class="usage-summary__entry">
      <div>
        <h3>知识使用分析</h3>
        <p>{{ entryLine }}</p>
      </div>
      <button type="button" class="usage-summary__more" @click="detailOpen = true">查看详情</button>
    </div>

    <AstraDialog
      :open="detailOpen"
      title="知识使用分析"
      :subtitle="`最近 ${days} 天`"
      dismissible
      testid="knowledge-usage-dialog"
      width="min(760px,100%)"
      @close="detailOpen = false"
    >
      <div v-if="hasUsage" class="usage-summary__body">
        <div class="usage-kpis">
          <article><span><Quote :size="16" /></span><small>总引用次数</small><strong>{{ totalReferences }}</strong></article>
          <article><span><BookOpen :size="16" /></span><small>使用知识数</small><strong>{{ usedResources }}</strong></article>
          <article><span><LibraryBig :size="16" /></span><small>反馈次数</small><strong class="text-value">{{ feedbackCount }}</strong></article>
        </div>
        <div class="top-knowledge">
          <h4>最常引用知识 TOP3</h4>
          <ol v-if="summary?.top_knowledge.length"><li v-for="item in summary.top_knowledge" :key="item.resource_id"><span>{{ item.title }}</span><strong>{{ item.reference_count }} 次</strong></li></ol>
          <p v-else>暂无知识引用记录</p>
        </div>
        <div class="usage-trend" aria-label="近七日知识引用趋势">
          <h4>引用趋势</h4>
          <div><span v-for="point in summary?.trend ?? []" :key="point.date" :title="`${point.date}：${point.reference_count} 次`"><i :style="{ height: `${barHeight(point.reference_count)}%` }" /><small>{{ point.date.slice(5) }}</small></span></div>
        </div>
      </div>
      <p v-else class="usage-summary__empty">最近 {{ days }} 天暂无知识引用</p>
    </AstraDialog>
  </section>
</template>

<script setup lang="ts">
import { BookOpen, LibraryBig, Quote } from 'lucide-vue-next'
import { computed, ref } from 'vue'
import type { LibraryUsageSummary } from '@/api/types'
import AstraDialog from '@/components/astra/AstraDialog.vue'

const props = defineProps<{ summary: LibraryUsageSummary | null }>()
const detailOpen = ref(false)

const days = computed(() => props.summary?.days ?? 7)
const totalReferences = computed(() => props.summary?.total_references ?? 0)
const usedResources = computed(() => props.summary?.used_resources ?? 0)
const feedbackCount = computed(() => props.summary?.feedback_count ?? 0)
const hasUsage = computed(() => totalReferences.value > 0 || usedResources.value > 0)
const entryLine = computed(() => (hasUsage.value
  ? `最近 ${days.value} 天 · ${totalReferences.value} 次引用 · ${usedResources.value} 条知识被使用`
  : `最近 ${days.value} 天暂无引用记录`))
const maximum = computed(() => Math.max(1, ...(props.summary?.trend.map((point) => point.reference_count) ?? [0])))
const barHeight = (value: number) => value ? Math.max(12, Math.round(value / maximum.value * 100)) : 4
</script>

<style scoped>
.usage-summary { flex: none; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-panel); }
.usage-summary__entry { display: flex; align-items: center; justify-content: space-between; gap: 18px; min-height: 40px; padding: 4px 12px; }
.usage-summary h3,.usage-summary p,.usage-summary h4 { margin: 0; }.usage-summary__entry h3 { color: var(--astra-text-primary); font-size: var(--astra-font-label); }.usage-summary__entry p { margin-left: 10px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
/* 助手入口已收进顶部栏，这里不再需要为悬浮球让位的右边距。 */
.usage-summary__more { flex: none; min-height: var(--astra-control-height); padding: 0 12px; color: var(--astra-primary-bright); border: 1px solid var(--astra-primary-border); border-radius: 8px; background: var(--astra-primary-soft); font-size: var(--astra-font-helper); cursor: pointer; }
.usage-summary__more:hover { background: var(--astra-primary-glow); }
.usage-summary__empty { color: var(--astra-text-muted); font-size: var(--astra-font-body); line-height: 1.6; }
.usage-summary__body { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.usage-kpis { display: grid; grid-column: 1 / -1; grid-template-columns: repeat(3,1fr); gap: 8px; }.usage-kpis article { display: grid; align-content: center; min-width: 0; padding: 12px; border-radius: 9px; background: #f5f9f7; }.usage-kpis span { display: grid; place-items: center; width: 29px; height: 29px; color: var(--astra-primary-bright); border-radius: 8px; background: var(--astra-primary-soft); }.usage-kpis small { margin-top: 6px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }.usage-kpis strong { margin-top: 2px; color: var(--astra-text-primary); font: var(--astra-weight-bold) var(--astra-font-kpi) var(--astra-font-display); }.usage-kpis .text-value { overflow: hidden; font-size: var(--astra-font-emphasis); line-height: 1.35; text-overflow: ellipsis; white-space: nowrap; }
.top-knowledge h4,.usage-trend h4 { color: var(--astra-text-secondary); font-size: var(--astra-font-label); }.top-knowledge ol { display: grid; gap: 5px; margin: 10px 0 0; padding: 0; list-style: none; counter-reset: item; }.top-knowledge li { display: grid; grid-template-columns: 24px minmax(0,1fr) auto; align-items: center; gap: 7px; min-height: 29px; color: var(--astra-text-secondary); font-size: var(--astra-font-body); counter-increment: item; }.top-knowledge li::before { display: grid; place-items: center; width: 22px; height: 22px; color: var(--astra-primary-bright); border-radius: 6px; background: var(--astra-primary-soft); content: counter(item); }.top-knowledge li span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }.top-knowledge li strong { color: var(--astra-text-muted); font-weight: var(--astra-weight-bold); }.top-knowledge p { margin-top: 18px; color: var(--astra-text-muted); font-size: var(--astra-font-body); }
.usage-trend > div { display: flex; align-items: end; gap: 7px; height: 99px; margin-top: 8px; }.usage-trend span { display: grid; grid-template-rows: 68px 18px; align-items: end; flex: 1; height: 91px; text-align: center; }.usage-trend i { display: block; min-height: 3px; border-radius: 4px 4px 2px 2px; background: linear-gradient(180deg,#24c47d,#0d9e61); }.usage-trend small { color: var(--astra-text-muted); font-size: var(--astra-font-micro); }
@media(max-width:700px){.usage-summary__entry{flex-wrap:wrap}.usage-summary__body{grid-template-columns:1fr}.usage-kpis{grid-column:auto}}
</style>