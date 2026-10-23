<template>
  <section ref="root" class="gateway-overview" data-testid="gateway-overview">
    <GatewayHeaderActions>
      <span class="gateway-overview__window-label">统计窗口</span>
      <ElSelect v-model="statsWindow" class="gateway-overview__window" aria-label="统计窗口" data-testid="gateway-stats-window">
        <ElOption value="hour" label="本小时" />
        <ElOption value="today" label="今日" />
        <ElOption value="7d" label="近 7 天" />
        <ElOption value="30d" label="近 30 天" />
      </ElSelect>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-overview-refresh" @click="load(false)">刷新</ElButton>
    </GatewayHeaderActions>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-overview-error">
      <ElButton type="primary" data-testid="gateway-overview-retry" @click="load(false)">重试</ElButton>
    </ElAlert>
    <ElAlert v-if="statsError" type="error" show-icon :closable="false" :title="statsError" data-testid="gateway-stats-error">
      <ElButton type="primary" @click="load(false)">重试</ElButton>
    </ElAlert>

    <section
      class="gateway-overview__status"
      :data-status="summary?.status ?? 'unknown'"
      data-testid="gateway-health-bar"
    >
      <span class="gateway-overview__status-pill" data-testid="gateway-health-status">{{ statusLabel }}</span>
      <div class="gateway-overview__status-copy">
        <p class="gateway-overview__impact" data-testid="gateway-health-impact">{{ impactText }}</p>
        <p v-if="reasonText" class="gateway-overview__reasons">{{ reasonText }}</p>
      </div>
      <ElButton type="primary" plain data-testid="gateway-show-abnormal" @click="showAbnormal">查看异常</ElButton>
    </section>

    <section class="gateway-overview__metrics" aria-label="核心指标">
      <ElCard
        v-for="metric in primaryCards"
        :key="metric.id"
        shadow="never"
        class="gateway-overview__metric"
        :class="{ 'gateway-overview__metric--empty': metric.empty }"
        :title="metric.hint || undefined"
        :data-testid="`gateway-metric-${metric.id}`"
      >
        <p class="gateway-overview__metric-label" :title="metric.label">{{ metric.label }}</p>
        <p class="gateway-overview__metric-value">{{ metric.value }}<small v-if="metric.unit && !metric.empty"> {{ metric.unit }}</small></p>
        <p class="gateway-overview__metric-window">{{ metric.window }}</p>
      </ElCard>
    </section>

    <div class="gateway-overview__split">
      <ElCard ref="nodesCard" shadow="never" class="gateway-overview__nodes-card" data-testid="gateway-overview-nodes">
        <template #header>
          <div class="gateway-overview__card-header">
            <div class="gateway-overview__card-title">
              <h4>节点与资源池</h4>
            </div>
            <ElCheckbox v-model="abnormalOnly" data-testid="gateway-abnormal-only">只看异常</ElCheckbox>
          </div>
        </template>
        <ElAlert v-if="nodesError" type="warning" show-icon :closable="false" :title="nodesError" />
        <ElTable v-if="!nodesError" :data="visibleRows" row-key="node_id" max-height="316" :empty-text="tableEmpty">
          <ElTableColumn label="节点" min-width="160">
            <template #default="{ row }">
              <RouterLink class="gateway-overview__node" :to="`/gateway/nodes/${row.node_id}`">{{ row.name }}</RouterLink>
              <span class="gateway-overview__sub">{{ row.pool_name ?? '未分配' }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="结论" min-width="96">
            <template #default="{ row }">{{ row.abnormal ? '需要处理' : '可以服务' }}</template>
          </ElTableColumn>
          <ElTableColumn label="健康" width="88" align="center">
            <template #default="{ row }"><ElTag :type="healthTag(row.health)">{{ row.health_label }}</ElTag></template>
          </ElTableColumn>
          <ElTableColumn label="调度" width="96" align="center">
            <template #default="{ row }"><ElTag :type="schedulingTag(row.scheduling)">{{ row.scheduling_label }}</ElTag></template>
          </ElTableColumn>
          <ElTableColumn label="当前并发" min-width="130">
            <template #default="{ row }">{{ loadText(row as GatewayNodeMetric) }}</template>
          </ElTableColumn>
          <ElTableColumn label="日志" width="64" align="center">
            <template #default="{ row }">
              <ElButton link type="primary" @click="openNodeLogs(row as GatewayNodeMetric)">查看</ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
      </ElCard>

      <ElCard shadow="never" class="gateway-overview__events" data-testid="gateway-overview-events">
        <template #header>
          <div class="gateway-overview__card-header">
            <div class="gateway-overview__card-title">
              <h4>最近事件</h4>
            </div>
            <span v-if="events.length" class="gateway-overview__event-count">{{ events.length }} 条</span>
          </div>
        </template>
        <ElAlert v-if="eventsError" type="warning" show-icon :closable="false" :title="eventsError" />
        <ul v-if="events.length" class="gateway-overview__event-list">
          <li v-for="event in events" :key="event.id">
            <button type="button" @click="openEventLogs(event)">
              <ElTag size="small" :type="event.source === 'config' ? 'info' : 'warning'">{{ event.source === 'config' ? '配置' : '运行' }}</ElTag>
              <span class="gateway-overview__event-summary" :title="event.summary">{{ event.summary }}</span>
              <time>{{ formatDateTime(event.created_at) }}</time>
            </button>
          </li>
        </ul>
        <ElEmpty v-else-if="!eventsError" :image-size="52" description="暂无事件" />
      </ElCard>
    </div>

    <ElCard shadow="never" class="gateway-overview__trend" data-testid="gateway-overview-trend">
      <template #header>
        <div class="gateway-overview__card-header">
          <div class="gateway-overview__card-title">
            <h4>调用趋势</h4>
            <p class="gateway-overview__card-sub">{{ trendHeadline }}</p>
          </div>
          <div class="gateway-overview__trend-tools">
            <ElSelect v-model="groupBy" class="gateway-overview__group" aria-label="趋势分组">
              <ElOption value="none" label="全部" />
              <ElOption value="model" label="按模型" />
              <ElOption value="node" label="按节点" />
            </ElSelect>
            <ElSelect v-if="seriesOptions.length > 1" v-model="seriesKey" class="gateway-overview__group" aria-label="趋势序列">
              <ElOption v-for="item in seriesOptions" :key="item.key" :value="item.key" :label="item.label" />
            </ElSelect>
          </div>
        </div>
      </template>
      <ElAlert
        v-if="trendError"
        type="warning"
        show-icon
        :closable="false"
        :title="trendError"
        data-testid="gateway-trend-error"
      />
      <GatewayTrendChart v-if="trendPoints.length" mode="calls" :call-points="trendPoints" tall />
      <ElEmpty v-else-if="!trendError" :image-size="52" description="暂无数据" />
    </ElCard>
    <details class="gateway-overview__details" data-testid="gateway-overview-details">
      <summary>辅助指标</summary>
      <div class="gateway-overview__detail-grid">
        <div v-for="item in detailItems" :key="item.id" class="gateway-overview__detail">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
          <small>{{ item.hint }}</small>
        </div>
      </div>
    </details>
  </section>
</template>

<script setup lang="ts">
import { RefreshCw } from 'lucide-vue-next'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElOption,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import {
  gatewayApi,
  GatewayApiError,
  type GatewayHealthSummary,
  type GatewayNodeMetric,
  type GatewayOpsEvent,
  type GatewayScopeStats,
  type GatewayStatsWindow,
  type GatewayTrend,
} from '@/api/gateway'
import { errorMessage, formatDateTime } from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import GatewayTrendChart from '@/views/gateway/GatewayTrendChart.vue'

const router = useRouter()
const root = ref<HTMLElement | null>(null)
const nodesCard = ref<{ $el?: HTMLElement } | null>(null)
const summary = ref<GatewayHealthSummary | null>(null)
const stats = ref<GatewayScopeStats | null>(null)
const rows = ref<GatewayNodeMetric[]>([])
const events = ref<GatewayOpsEvent[]>([])
const trend = ref<GatewayTrend | null>(null)
const error = ref('')
const healthError = ref('')
const healthPermission = ref(false)
const statsError = ref('')
const statsPermission = ref(false)
const statsLoading = ref(false)
const nodesError = ref('')
const eventsError = ref('')
const trendError = ref('')
const loading = ref(false)
const abnormalOnly = ref(false)
const statsWindow = ref<GatewayStatsWindow>('today')
const groupBy = ref<'none' | 'model' | 'node'>('none')
const seriesKey = ref('all')
let timer = 0

const WINDOW_LABEL: Record<GatewayStatsWindow, string> = {
  hour: '本小时',
  today: '今日',
  '7d': '近 7 天',
  '30d': '近 30 天',
}

const statusLabel = computed(() => {
  const status = summary.value?.status
  if (status === 'normal') return '正常'
  if (status === 'degraded') return '降级'
  if (status === 'down') return '不可用'
  return '未知'
})
const impactText = computed(() => summary.value?.impact_summary || '暂无影响范围')
const reasonText = computed(() => (summary.value?.reasons ?? []).join('；'))
const healthyRows = computed(() => rows.value.filter((row) => !row.abnormal))
const abnormalRows = computed(() => rows.value.filter((row) => row.abnormal))
const visibleRows = computed(() => abnormalOnly.value
  ? abnormalRows.value
  : [...abnormalRows.value, ...healthyRows.value])
const tableEmpty = computed(() => {
  if (!rows.value.length) return '暂无节点'
  if (abnormalOnly.value) return '没有异常节点'
  return '暂无节点'
})
const seriesOptions = computed(() => trend.value?.series ?? [])
const activeSeries = computed(() => seriesOptions.value.find((item) => item.key === seriesKey.value) ?? seriesOptions.value[0])
const trendPoints = computed(() => {
  const points = activeSeries.value?.points ?? []
  return points.some((point) => point.requests > 0 || point.errors > 0) ? points : []
})
const trendHeadline = computed(() => {
  const points = trendPoints.value
  if (!points.length) return '暂无数据'
  const requests = points.reduce((sum, point) => sum + point.requests, 0)
  const errors = points.reduce((sum, point) => sum + point.errors, 0)
  return `窗口内 ${numberText(requests)} 次请求 · 失败 ${numberText(errors)} 次`
})

const primaryCards = computed(() => {
  const current = stats.value
  const windowLabel = WINDOW_LABEL[statsWindow.value]
  const statsFailure = statsPermission.value ? '无权限' : statsError.value ? '加载失败' : ''
  const statsPending = !current && statsLoading.value
  const unavailable = (value: string) => statsFailure || (statsPending ? '加载中' : value)
  const terminal = current ? current.success + current.errors : 0
  const failureRate = current && terminal > 0 ? current.errors / terminal : null
  const p95 = current?.ttft_ms.p95
  return [
    {
      id: 'requests',
      label: '请求数',
      value: unavailable(current ? numberText(current.requests) : '暂无数据'),
      unit: statsFailure || current == null ? '' : '条',
      window: windowLabel,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : '',
      empty: Boolean(statsFailure) || current == null,
    },
    {
      id: 'failure',
      label: '请求失败率',
      value: unavailable(failureRate == null ? '无终态样本' : percentText(failureRate)),
      unit: '',
      window: windowLabel,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : terminal
            ? `失败 ${numberText(current?.errors ?? 0)} / 终态 ${numberText(terminal)}`
            : `${windowLabel}内没有已确认结束的请求`,
      empty: Boolean(statsFailure) || failureRate == null,
    },
    {
      id: 'qps',
      label: '平均 QPS',
      value: unavailable(current ? current.qps.toFixed(2) : '暂无数据'),
      unit: statsFailure || current == null ? '' : 'req/s',
      window: `${windowLabel}平均`,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : '',
      empty: Boolean(statsFailure) || current == null,
    },
    {
      id: 'ttft',
      label: '首 Token 延迟 P95（TTFT）',
      value: unavailable(p95 == null ? '无采样' : `${Math.round(p95)} ms`),
      unit: '',
      window: `${windowLabel} P95`,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : p95 == null ? '无采样' : '',
      empty: Boolean(statsFailure) || p95 == null,
    },
    {
      id: 'output-tps',
      label: '输出速度',
      value: unavailable(current?.output_tps.avg == null ? '无采样' : current.output_tps.avg.toFixed(1)),
      unit: statsFailure || current?.output_tps.avg == null ? '' : 'Token/s',
      window: `${windowLabel}平均`,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : '',
      empty: Boolean(statsFailure) || current?.output_tps.avg == null,
    },
    {
      id: 'active',
      label: '当前并发',
      value: unavailable(current ? numberText(current.active) : '暂无数据'),
      unit: '',
      window: '实时',
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : '',
      empty: Boolean(statsFailure) || current == null,
    },
  ]
})

const detailItems = computed(() => {
  const health = summary.value?.metrics
  const current = stats.value
  const windowLabel = WINDOW_LABEL[statsWindow.value]
  const healthUnavailable = healthPermission.value ? '无权限' : healthError.value ? '加载失败' : ''
  const statsUnavailable = statsPermission.value ? '无权限' : statsError.value ? '加载失败' : ''
  return [
    { id: 'retry', label: '重试率', value: healthUnavailable || percentOrEmpty(health?.retry_rate), hint: '近 5 分钟' },
    { id: '429', label: '上游限流（429）', value: healthUnavailable || percentOrEmpty(health?.upstream_429_rate), hint: '近 5 分钟' },
    { id: 'stream', label: '流式中断率', value: healthUnavailable || percentOrEmpty(health?.stream_interrupt_rate), hint: '近 5 分钟' },
    { id: 'tokens', label: 'Token 吞吐量', value: healthUnavailable || throughputOrEmpty(health?.token_throughput), hint: '近 5 分钟' },
    { id: 'unknown', label: 'Token 用量缺失率', value: healthUnavailable || percentOrEmpty(health?.unknown_usage_share), hint: '近 5 分钟' },
    { id: 'connect', label: '连接耗时（平均）', value: statsUnavailable || msOrEmpty(current?.connect_ms.avg), hint: `${windowLabel}平均` },
    { id: 'ttfb', label: 'TTFB（平均）', value: statsUnavailable || msOrEmpty(current?.ttfb_ms.avg), hint: `${windowLabel}平均` },
    { id: 'capacity', label: '并发容量', value: healthUnavailable || capacityText(health), hint: '实时' },
    { id: 'peak', label: '5 分钟峰值并发', value: healthUnavailable || (health?.peak_concurrency == null ? '暂无数据' : numberText(health.peak_concurrency)), hint: '近 5 分钟' },
    { id: 'queue', label: '排队', value: healthUnavailable || (health?.queue == null ? '无队列' : numberText(health.queue)), hint: '实时' },
  ]
})

function capacityText(health: GatewayHealthSummary['metrics'] | undefined): string {
  if (!health || health.active == null) return '采集失败'
  return `${numberText(health.active)} / ${health.capacity_limit == null ? '不限' : numberText(health.capacity_limit)}`
}

function numberText(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value)
}
function percentText(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}
function percentOrEmpty(value: number | null | undefined): string {
  return value == null ? '暂无数据' : percentText(value)
}
function msOrEmpty(value: number | null | undefined): string {
  return value == null ? '暂无数据' : `${Math.round(value)} ms`
}
function throughputOrEmpty(value: number | null | undefined): string {
  return value == null ? '暂无数据' : `${value.toFixed(2)} token/s`
}
function loadText(row: GatewayNodeMetric): string {
  if (row.active === null) return '采集失败'
  const share = row.occupancy === null ? '不限' : percentText(row.occupancy)
  const queue = row.queue === null ? '无队列' : `排队 ${row.queue}`
  return `${share} · ${row.active}${row.capacity_limit ? `/${row.capacity_limit}` : ''} · ${queue}`
}
function healthTag(value: string): 'success' | 'danger' | 'info' {
  if (value === 'healthy') return 'success'
  if (value === 'unhealthy') return 'danger'
  return 'info'
}
function schedulingTag(value: string): 'success' | 'danger' | 'warning' | 'info' {
  if (value === 'available') return 'success'
  if (value === 'circuit') return 'danger'
  if (value === 'limited' || value === 'half_open') return 'warning'
  return 'info'
}
async function showAbnormal() {
  abnormalOnly.value = true
  await nextTick()
  nodesCard.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
function openNodeLogs(row: GatewayNodeMetric) {
  const end = new Date()
  const start = new Date(end.getTime() - 5 * 60 * 1000)
  void router.push({
    path: '/gateway/logs',
    query: { node_id: row.node_id, from: start.toISOString(), to: end.toISOString() },
  })
}
function openEventLogs(event: GatewayOpsEvent) {
  const at = new Date(event.created_at).getTime()
  const start = new Date(at - 5 * 60 * 1000)
  const end = new Date(at + 5 * 60 * 1000)
  void router.push({
    path: '/gateway/logs',
    query: {
      node_id: event.node_id ?? undefined,
      model: event.model ?? undefined,
      pool_id: event.pool_id ?? undefined,
      key_id: event.key_id ?? undefined,
      from: start.toISOString(),
      to: end.toISOString(),
    },
  })
}

function emptyMetrics(): GatewayHealthSummary['metrics'] {
  return {
    qps: null,
    qps_requests: 0,
    error_rate: null,
    error_samples: 0,
    error_failures: 0,
    error_rate_applies: false,
    retry_rate: null,
    upstream_429_rate: null,
    stream_interrupt_rate: null,
    ttft_p95_ms: null,
    output_tps_p95: null,
    connect_p95_ms: null,
    ttfb_p95_ms: null,
    duration_p95_ms: null,
    unknown_usage_share: null,
    token_throughput: null,
    known_total_tokens: null,
    active: null,
    capacity_limit: null,
    capacity_occupancy: null,
    peak_concurrency: null,
    queue: null,
  }
}

function unavailableHealth(reason: string): GatewayHealthSummary {
  const now = new Date().toISOString()
  return {
    status: 'unknown',
    stale: true,
    reasons: [reason],
    impact: [],
    impact_summary: '健康摘要不可用',
    window: { qps_seconds: 60, error_seconds: 300, start: now, end: now },
    generated_at: now,
    metrics: emptyMetrics(),
    ignored_pools: [],
  }
}

function isAbort(reason: unknown): boolean {
  return reason instanceof Error && reason.name === 'AbortError'
}

function permissionDenied(reason: unknown): boolean {
  return reason instanceof GatewayApiError && (reason.status === 401 || reason.status === 403)
}

let loadSeq = 0
let activeLoad: AbortController | null = null

async function load(silent: boolean) {
  const seq = ++loadSeq
  activeLoad?.abort()
  const controller = new AbortController()
  activeLoad = controller
  if (!silent) loading.value = true
  statsLoading.value = true
  const signal = controller.signal
  const stale = () => seq !== loadSeq || signal.aborted
  try {
    try {
      const health = await gatewayApi.healthSummary(signal)
      if (stale()) return
      summary.value = health
      healthError.value = ''
      healthPermission.value = false
      error.value = ''
    } catch (reason) {
      if (stale() || isAbort(reason)) return
      summary.value = unavailableHealth('健康摘要加载失败')
      healthPermission.value = permissionDenied(reason)
      healthError.value = errorMessage(reason, '健康摘要加载失败')
      error.value = healthError.value
    }
    if (stale()) return
    statsLoading.value = true
    const [nodeRows, eventRows, trendValue, statsValue] = await Promise.allSettled([
      gatewayApi.nodeMetrics(signal),
      gatewayApi.opsEvents(40, signal),
      gatewayApi.trends(statsWindow.value, groupBy.value, signal),
      gatewayApi.stats('overview', statsWindow.value),
    ])
    if (stale()) return
    if (nodeRows.status === 'fulfilled') {
      rows.value = [...nodeRows.value].sort((left, right) => Number(right.abnormal) - Number(left.abnormal))
      nodesError.value = ''
    } else if (!isAbort(nodeRows.reason)) {
      nodesError.value = errorMessage(nodeRows.reason, '节点指标加载失败')
    }
    if (eventRows.status === 'fulfilled') {
      events.value = [...eventRows.value].sort((left, right) => right.created_at.localeCompare(left.created_at))
      eventsError.value = ''
    } else if (!isAbort(eventRows.reason)) {
      eventsError.value = errorMessage(eventRows.reason, '事件加载失败')
    }
    if (trendValue.status === 'fulfilled') {
      trend.value = trendValue.value
      trendError.value = ''
      if (!trendValue.value.series.some((item) => item.key === seriesKey.value)) seriesKey.value = 'all'
    } else if (!isAbort(trendValue.reason)) {
      trendError.value = errorMessage(trendValue.reason, '趋势加载失败')
    }
    if (statsValue.status === 'fulfilled') {
      stats.value = statsValue.value
      statsError.value = ''
      statsPermission.value = false
    } else {
      stats.value = null
      statsPermission.value = permissionDenied(statsValue.reason)
      statsError.value = errorMessage(statsValue.reason, '统计指标加载失败')
    }
    statsLoading.value = false
  } finally {
    if (seq === loadSeq) {
      loading.value = false
      statsLoading.value = false
    }
  }
}

watch(statsWindow, () => {
  stats.value = null
  statsError.value = ''
  statsPermission.value = false
  statsLoading.value = true
  void load(false)
})
watch(groupBy, () => { void load(true) })
onMounted(() => {
  void load(false)
  timer = window.setInterval(() => { void load(true) }, 30_000)
})
onBeforeUnmount(() => {
  window.clearInterval(timer)
  loadSeq += 1
  activeLoad?.abort()
})
</script>

<style scoped>
/* 运行总览：状态 → 核心指标 → 趋势与事件 → 节点表 → 辅助指标。
   卡片之间统一 16px 间距，卡片标题区统一「标题 + 一句范围说明」两行结构。 */
.gateway-overview { display: flex; flex-direction: column; gap: 16px; padding-bottom: 8px; }
.gateway-overview__window { width: 112px; }
.gateway-overview__window-label { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }

/* ── 服务状态 ─────────────────────────────────────────────────────── */
.gateway-overview__status {
  display: flex; align-items: center; gap: 12px;
  padding: 12px 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}
.gateway-overview__status[data-status='normal'] { border-color: #cfe9dd; background: #f3fbf6; }
.gateway-overview__status[data-status='degraded'] { border-color: #e8cd8f; background: #fff9ec; }
.gateway-overview__status[data-status='down'] { border-color: #e7a8ae; background: #fff5f6; }
.gateway-overview__status[data-status='unknown'] { border-color: #d5e0da; background: #f6f8f7; }
.gateway-overview__status-pill {
  flex: none;
  padding: 3px 10px;
  border-radius: 999px;
  background: #e3f4ec; color: #187956;
  font-size: 12px; font-weight: var(--astra-weight-bold);
}
.gateway-overview__status[data-status='degraded'] .gateway-overview__status-pill { background: #fbeac4; color: #8a5a08; }
.gateway-overview__status[data-status='down'] .gateway-overview__status-pill { background: #f8dadd; color: #9f2f3a; }
.gateway-overview__status[data-status='unknown'] .gateway-overview__status-pill { background: #e7eeeb; color: #5c736a; }
.gateway-overview__status-copy { display: grid; flex: 1 1 auto; gap: 2px; min-width: 0; }
.gateway-overview__impact { margin: 0; color: var(--astra-text-primary); font-weight: 600; }
.gateway-overview__reasons { margin: 0; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }

/* ── 核心指标 ─────────────────────────────────────────────────────── */
.gateway-overview__metrics {
  display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
  border: 1px solid var(--astra-border); border-radius: 12px;
  background: var(--astra-bg-panel); overflow: hidden;
}
.gateway-overview__metric { border-radius: 0; background: transparent; }
.gateway-overview__metric:not(:first-child) { border-left: 1px solid var(--astra-border); }
.gateway-overview__metric :deep(.el-card__body) { display: grid; gap: 6px; padding: 14px 16px; }
.gateway-overview__metric-label {
  margin: 0; color: var(--astra-text-secondary); font-size: 12px; line-height: 16px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.gateway-overview__metric-value {
  margin: 0; color: var(--astra-text-primary);
  font-size: 26px; line-height: 32px; font-weight: var(--astra-weight-bold);
  font-variant-numeric: tabular-nums;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.gateway-overview__metric-value small { color: var(--astra-text-secondary); font-size: 12px; font-weight: 400; }
.gateway-overview__metric-window { margin: 0; color: var(--astra-text-muted); font-size: 11px; }
.gateway-overview__metric--empty .gateway-overview__metric-value { color: var(--astra-text-muted); font-size: 14px; }

/* ── 趋势与事件 ───────────────────────────────────────────────────── */
.gateway-overview__split { display: grid; grid-template-columns: minmax(0, 1.7fr) minmax(320px, 1fr); gap: 16px; align-items: stretch; }
/* 网关壳会去掉内层卡片的边框与左右内边距；总览需要靠卡片分组，这里恢复边框与内边距。 */
.gateway-overview__split :deep(.el-card),
.gateway-overview .gateway-overview__trend {
  border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel);
}
.gateway-overview :deep(.el-card) > .el-card__header { padding-left: 16px; padding-right: 16px; }
.gateway-overview :deep(.el-card) > .el-card__body { padding-left: 16px; padding-right: 16px; }
.gateway-overview__card-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.gateway-overview__card-title { min-width: 0; }
.gateway-overview__card-title h4 { margin: 0; color: var(--astra-text-primary); font-size: 14px; line-height: 20px; }
.gateway-overview__card-sub { margin: 2px 0 0; color: var(--astra-text-muted); font-size: 12px; line-height: 18px; }
.gateway-overview__trend-tools { display: flex; flex: none; flex-wrap: wrap; gap: 8px; }
.gateway-overview__group { width: 120px; }
.gateway-overview__trend :deep(.el-card__body) { padding-top: 12px; }
.gateway-overview__trend :deep(.el-alert) { margin-bottom: 8px; }

.gateway-overview__events :deep(.el-card__body) { padding-top: 4px; }
.gateway-overview__event-count { flex: none; color: var(--astra-text-muted); font-size: 12px; }
.gateway-overview__event-list {
  display: grid; gap: 0; margin: 0; padding: 0; list-style: none;
  max-height: 316px; overflow-y: auto;
}
.gateway-overview__event-list li { border-bottom: 1px solid var(--astra-border); }
.gateway-overview__event-list li:last-child { border-bottom: none; }
.gateway-overview__event-list button {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px;
  width: 100%; padding: 10px 8px;
  border: 0; border-radius: 6px; background: transparent;
  color: var(--astra-text-primary); text-align: left; cursor: pointer;
}
.gateway-overview__event-list button:hover { background: var(--astra-bg-elevated); }
.gateway-overview__event-summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.gateway-overview__event-list time { color: var(--astra-text-muted); font-size: 11px; white-space: nowrap; }

/* ── 节点与资源池 ─────────────────────────────────────────────────── */
.gateway-overview__nodes-card { scroll-margin-top: 8px; }
.gateway-overview__nodes-card :deep(.el-card__body) { padding-top: 4px; }
.gateway-overview__node { color: var(--astra-primary-bright); font-weight: var(--astra-weight-bold); text-decoration: none; }
.gateway-overview__sub { display: block; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }

/* ── 辅助指标 ─────────────────────────────────────────────────────── */
.gateway-overview__details { border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); overflow: hidden; }
.gateway-overview__details summary { display: flex; align-items: center; gap: 8px; padding: 12px 16px; cursor: pointer; color: var(--astra-text-primary); font-size: 14px; font-weight: var(--astra-weight-bold); list-style: none; }
.gateway-overview__details summary::-webkit-details-marker { display: none; }
.gateway-overview__details summary::before { content: '▸'; color: var(--astra-text-muted); }
.gateway-overview__details[open] summary::before { transform: rotate(90deg); }
.gateway-overview__detail-grid { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; padding: 0 16px 16px; }
.gateway-overview__detail { display: grid; gap: 3px; padding: 10px 12px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-elevated); }
.gateway-overview__detail span { color: var(--astra-text-muted); font-size: 11px; }
.gateway-overview__detail strong { color: var(--astra-text-primary); font-size: 14px; font-variant-numeric: tabular-nums; }
.gateway-overview__detail small { color: var(--astra-text-muted); font-size: 10px; }

@media (max-width: 1400px) {
  .gateway-overview__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
  .gateway-overview__metric { border-left: 0; border-top: 0; }
  .gateway-overview__metric:not(:nth-child(3n + 1)) { border-left: 1px solid var(--astra-border); }
  .gateway-overview__metric:nth-child(n + 4) { border-top: 1px solid var(--astra-border); }
}
@media (max-width: 1100px) {
  .gateway-overview__split { grid-template-columns: 1fr; }
  .gateway-overview__detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .gateway-overview__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .gateway-overview__metric { border-left: 0; border-top: 0; }
  .gateway-overview__metric:not(:nth-child(2n + 1)) { border-left: 1px solid var(--astra-border); }
  .gateway-overview__metric:nth-child(n + 3) { border-top: 1px solid var(--astra-border); }
  .gateway-overview__status { align-items: flex-start; flex-wrap: wrap; }
  .gateway-overview__detail-grid { grid-template-columns: 1fr; }
}
</style>
