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
      class="gateway-overview__verdict"
      :data-status="summary?.status ?? 'unknown'"
      data-testid="gateway-health-bar"
    >
      <div>
        <div class="gateway-overview__verdict-line">
          <span class="gateway-overview__status" data-testid="gateway-health-status">{{ statusLabel }}</span>
          <span class="gateway-overview__impact" data-testid="gateway-health-impact">{{ impactText }}</span>
        </div>
        <p v-if="reasonText" class="gateway-overview__reasons">{{ reasonText }}</p>
      </div>
      <ElButton type="primary" plain data-testid="gateway-show-abnormal" @click="showAbnormal">查看异常</ElButton>
    </section>

    <p class="gateway-overview__scope-line">
      统计窗口仅作用于请求数、TTFT 平均、输出速度平均；失败率近 5 分钟，QPS 近 1 分钟，并发实时。
    </p>

    <section class="gateway-overview__metrics" aria-label="核心指标">
      <ElCard
        v-for="metric in primaryCards"
        :key="metric.id"
        shadow="never"
        :class="{ 'gateway-overview__metric--empty': metric.empty }"
        :title="metric.hint"
        :data-testid="`gateway-metric-${metric.id}`"
      >
        <div class="gateway-overview__metric-head">
          <p class="gateway-overview__metric-label">{{ metric.label }}</p>
          <span class="gateway-overview__metric-window">{{ metric.window }}</span>
        </div>
        <p class="gateway-overview__metric-value">{{ metric.value }}<small v-if="metric.unit && !metric.empty"> {{ metric.unit }}</small></p>
      </ElCard>
    </section>

    <ElCard shadow="never" data-testid="gateway-overview-trend">
      <template #header>
        <div class="gateway-overview__card-header">
          <div>
            <h4>调用趋势</h4>
            <ElText>请求数与失败数</ElText>
          </div>
          <div class="gateway-overview__trend-tools">
            <ElRadioGroup v-model="trendWindow" aria-label="趋势时间窗口" data-testid="gateway-window-switch">
              <ElRadioButton value="1h">近 1 小时</ElRadioButton>
              <ElRadioButton value="15m">近 15 分钟</ElRadioButton>
            </ElRadioGroup>
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
      <p v-if="trendStale" class="gateway-overview__trend-stale" data-testid="gateway-trend-stale">{{ trendStale }}</p>
      <p class="gateway-overview__trend-headline">{{ trendHeadline }}</p>
      <GatewayTrendChart v-if="trendPoints.length" mode="calls" :call-points="trendPoints" compact />
      <ElEmpty v-else-if="!trendError" :image-size="52" description="暂无数据" />
    </ElCard>

    <div class="gateway-overview__main">
      <ElCard shadow="never" data-testid="gateway-overview-nodes">
        <template #header>
          <div class="gateway-overview__card-header">
            <div><h4>节点与资源池</h4></div>
            <ElCheckbox v-model="abnormalOnly" data-testid="gateway-abnormal-only">只看异常</ElCheckbox>
          </div>
        </template>
        <ElAlert v-if="nodesError" type="warning" show-icon :closable="false" :title="nodesError" />
        <ElTable v-if="!nodesError" :data="visibleRows" row-key="node_id" :empty-text="tableEmpty">
          <ElTableColumn label="节点" min-width="140">
            <template #default="{ row }">
              <RouterLink class="gateway-overview__node" :to="`/gateway/nodes/${row.node_id}`">{{ row.name }}</RouterLink>
              <span class="gateway-overview__sub">{{ row.pool_name ?? '未分配' }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="结论" min-width="130">
            <template #default="{ row }">{{ row.abnormal ? '需要处理' : '可以服务' }}</template>
          </ElTableColumn>
          <ElTableColumn label="健康" width="88">
            <template #default="{ row }"><ElTag :type="healthTag(row.health)">{{ row.health_label }}</ElTag></template>
          </ElTableColumn>
          <ElTableColumn label="调度" width="100">
            <template #default="{ row }"><ElTag :type="schedulingTag(row.scheduling)">{{ row.scheduling_label }}</ElTag></template>
          </ElTableColumn>
          <ElTableColumn label="当前并发" min-width="150">
            <template #default="{ row }">{{ loadText(row as GatewayNodeMetric) }}</template>
          </ElTableColumn>
          <ElTableColumn label="日志" width="72">
            <template #default="{ row }">
              <ElButton link type="primary" @click="openNodeLogs(row as GatewayNodeMetric)">查看</ElButton>
            </template>
          </ElTableColumn>
        </ElTable>
      </ElCard>

      <ElCard shadow="never" class="gateway-overview__events" data-testid="gateway-overview-events">
        <template #header>
          <div class="gateway-overview__card-header">
            <div><h4>最近事件</h4></div>
          </div>
        </template>
        <ElAlert v-if="eventsError" type="warning" show-icon :closable="false" :title="eventsError" />
        <ul v-if="events.length" class="gateway-overview__event-list">
          <li v-for="event in events" :key="event.id">
            <button type="button" @click="openEventLogs(event)">
              <ElTag size="small" :type="event.source === 'config' ? 'info' : 'warning'">{{ event.source === 'config' ? '配置' : '运行' }}</ElTag>
              <span>{{ event.summary }}</span>
              <time>{{ formatDateTime(event.created_at) }}</time>
            </button>
          </li>
        </ul>
        <ElEmpty v-else-if="!eventsError" :image-size="52" description="暂无事件" />
      </ElCard>
    </div>

    <details class="gateway-overview__details" data-testid="gateway-overview-details">
      <summary>辅助指标 <span>近 5 分钟 · 连接与 TTFB 为所选统计窗口平均</span></summary>
      <div class="gateway-overview__detail-grid">
        <div v-for="item in detailItems" :key="item.id" class="gateway-overview__detail">
          <span>{{ item.label }}</span>
          <strong>{{ item.value }}</strong>
          <small>{{ item.hint }}</small>
        </div>
      </div>
      <p class="gateway-overview__limit">延迟均值趋势：当前趋势接口不提供，本版本不展示。</p>
    </details>
  </section>
</template>

<script setup lang="ts">
import { RefreshCw } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCheckbox,
  ElEmpty,
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
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
const trendUpdatedAt = ref<string | null>(null)
const loading = ref(false)
const abnormalOnly = ref(false)
const statsWindow = ref<GatewayStatsWindow>('today')
const trendWindow = ref<'1h' | '15m'>('1h')
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
const trendStale = computed(() => {
  if (!trend.value || !trendUpdatedAt.value) return ''
  const windowMismatch = trend.value.window !== trendWindow.value
  const groupMismatch = trend.value.group_by !== groupBy.value
  if (!windowMismatch && !groupMismatch && !trendError.value) return ''
  const groupLabel = trend.value.group_by === 'none' ? '全部' : trend.value.group_by === 'model' ? '按模型' : '按节点'
  const failed = trendError.value ? '（更新失败）' : ''
  return `图表仍是近 ${trendWindowText(trend.value.window)} · ${groupLabel}${failed}，更新于 ${formatDateTime(trendUpdatedAt.value)}`
})

const primaryCards = computed(() => {
  const health = summary.value?.metrics
  const current = stats.value
  const windowLabel = WINDOW_LABEL[statsWindow.value]
  const statsFailure = statsPermission.value ? '无权限' : statsError.value ? '加载失败' : ''
  const healthFailure = healthPermission.value ? '无权限' : healthError.value ? '加载失败' : ''
  const statsPending = !current && statsLoading.value
  const requestsValue = statsFailure || (current ? numberText(current.requests) : statsPending ? '加载中' : '暂无数据')
  const failureValue = healthFailure || (health?.error_rate == null ? '无终态样本' : percentText(health.error_rate))
  const qpsValue = healthFailure || (health?.qps == null ? '无采样' : health.qps.toFixed(2))
  const ttftValue = statsFailure || (statsPending ? '加载中' : current?.ttft_ms.avg == null ? '无采样' : `${Math.round(current.ttft_ms.avg)} ms`)
  const tpsValue = statsFailure || (statsPending ? '加载中' : current?.output_tps.avg == null ? '无采样' : current.output_tps.avg.toFixed(1))
  const activeValue = healthFailure || (health?.active == null ? '采集失败' : numberText(health.active))
  return [
    {
      id: 'requests',
      label: '请求数',
      value: requestsValue,
      unit: statsFailure || current == null ? '' : '条',
      window: windowLabel,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : `${windowLabel}内已落库调用`,
      empty: Boolean(statsFailure) || current == null,
    },
    {
      id: 'failure',
      label: '请求失败率',
      value: failureValue,
      unit: '',
      window: '近 5 分钟',
      hint: healthFailure
        ? (healthPermission.value ? '当前账号无权查看健康摘要' : '健康摘要加载失败')
        : health?.error_rate == null
          ? '近 5 分钟没有已确认结束的请求'
          : `终态样本 ${numberText(health.error_samples)} · 失败 ${numberText(health.error_failures)}${health.error_rate_applies ? '' : ' · 样本不足 20'}`,
      empty: Boolean(healthFailure) || health?.error_rate == null,
    },
    {
      id: 'qps',
      label: 'QPS',
      value: qpsValue,
      unit: 'req/s',
      window: '近 1 分钟',
      hint: healthFailure ? (healthPermission.value ? '当前账号无权查看健康摘要' : '健康摘要加载失败') : '近 1 分钟请求数 / 60 秒',
      empty: Boolean(healthFailure) || health?.qps == null,
    },
    {
      id: 'ttft',
      label: '首 Token 延迟（TTFT）',
      value: ttftValue,
      unit: '',
      window: `${windowLabel}平均`,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : '所选窗口内已确认结束且含首 Token 的请求平均',
      empty: Boolean(statsFailure) || current?.ttft_ms.avg == null,
    },
    {
      id: 'output-tps',
      label: '输出速度（Token/s）',
      value: tpsValue,
      unit: 'Token/s',
      window: `${windowLabel}平均`,
      hint: statsFailure
        ? (statsPermission.value ? '当前账号无权查看统计' : '统计接口加载失败')
        : statsPending
          ? '正在读取统计窗口'
          : '所选窗口内的输出速度平均',
      empty: Boolean(statsFailure) || current?.output_tps.avg == null,
    },
    {
      id: 'active',
      label: '当前并发数',
      value: activeValue,
      unit: '',
      window: '实时',
      hint: healthFailure ? (healthPermission.value ? '当前账号无权查看健康摘要' : '健康摘要加载失败') : '实时运行态，不受统计窗口影响',
      empty: Boolean(healthFailure) || health?.active == null,
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
function trendWindowText(value: string): string {
  return value === '15m' ? '15 分钟' : '1 小时'
}
function showAbnormal() {
  abnormalOnly.value = true
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
      gatewayApi.trends(trendWindow.value, groupBy.value, signal),
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
      trendUpdatedAt.value = new Date().toISOString()
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
watch([trendWindow, groupBy], () => { void load(true) })
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
.gateway-overview { display: grid; grid-auto-rows: max-content; align-content: start; gap: 12px; padding-bottom: 8px; }
.gateway-overview__window { width: 112px; }
.gateway-overview__window-label { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gateway-overview__verdict {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 4px 2px; border: 1px solid transparent; border-radius: 10px; background: transparent;
}
.gateway-overview__verdict[data-status='degraded'] { border-color: #e2c27a; background: #fff8ea; padding: 10px 14px; }
.gateway-overview__verdict[data-status='down'] { border-color: #e7a8ae; background: #fff5f6; padding: 10px 14px; }
.gateway-overview__verdict[data-status='unknown'] { border-color: #c5d4ce; background: #f6f8f7; padding: 10px 14px; }
.gateway-overview__verdict-line { display: flex; flex-wrap: wrap; align-items: baseline; gap: 6px 10px; }
.gateway-overview__status { padding: 2px 8px; border-radius: 999px; background: #e8f6ef; color: #187956; font-size: 12px; font-weight: var(--astra-weight-bold); }
.gateway-overview__verdict[data-status='degraded'] .gateway-overview__status { background: #fbeac4; color: #8a5a08; }
.gateway-overview__verdict[data-status='down'] .gateway-overview__status { background: #f8dadd; color: #9f2f3a; }
.gateway-overview__verdict[data-status='unknown'] .gateway-overview__status { background: #e7eeeb; color: #5c736a; }
.gateway-overview__impact, .gateway-overview__reasons { margin: 0; color: var(--astra-text-secondary); }
.gateway-overview__reasons { margin-top: 4px; }
.gateway-overview__scope-line { margin: -4px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.gateway-overview__metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 10px; }
.gateway-overview__metric-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 6px; }
.gateway-overview__metric-label { margin: 0; color: var(--astra-text-secondary); font-size: var(--astra-font-control); }
.gateway-overview__metric-window { flex: none; padding: 2px 6px; border-radius: 5px; background: var(--astra-bg-elevated); color: var(--astra-text-muted); font-size: 10px; }
.gateway-overview__metric-value { margin: 7px 0 0; color: var(--astra-text-primary); font-size: 24px; line-height: 1.15; font-weight: var(--astra-weight-bold); font-variant-numeric: tabular-nums; }
.gateway-overview__metric-value small { color: var(--astra-text-secondary); font-size: var(--astra-font-helper); font-weight: 400; }
.gateway-overview__metrics :deep(.el-card__body) { padding: 10px 12px 12px; }
.gateway-overview__main :deep(.el-card__body), .gateway-overview__metrics :deep(.el-card__body) { min-height: 0; }
.gateway-overview__metric--empty .gateway-overview__metric-value { color: var(--astra-text-muted); font-size: var(--astra-font-control); }
.gateway-overview__main { display: grid; grid-template-columns: minmax(0, 1.6fr) minmax(280px, 0.8fr); gap: 12px; align-items: start; }
.gateway-overview__card-header { display: flex; align-items: flex-start; gap: 12px; }
.gateway-overview__card-header > div { margin-right: auto; }
.gateway-overview__card-header h4 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-control); }
.gateway-overview__node { color: var(--astra-primary-bright); font-weight: var(--astra-weight-bold); text-decoration: none; }
.gateway-overview__sub { display: block; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.gateway-overview__event-list { display: grid; gap: 8px; margin: 0; padding: 0; list-style: none; }
.gateway-overview__event-list button {
  display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 6px 8px; width: 100%;
  padding: 8px; border: 0; border-radius: 8px; background: transparent; color: var(--astra-text-primary); text-align: left; cursor: pointer;
}
.gateway-overview__event-list button:hover { background: var(--astra-bg-elevated); }
.gateway-overview__event-list time { grid-column: 2; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.gateway-overview__trend-tools { display: flex; flex-wrap: wrap; gap: 8px; }
.gateway-overview__group { width: 120px; }
.gateway-overview__trend-headline { margin: 0 0 8px; color: var(--astra-text-secondary); }
.gateway-overview__trend-stale { margin: 0 0 8px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.gateway-overview__details { border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); overflow: hidden; }
.gateway-overview__details summary { display: flex; align-items: center; gap: 8px; padding: 12px 14px; cursor: pointer; color: var(--astra-text-primary); font-size: var(--astra-font-control); font-weight: var(--astra-weight-bold); list-style: none; }
.gateway-overview__details summary::-webkit-details-marker { display: none; }
.gateway-overview__details summary::before { content: '▸'; color: var(--astra-text-muted); }
.gateway-overview__details[open] summary::before { transform: rotate(90deg); }
.gateway-overview__details summary span { color: var(--astra-text-muted); font-size: var(--astra-font-helper); font-weight: 400; }
.gateway-overview__detail-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; padding: 0 14px 12px; }
.gateway-overview__detail { display: grid; gap: 3px; padding: 9px 10px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-elevated); }
.gateway-overview__detail span { color: var(--astra-text-muted); font-size: 10px; }
.gateway-overview__detail strong { color: var(--astra-text-primary); font-size: var(--astra-font-control); font-variant-numeric: tabular-nums; }
.gateway-overview__detail small { color: var(--astra-text-muted); font-size: 10px; }
.gateway-overview__limit { margin: 0; padding: 0 14px 12px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
@media (max-width: 1300px) {
  .gateway-overview__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); }
}
@media (max-width: 1100px) {
  .gateway-overview__main { grid-template-columns: 1fr; }
  .gateway-overview__detail-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 760px) {
  .gateway-overview__metrics { grid-template-columns: 1fr; }
  .gateway-overview__verdict { align-items: flex-start; flex-direction: column; }
  .gateway-overview__detail-grid { grid-template-columns: 1fr; }
}
</style>
