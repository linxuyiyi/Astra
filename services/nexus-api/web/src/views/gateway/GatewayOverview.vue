<template>
  <section ref="root" class="ov-page" data-testid="gateway-overview">
    <GatewayHeaderActions>
      <ElRadioGroup
        v-model="statsWindow"
        size="small"
        class="ov-window-group"
        aria-label="统计窗口"
        data-testid="gateway-stats-window"
      >
        <ElRadioButton value="hour">1 小时</ElRadioButton>
        <ElRadioButton value="today">今日</ElRadioButton>
        <ElRadioButton value="7d">近 7 天</ElRadioButton>
        <ElRadioButton value="30d">近 30 天</ElRadioButton>
      </ElRadioGroup>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-overview-refresh" @click="load(false)">刷新</ElButton>
    </GatewayHeaderActions>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-overview-error">
      <ElButton type="primary" data-testid="gateway-overview-retry" @click="load(false)">重试</ElButton>
    </ElAlert>
    <ElAlert v-if="statsError" type="error" show-icon :closable="false" :title="statsError" data-testid="gateway-stats-error">
      <ElButton type="primary" @click="load(false)">重试</ElButton>
    </ElAlert>

    <!-- Layer 1: 全局服务态势条 -->
    <section
      class="ov-status"
      :data-status="summary?.status ?? 'unknown'"
      data-testid="gateway-health-bar"
    >
      <div class="ov-status__left">
        <span class="gateway-health-label ov-status__badge" data-testid="gateway-health-status">
          <i class="astra-service-dot" :data-state="summary?.status ?? 'unknown'" aria-hidden="true" />
          {{ statusLabel }}
        </span>
        <div class="ov-status__copy">
          <p class="ov-status__impact" data-testid="gateway-health-impact">{{ impactText }}</p>
          <p v-if="showReasons" class="ov-status__reasons">{{ reasonText }}</p>
        </div>
        <div v-if="impactPools.length" class="ov-status__chips">
          <RouterLink
            v-for="item in impactPools.slice(0, 3)"
            :key="item.pool_id"
            class="ov-impact-chip"
            :to="{ path: '/gateway/nodes', query: { pool: item.pool_id } }"
            :title="item.text"
          >
            <i class="ov-impact-chip__dot" :data-state="item.state" aria-hidden="true" />
            <span>{{ item.pool_name }}</span>
          </RouterLink>
          <span v-if="impactPools.length > 3" class="ov-impact-more">+{{ impactPools.length - 3 }}</span>
        </div>
      </div>

      <div class="ov-status__right">
        <button type="button" class="ov-pulse-pill" @click="scrollToInstances">
          <i class="ov-pulse-dot" :class="onlineInstanceCount > 0 ? 'is-ok' : 'is-off'" aria-hidden="true" />
          <span>数据面</span>
          <b>{{ onlineInstanceCount }}/{{ instances.length }}</b>
        </button>
        <button type="button" class="ov-pulse-pill" @click="scrollToNodes('all')">
          <i class="ov-pulse-dot" :class="healthyRows.length > 0 ? 'is-ok' : rows.length > 0 ? 'is-warn' : 'is-off'" aria-hidden="true" />
          <span>健康节点</span>
          <b>{{ healthyRows.length }}/{{ rows.length }}</b>
        </button>
        <ElButton
          v-if="abnormalRows.length > 0"
          size="small"
          plain
          data-testid="gateway-show-abnormal"
          @click="showAbnormal"
        >查看异常（{{ abnormalRows.length }}）</ElButton>
        <ElButton
          v-else-if="rows.length === 0"
          size="small"
          type="primary"
          plain
          @click="router.push('/gateway/nodes')"
        >配置上游节点</ElButton>
      </div>
    </section>

    <!-- Layer 2: 四大支柱 KPI 卡片（请求量 / 成功率 / Token 消耗 / 延迟性能） -->
    <section class="ov-kpis" aria-label="核心运行指标">
      <article class="ov-kpi" data-testid="gateway-metric-requests">
        <header class="ov-kpi__head">
          <span class="ov-kpi__label">总请求量</span>
          <span class="ov-kpi__tag">{{ currentWindowLabel }}</span>
        </header>
        <div class="ov-kpi__value" :class="{ 'is-muted': kpiUnavailable }">
          {{ kpiRequests.main }}
          <small v-if="kpiRequests.unit">{{ kpiRequests.unit }}</small>
        </div>
        <footer class="ov-kpi__foot">
          <span>{{ kpiRequests.sub }}</span>
        </footer>
      </article>

      <article class="ov-kpi" data-testid="gateway-metric-failure">
        <header class="ov-kpi__head">
          <span class="ov-kpi__label">请求成功率</span>
          <span class="ov-kpi__tag">{{ currentWindowLabel }}</span>
        </header>
        <div class="ov-kpi__value" :class="{ 'is-muted': kpiSuccess.muted, 'is-bad': kpiSuccess.bad }">
          {{ kpiSuccess.main }}
        </div>
        <footer class="ov-kpi__foot">
          <span>
            成功 <b>{{ kpiSuccess.okText }}</b> · 失败
            <b :class="{ 'is-danger': kpiSuccess.errors > 0 }">{{ kpiSuccess.errText }}</b>
          </span>
          <div class="ov-ratio-bar" aria-hidden="true">
            <i class="ov-ratio-bar__ok" :style="{ width: `${kpiSuccess.okPct}%` }" />
            <i class="ov-ratio-bar__err" :style="{ width: `${kpiSuccess.errPct}%` }" />
          </div>
        </footer>
      </article>

      <article class="ov-kpi" data-testid="gateway-metric-tokens">
        <header class="ov-kpi__head">
          <span class="ov-kpi__label">Token 消耗</span>
          <span class="ov-kpi__tag">{{ currentWindowLabel }}</span>
        </header>
        <div class="ov-kpi__value" :class="{ 'is-muted': kpiUnavailable }">
          {{ kpiTokens.main }}
        </div>
        <footer class="ov-kpi__foot">
          <span>输入 <b>{{ kpiTokens.inText }}</b> · 输出 <b>{{ kpiTokens.outText }}</b></span>
          <div class="ov-ratio-bar" aria-hidden="true">
            <i class="ov-ratio-bar__in" :style="{ width: `${kpiTokens.inPct}%` }" />
            <i class="ov-ratio-bar__out" :style="{ width: `${kpiTokens.outPct}%` }" />
          </div>
        </footer>
      </article>

      <article class="ov-kpi" data-testid="gateway-metric-ttft">
        <header class="ov-kpi__head">
          <span class="ov-kpi__label">{{ kpiLatency.label }}</span>
          <span class="ov-kpi__tag">{{ currentWindowLabel }} P95</span>
        </header>
        <div class="ov-kpi__value" :class="{ 'is-muted': kpiLatency.muted }">
          {{ kpiLatency.main }}
          <small v-if="kpiLatency.unit">{{ kpiLatency.unit }}</small>
        </div>
        <footer class="ov-kpi__foot">
          <span>输出 <b>{{ kpiLatency.tpsText }}</b> · 总耗时 P95 <b>{{ kpiLatency.durText }}</b></span>
        </footer>
      </article>
    </section>

    <!-- Layer 3: 首屏核心双栏（多维趋势图 + 延迟剖析与稳定性） -->
    <div class="ov-main-grid">
      <ElCard shadow="never" class="ov-card ov-trend-card" data-testid="gateway-overview-trend">
        <template #header>
          <div class="ov-card__head">
            <div class="ov-card__title">
              <h4>流量与性能趋势</h4>
              <p class="ov-card__sub">{{ chartHeadline }}</p>
            </div>
            <div class="ov-trend-tools">
              <ElRadioGroup v-model="chartTab" size="small" aria-label="趋势视角" data-testid="gateway-trend-tab">
                <ElRadioButton value="calls">请求量</ElRadioButton>
                <ElRadioButton value="tokens">Token</ElRadioButton>
                <ElRadioButton value="ttft">TTFT</ElRadioButton>
                <ElRadioButton value="tps">速度</ElRadioButton>
              </ElRadioGroup>
              <ElSelect v-model="groupBy" size="small" class="ov-select-sm" aria-label="趋势分组">
                <ElOption value="none" label="全部维度" />
                <ElOption value="model" label="按模型" />
                <ElOption value="node" label="按节点" />
              </ElSelect>
              <ElSelect
                v-if="seriesOptions.length > 1"
                v-model="seriesKey"
                size="small"
                class="ov-select-md"
                aria-label="趋势序列"
              >
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

        <template v-if="hasActiveChartData">
          <GatewayTrendChart
            v-if="chartTab === 'calls'"
            mode="calls"
            :call-points="callPoints"
            tall
          />
          <GatewayTrendChart
            v-else-if="chartTab === 'tokens' && groupBy === 'none' && useSplitTokenBuckets"
            mode="tokens"
            :buckets="tokenBuckets"
            tall
          />
          <GatewayTrendChart
            v-else-if="chartTab === 'tokens'"
            mode="total-tokens"
            value-name="总 Token"
            :points="tokenSeriesPoints"
            tall
          />
          <GatewayTrendChart
            v-else-if="chartTab === 'ttft'"
            mode="value"
            value-name="TTFT P95"
            unit="ms"
            :points="ttftSeriesPoints"
            tall
          />
          <GatewayTrendChart
            v-else
            mode="value"
            value-name="输出速度 P95"
            unit=" tok/s"
            :points="tpsSeriesPoints"
            tall
          />
        </template>
        <div v-else-if="!trendError" class="ov-chart-empty">
          <div class="ov-chart-empty__grid" aria-hidden="true">
            <span /><span /><span /><span />
          </div>
          <div class="ov-chart-empty__body">
            <p class="ov-chart-empty__title">{{ chartEmptyText }}</p>
            <RouterLink class="ov-link" to="/gateway/keys">查看 /v1 接入地址与 API Key →</RouterLink>
          </div>
        </div>
      </ElCard>

      <ElCard shadow="never" class="ov-card ov-profile-card" data-testid="gateway-overview-details">
        <template #header>
          <div class="ov-card__head">
            <div class="ov-card__title">
              <h4>延迟剖析与稳定性</h4>
              <p class="ov-card__sub">分阶段耗时与近 5 分钟健康信号</p>
            </div>
            <ElRadioGroup v-model="latencyStatMode" size="small" aria-label="延迟分位">
              <ElRadioButton value="avg">平均</ElRadioButton>
              <ElRadioButton value="p50">P50</ElRadioButton>
              <ElRadioButton value="p95">P95</ElRadioButton>
            </ElRadioGroup>
          </div>
        </template>

        <div class="ov-latency-list">
          <div v-for="stage in latencyStages" :key="stage.id" class="ov-latency-row">
            <div class="ov-latency-row__top">
              <span class="ov-latency-row__label">{{ stage.label }}</span>
              <b class="ov-latency-row__val" :class="{ 'is-muted': stage.value === null }">
                {{ stage.value === null ? '—' : formatLatency(stage.value) }}
              </b>
            </div>
            <div class="ov-latency-row__track">
              <i
                class="ov-latency-row__fill"
                :class="stage.tone"
                :style="{ width: `${stage.pct}%` }"
              />
            </div>
          </div>
        </div>

        <div class="ov-divider" />

        <div class="ov-stability">
          <div class="ov-stability__head">
            <span>近 5 分钟稳定性</span>
            <span class="ov-stability__cap">峰值并发 {{ peakConcurrencyText }}</span>
          </div>
          <div class="ov-stability__grid">
            <div v-for="item in stabilityItems" :key="item.id" class="ov-stability__cell">
              <span class="ov-stability__label">{{ item.label }}</span>
              <strong class="ov-stability__val" :class="item.warn ? 'is-warn' : ''">{{ item.value }}</strong>
            </div>
          </div>
        </div>
      </ElCard>
    </div>

    <!-- Layer 4A: 上游节点运行矩阵 -->
    <ElCard ref="nodesCard" shadow="never" class="ov-card ov-nodes-card" data-testid="gateway-overview-nodes">
      <template #header>
        <div class="ov-card__head">
          <div class="ov-card__title">
            <h4>上游节点运行态</h4>
            <p class="ov-card__sub">按健康状态、并发水位与窗口性能指标排序</p>
          </div>
          <div class="ov-card__tools">
            <ElRadioGroup v-model="nodeFilter" size="small" aria-label="节点筛选" data-testid="gateway-abnormal-only">
              <ElRadioButton value="all">全部（{{ rows.length }}）</ElRadioButton>
              <ElRadioButton value="abnormal">异常（{{ abnormalRows.length }}）</ElRadioButton>
              <ElRadioButton value="healthy">健康（{{ healthyRows.length }}）</ElRadioButton>
            </ElRadioGroup>
            <RouterLink class="ov-link" to="/gateway/nodes">管理节点 →</RouterLink>
          </div>
        </div>
      </template>

      <ElAlert v-if="nodesError" type="warning" show-icon :closable="false" :title="nodesError" />
      <ElTable
        v-else
        :data="visibleRows"
        row-key="node_id"
        max-height="340"
        :empty-text="tableEmpty"
      >
        <ElTableColumn label="节点 / 资源池" min-width="190">
          <template #default="{ row }">
            <div class="ov-node-cell">
              <RouterLink class="ov-node-link" :to="`/gateway/nodes/${row.node_id}`">{{ row.name }}</RouterLink>
              <span class="ov-node-pool">{{ row.pool_name ?? '未分配' }}</span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="运行状态" width="166">
          <template #default="{ row }">
            <div class="ov-state-tags">
              <ElTag
                size="small"
                :type="healthTag(row.health)"
                :class="{ 'astra-tag--neutral': row.health === 'unknown' }"
              >{{ row.health_label }}</ElTag>
              <ElTag
                size="small"
                :type="schedulingTag(row.scheduling)"
                :class="{ 'astra-tag--neutral': row.scheduling === 'disabled' }"
              >{{ row.scheduling_label }}</ElTag>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="负载水位" min-width="168">
          <template #default="{ row }">
            <div class="ov-load-cell">
              <div class="ov-load-bar">
                <i
                  :style="{ width: `${occupancyPercent(row as GatewayNodeMetric)}%` }"
                  :class="occupancyTone(row as GatewayNodeMetric)"
                />
              </div>
              <span class="ov-num">{{ loadCompactText(row as GatewayNodeMetric) }}</span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="QPS" width="88" align="right">
          <template #default="{ row }">
            <span class="ov-num">{{ row.qps != null ? row.qps.toFixed(2) : '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="TTFT P95" width="104" align="right">
          <template #default="{ row }">
            <span class="ov-num">{{ row.ttft_p95_ms != null ? formatLatency(row.ttft_p95_ms) : '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="错误率" width="104" align="right">
          <template #default="{ row }">
            <span
              class="ov-num"
              :class="{ 'is-danger': row.error_rate != null && row.error_rate >= 0.05 }"
              :title="row.error_samples ? `样本数 ${row.error_samples}` : undefined"
            >{{ row.error_rate != null ? percentText(row.error_rate) : '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="76" align="center" fixed="right">
          <template #default="{ row }">
            <ElButton link type="primary" @click="openNodeLogs(row as GatewayNodeMetric)">日志</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <!-- Layer 4B: 基础设施与最近动态双栏 -->
    <div class="ov-bottom-grid">
      <ElCard ref="instancesCard" shadow="never" class="ov-card ov-instances-card" data-testid="gateway-overview-instances">
        <template #header>
          <div class="ov-card__head">
            <div class="ov-card__title">
              <h4>Gateway 数据面实例</h4>
              <p class="ov-card__sub">转发进程心跳、吞吐与系统资源</p>
            </div>
            <span v-if="instances.length" class="ov-count-pill">
              在线 {{ onlineInstanceCount }} / {{ instances.length }}
            </span>
          </div>
        </template>
        <ElAlert v-if="instancesError" type="warning" show-icon :closable="false" :title="instancesError" />
        <ElTable v-else-if="instances.length" :data="instances" row-key="instance_id" size="small">
          <ElTableColumn label="实例 / 版本" min-width="200">
            <template #default="{ row }">
              <div class="ov-inst-cell">
                <span class="ov-mono ov-inst-id" :title="row.instance_id">{{ row.instance_id }}</span>
                <span class="ov-inst-meta">v{{ row.app_version ?? '—' }} · 运行 {{ formatSeconds(row.uptime_seconds) }}</span>
              </div>
            </template>
          </ElTableColumn>
          <ElTableColumn label="状态 / 心跳" width="128">
            <template #default="{ row }">
              <div class="ov-inst-status">
                <ElTag
                  size="small"
                  :type="row.status === 'ACTIVE' ? 'success' : 'info'"
                  :class="{ 'astra-tag--neutral': row.status !== 'ACTIVE' }"
                >{{ row.status === 'ACTIVE' ? '在线' : '超时' }}</ElTag>
                <span class="ov-num">{{ formatRelativeTime(row.updated_at) }}</span>
              </div>
            </template>
          </ElTableColumn>
          <ElTableColumn label="并发 / 请求" width="104" align="right">
            <template #default="{ row }">
              <span class="ov-num">{{ row.active ?? '—' }} / {{ row.requests ?? '—' }}</span>
            </template>
          </ElTableColumn>
          <ElTableColumn label="CPU / 内存" width="126" align="right">
            <template #default="{ row }">
              <span class="ov-num">{{ row.cpu_percent != null ? `${row.cpu_percent.toFixed(1)}%` : '—' }} / {{ formatBytes(row.memory_bytes) }}</span>
            </template>
          </ElTableColumn>
        </ElTable>
        <p v-else class="ov-muted-empty">暂无 Gateway 数据面实例上报心跳</p>
      </ElCard>

      <ElCard shadow="never" class="ov-card ov-events-card" data-testid="gateway-overview-events">
        <template #header>
          <div class="ov-card__head">
            <div class="ov-card__title">
              <h4>最近动态</h4>
              <p class="ov-card__sub">节点探测告警与配置变更</p>
            </div>
            <ElRadioGroup v-model="eventFilter" size="small" aria-label="动态类型">
              <ElRadioButton value="all">全部</ElRadioButton>
              <ElRadioButton value="runtime">运行</ElRadioButton>
              <ElRadioButton value="config">配置</ElRadioButton>
            </ElRadioGroup>
          </div>
        </template>
        <ElAlert v-if="eventsError" type="warning" show-icon :closable="false" :title="eventsError" />
        <ul v-else-if="filteredEvents.length" class="ov-event-list">
          <li v-for="event in filteredEvents" :key="event.id">
            <button type="button" @click="openEventLogs(event)">
              <ElTag
                size="small"
                :type="event.source === 'config' ? 'info' : 'warning'"
                :class="{ 'astra-tag--neutral': event.source === 'config' }"
              >{{ event.source === 'config' ? '配置' : '运行' }}</ElTag>
              <span class="ov-event-summary" :title="event.summary">{{ formatEventSummary(event) }}</span>
              <time>{{ formatDateTime(event.created_at) }}</time>
            </button>
          </li>
        </ul>
        <p v-else class="ov-muted-empty">{{ eventFilter === 'all' ? '暂无运行或配置动态' : '当前分类下暂无记录' }}</p>
      </ElCard>
    </div>
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
  ElOption,
  ElRadioButton,
  ElRadioGroup,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
} from 'element-plus'
import {
  gatewayApi,
  GatewayApiError,
  type GatewayHealthSummary,
  type GatewayInstance,
  type GatewayNodeMetric,
  type GatewayOpsEvent,
  type GatewayScopeStats,
  type GatewayStatsWindow,
  type GatewayTrend,
} from '@/api/gateway'
import { formatTokens } from '@/utils/format'
import {
  auditActionLabel,
  auditResourceTypeLabel,
  errorMessage,
  formatBytes,
  formatDateTime,
  formatRelativeTime,
  formatSeconds,
} from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import GatewayTrendChart from '@/views/gateway/GatewayTrendChart.vue'

const router = useRouter()
const root = ref<HTMLElement | null>(null)
const nodesCard = ref<{ $el?: HTMLElement } | null>(null)
const instancesCard = ref<{ $el?: HTMLElement } | null>(null)

const summary = ref<GatewayHealthSummary | null>(null)
const stats = ref<GatewayScopeStats | null>(null)
const rows = ref<GatewayNodeMetric[]>([])
const events = ref<GatewayOpsEvent[]>([])
const instances = ref<GatewayInstance[]>([])
const trend = ref<GatewayTrend | null>(null)

const error = ref('')
const healthError = ref('')
const healthPermission = ref(false)
const statsError = ref('')
const statsPermission = ref(false)
const statsLoading = ref(false)
const nodesError = ref('')
const eventsError = ref('')
const instancesError = ref('')
const trendError = ref('')
const loading = ref(false)

const statsWindow = ref<GatewayStatsWindow>('today')
const chartTab = ref<'calls' | 'tokens' | 'ttft' | 'tps'>('calls')
const latencyStatMode = ref<'avg' | 'p50' | 'p95'>('p95')
const groupBy = ref<'none' | 'model' | 'node'>('none')
const seriesKey = ref('all')
const nodeFilter = ref<'all' | 'abnormal' | 'healthy'>('all')
const eventFilter = ref<'all' | 'runtime' | 'config'>('all')

let timer = 0

const WINDOW_LABEL: Record<GatewayStatsWindow, string> = {
  hour: '近 1 小时',
  today: '今日',
  '7d': '近 7 天',
  '30d': '近 30 天',
}

const currentWindowLabel = computed(() => WINDOW_LABEL[statsWindow.value])
const statusLabel = computed(() => {
  const status = summary.value?.status
  if (status === 'normal') return '服务正常'
  if (status === 'degraded') return '服务降级'
  if (status === 'down') return '不可用'
  return '状态未知'
})
const impactText = computed(() => summary.value?.impact_summary || '所有已启用路由状态正常')
const reasonText = computed(() => (summary.value?.reasons ?? []).join('；'))
const showReasons = computed(() => Boolean(reasonText.value) && reasonText.value !== impactText.value)

const impactPools = computed(() => {
  const seen = new Map<string, { pool_id: string; pool_name: string; state: string; text: string }>()
  for (const item of summary.value?.impact ?? []) {
    if (!seen.has(item.pool_id)) {
      seen.set(item.pool_id, {
        pool_id: item.pool_id,
        pool_name: item.pool_name,
        state: item.state,
        text: item.text,
      })
    }
  }
  return [...seen.values()]
})

const onlineInstanceCount = computed(() => instances.value.filter((item) => item.status === 'ACTIVE').length)
const healthyRows = computed(() => rows.value.filter((row) => !row.abnormal))
const abnormalRows = computed(() => rows.value.filter((row) => row.abnormal))
const visibleRows = computed(() => {
  if (nodeFilter.value === 'abnormal') return abnormalRows.value
  if (nodeFilter.value === 'healthy') return healthyRows.value
  return [...abnormalRows.value, ...healthyRows.value]
})
const tableEmpty = computed(() => {
  if (!rows.value.length) return '暂未配置上游节点'
  if (nodeFilter.value === 'abnormal') return '当前没有异常节点'
  if (nodeFilter.value === 'healthy') return '当前没有健康可用节点'
  return '暂无节点'
})

const filteredEvents = computed(() => {
  if (eventFilter.value === 'all') return events.value
  return events.value.filter((item) => item.source === eventFilter.value)
})

// ── 四大支柱 KPI ──────────────────────────────────────────────────────
const kpiUnavailable = computed(() => Boolean(statsPermission.value || statsError.value) || (!stats.value && statsLoading.value))
const kpiFallback = computed(() => (
  statsPermission.value ? '无权限' : statsError.value ? '加载失败' : statsLoading.value ? '…' : '—'
))

const kpiRequests = computed(() => {
  const current = stats.value
  if (!current) {
    return { main: kpiFallback.value, unit: '', sub: '等待统计数据' }
  }
  const peakPart = current.peak_concurrency > 0 ? ` · 峰值 ${current.peak_concurrency}` : ''
  return {
    main: numberText(current.requests),
    unit: '次',
    sub: `平均 ${current.qps.toFixed(2)} req/s · 并发 ${current.active}${peakPart}`,
  }
})

const kpiSuccess = computed(() => {
  const current = stats.value
  if (!current) {
    return { main: kpiFallback.value, muted: true, bad: false, okText: '—', errText: '—', errors: 0, okPct: 0, errPct: 0 }
  }
  const terminal = current.success + current.errors
  if (terminal <= 0) {
    return { main: '—', muted: true, bad: false, okText: '0', errText: '0', errors: 0, okPct: 0, errPct: 0 }
  }
  const rate = current.success / terminal
  const okPct = Math.round(rate * 1000) / 10
  const errPct = Math.max(0, Math.round((100 - okPct) * 10) / 10)
  return {
    main: `${(rate * 100).toFixed(rate >= 0.9995 ? 0 : 1)}%`,
    muted: false,
    bad: rate < 0.95,
    okText: numberText(current.success),
    errText: numberText(current.errors),
    errors: current.errors,
    okPct,
    errPct,
  }
})

const kpiTokens = computed(() => {
  const current = stats.value
  if (!current) {
    return { main: kpiFallback.value, inText: '—', outText: '—', inPct: 0, outPct: 0 }
  }
  const sum = current.input_tokens + current.output_tokens
  const inPct = sum > 0 ? Math.round((current.input_tokens / sum) * 100) : 0
  const outPct = sum > 0 ? Math.max(0, 100 - inPct) : 0
  return {
    main: formatTokens(current.total_tokens),
    inText: formatTokens(current.input_tokens),
    outText: formatTokens(current.output_tokens),
    inPct,
    outPct,
  }
})

const kpiLatency = computed(() => {
  const current = stats.value
  if (!current) {
    return { label: '首 Token 延迟', main: kpiFallback.value, unit: '', muted: true, tpsText: '—', durText: '—' }
  }
  const ttft = current.ttft_ms.p95
  const dur = current.duration_ms.p95
  const tps = current.output_tps.avg
  const primaryVal = ttft ?? dur
  const label = ttft !== null ? '首 Token 延迟' : dur !== null ? '请求耗时' : '首 Token 延迟'
  if (primaryVal === null) {
    return {
      label,
      main: '—',
      unit: '',
      muted: true,
      tpsText: tps !== null ? `${tps.toFixed(1)} tok/s` : '—',
      durText: '—',
    }
  }
  const formatted = primaryVal >= 1000
    ? { val: (primaryVal / 1000).toFixed(2), unit: 's' }
    : { val: String(Math.round(primaryVal)), unit: 'ms' }
  return {
    label,
    main: formatted.val,
    unit: formatted.unit,
    muted: false,
    tpsText: tps !== null ? `${tps.toFixed(1)} tok/s` : '—',
    durText: dur !== null ? formatLatency(dur) : '—',
  }
})

// ── 趋势图数据 ────────────────────────────────────────────────────────
const seriesOptions = computed(() => trend.value?.series ?? [])
const activeSeries = computed(() => seriesOptions.value.find((item) => item.key === seriesKey.value) ?? seriesOptions.value[0])
const rawTrendPoints = computed(() => activeSeries.value?.points ?? [])

const callPoints = computed(() => rawTrendPoints.value.map((p) => ({
  bucket_start: p.bucket_start,
  requests: p.requests,
  errors: p.errors,
})))

const tokenBuckets = computed(() => {
  const list = stats.value?.trend ?? []
  return list.some((b) => b.total_tokens > 0 || b.input_tokens > 0 || b.output_tokens > 0) ? list : []
})

const tokenSeriesPoints = computed(() => rawTrendPoints.value.map((p) => ({
  bucket_start: p.bucket_start,
  value: p.total_tokens,
})))

const useSplitTokenBuckets = computed(() => {
  if (!tokenBuckets.value.length) return false
  if (tokenBuckets.value.length >= 2) return true
  const trendActiveCount = tokenSeriesPoints.value.filter((p) => (p.value ?? 0) > 0).length
  return trendActiveCount <= 1
})

const ttftSeriesPoints = computed(() => rawTrendPoints.value.map((p) => ({
  bucket_start: p.bucket_start,
  value: p.ttft_p95_ms !== null ? Math.round(p.ttft_p95_ms) : null,
})))

const tpsSeriesPoints = computed(() => rawTrendPoints.value.map((p) => ({
  bucket_start: p.bucket_start,
  value: p.output_tps_p95 !== null ? Number(p.output_tps_p95.toFixed(1)) : null,
})))

const hasActiveChartData = computed(() => {
  if (chartTab.value === 'calls') {
    return callPoints.value.some((p) => p.requests > 0 || p.errors > 0)
  }
  if (chartTab.value === 'tokens') {
    if (groupBy.value === 'none' && tokenBuckets.value.length) return true
    return tokenSeriesPoints.value.some((p) => (p.value ?? 0) > 0)
  }
  if (chartTab.value === 'ttft') {
    return ttftSeriesPoints.value.some((p) => p.value !== null)
  }
  return tpsSeriesPoints.value.some((p) => p.value !== null)
})

const chartHeadline = computed(() => {
  if (chartTab.value === 'calls') {
    const req = callPoints.value.reduce((s, p) => s + p.requests, 0)
    const err = callPoints.value.reduce((s, p) => s + p.errors, 0)
    return req > 0 ? `${currentWindowLabel.value}共 ${numberText(req)} 次请求 · 失败 ${numberText(err)} 次` : `${currentWindowLabel.value}暂无请求流量`
  }
  if (chartTab.value === 'tokens') {
    const total = groupBy.value === 'none'
      ? (stats.value?.total_tokens ?? 0)
      : tokenSeriesPoints.value.reduce((s, p) => s + (p.value ?? 0), 0)
    return total > 0 ? `${currentWindowLabel.value}累计 ${formatTokens(total)} Token` : `${currentWindowLabel.value}暂无 Token 用量记录`
  }
  if (chartTab.value === 'ttft') {
    const p95 = trend.value?.ttft_p95_ms ?? stats.value?.ttft_ms.p95 ?? null
    return p95 !== null ? `窗口整体首 Token 延迟 P95：${formatLatency(p95)}` : `${currentWindowLabel.value}暂无首 Token 延迟采样`
  }
  const tps = trend.value?.output_tps_p95 ?? stats.value?.output_tps.p95 ?? null
  return tps !== null ? `窗口整体输出速度 P95：${tps.toFixed(1)} tok/s` : `${currentWindowLabel.value}暂无流式输出速度采样`
})

const chartEmptyText = computed(() => {
  if (chartTab.value === 'calls') return `${currentWindowLabel.value}暂无调用流量`
  if (chartTab.value === 'tokens') return `${currentWindowLabel.value}暂无 Token 统计数据`
  if (chartTab.value === 'ttft') return `${currentWindowLabel.value}暂无流式 TTFT 采样`
  return `${currentWindowLabel.value}暂无输出速度采样`
})

// ── 延迟剖析与稳定性 ──────────────────────────────────────────────────
const latencyStages = computed(() => {
  const current = stats.value
  const key = latencyStatMode.value
  const raw = [
    { id: 'connect', label: '建立连接 (Connect)', value: current?.connect_ms[key] ?? null, tone: 'tone-1' },
    { id: 'ttfb', label: '首字节响应 (TTFB)', value: current?.ttfb_ms[key] ?? null, tone: 'tone-2' },
    { id: 'ttft', label: '首 Token 生成 (TTFT)', value: current?.ttft_ms[key] ?? null, tone: 'tone-3' },
    { id: 'duration', label: '请求总耗时 (Total)', value: current?.duration_ms[key] ?? null, tone: 'tone-4' },
  ]
  const maxLog = Math.max(1, ...raw.map((r) => (r.value !== null && r.value > 0 ? Math.log1p(r.value) : 0)))
  return raw.map((item) => ({
    ...item,
    pct: item.value !== null && item.value > 0 ? Math.max(8, Math.round((Math.log1p(item.value) / maxLog) * 100)) : 0,
  }))
})

const peakConcurrencyText = computed(() => {
  const peak = summary.value?.metrics.peak_concurrency ?? stats.value?.peak_concurrency
  return peak == null ? '—' : numberText(peak)
})

const stabilityItems = computed(() => {
  const health = summary.value?.metrics
  const unavailable = healthPermission.value ? '无权限' : healthError.value ? '失败' : ''
  const cap = !health || health.active == null
    ? '—'
    : `${numberText(health.active)} / ${health.capacity_limit == null ? '不限' : numberText(health.capacity_limit)}`
  const queueSuffix = health?.queue ? `（排队 ${health.queue}）` : ''
  return [
    {
      id: 'retry',
      label: '重试率',
      value: unavailable || percentOrDash(health?.retry_rate),
      warn: Boolean(health?.retry_rate && health.retry_rate >= 0.05),
    },
    {
      id: '429',
      label: '上游 429 限流',
      value: unavailable || percentOrDash(health?.upstream_429_rate),
      warn: Boolean(health?.upstream_429_rate && health.upstream_429_rate > 0),
    },
    {
      id: 'stream',
      label: '流式中断率',
      value: unavailable || percentOrDash(health?.stream_interrupt_rate),
      warn: Boolean(health?.stream_interrupt_rate && health.stream_interrupt_rate >= 0.02),
    },
    {
      id: 'capacity',
      label: '实时并发容量',
      value: unavailable || `${cap}${queueSuffix}`,
      warn: Boolean(health?.capacity_occupancy && health.capacity_occupancy >= 0.8),
    },
  ]
})

// ── 工具函数 ──────────────────────────────────────────────────────────
function numberText(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(value)
}
function percentText(value: number): string {
  return `${(value * 100).toFixed(value >= 0.1 ? 1 : 2)}%`
}
function percentOrDash(value: number | null | undefined): string {
  return value == null ? '—' : percentText(value)
}
function formatLatency(ms: number): string {
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`
  return `${Math.round(ms)} ms`
}
function occupancyPercent(row: GatewayNodeMetric): number {
  if (row.occupancy === null) return 0
  return Math.min(100, Math.max(0, Math.round(row.occupancy * 100)))
}
function occupancyTone(row: GatewayNodeMetric): string {
  if (row.occupancy === null) return ''
  if (row.occupancy >= 0.85) return 'is-danger'
  if (row.occupancy >= 0.65) return 'is-warn'
  return ''
}
function loadCompactText(row: GatewayNodeMetric): string {
  if (row.active === null) return '采集失败'
  const cap = row.capacity_limit ? `${row.active}/${row.capacity_limit}` : `${row.active}/不限`
  return row.queue ? `${cap} · 排队 ${row.queue}` : cap
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

async function scrollToNodes(filter: 'all' | 'abnormal' | 'healthy') {
  nodeFilter.value = filter
  await nextTick()
  nodesCard.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}
async function showAbnormal() {
  await scrollToNodes('abnormal')
}
async function scrollToInstances() {
  await nextTick()
  instancesCard.value?.$el?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function openNodeLogs(row: GatewayNodeMetric) {
  const end = new Date()
  const start = new Date(end.getTime() - 15 * 60 * 1000)
  void router.push({
    path: '/gateway/logs',
    query: { node_id: row.node_id, from: start.toISOString(), to: end.toISOString() },
  })
}

function formatEventSummary(event: GatewayOpsEvent): string {
  if (event.source !== 'config') return event.summary
  const parts = event.summary.split(' · ')
  if (parts.length < 2) return event.summary
  const action = auditActionLabel(parts[0].trim())
  const rest = parts.slice(1).join(' · ').trim()
  const [resType, ...idParts] = rest.split(/\s+/)
  const id = idParts.join(' ')
  const resLabel = auditResourceTypeLabel(resType)
  if (action !== parts[0].trim()) {
    return id ? `${action} · ${id}` : `${action} · ${resLabel}`
  }
  return event.summary
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
    const [nodeRows, eventRows, trendValue, statsValue, instanceRows] = await Promise.allSettled([
      gatewayApi.nodeMetrics(signal),
      gatewayApi.opsEvents(40, signal),
      gatewayApi.trends(statsWindow.value, groupBy.value, signal),
      gatewayApi.stats('overview', statsWindow.value),
      gatewayApi.instances(),
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
    if (instanceRows.status === 'fulfilled') {
      instances.value = instanceRows.value
      instancesError.value = ''
    } else if (!isAbort(instanceRows.reason)) {
      instancesError.value = errorMessage(instanceRows.reason, 'Gateway 实例加载失败')
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
.ov-page { display: flex; flex-direction: column; gap: 14px; padding-bottom: 12px; }
.ov-window-group { flex: none; }

/* ── Layer 1: 服务态势条 ─────────────────────────────────────────── */
.ov-status {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px;
  padding: 11px 16px;
  border: 1px solid var(--astra-border);
  border-left: 3px solid var(--astra-neutral);
  border-radius: 10px;
  background: var(--astra-bg-panel);
}
.ov-status[data-status='normal'] { border-left-color: var(--astra-success); }
.ov-status[data-status='degraded'] { border-left-color: var(--astra-warning); }
.ov-status[data-status='down'] { border-left-color: var(--astra-danger); }

.ov-status__left { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 14px; min-width: 0; flex: 1 1 auto; }
.ov-status__badge { flex: none; font-size: 13px; }
.ov-status__copy { display: flex; align-items: baseline; flex-wrap: wrap; gap: 4px 10px; min-width: 0; }
.ov-status__impact { margin: 0; color: var(--astra-text-primary); font-size: 13px; font-weight: 600; }
.ov-status__reasons { margin: 0; color: var(--astra-text-muted); font-size: 12px; }
.ov-status__chips { display: flex; align-items: center; flex-wrap: wrap; gap: 6px; }
.ov-impact-chip {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 2px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 999px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  text-decoration: none;
  transition: border-color 0.15s ease, color 0.15s ease;
}
.ov-impact-chip:hover { border-color: var(--astra-border-hover); color: var(--astra-text-primary); }
.ov-impact-chip__dot { width: 6px; height: 6px; border-radius: 50%; background: var(--astra-danger); }
.ov-impact-chip__dot[data-state='degraded'] { background: var(--astra-warning); }
.ov-impact-more { color: var(--astra-text-muted); font-size: 12px; }

.ov-status__right { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; flex: none; }
.ov-pulse-pill {
  display: inline-flex; align-items: center; gap: 6px;
  padding: 4px 10px;
  border: 1px solid var(--astra-border);
  border-radius: 999px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.ov-pulse-pill:hover { border-color: var(--astra-border-hover); background: var(--astra-bg-panel); color: var(--astra-text-primary); }
.ov-pulse-pill b { color: var(--astra-text-primary); font-weight: 600; font-variant-numeric: tabular-nums; }
.ov-pulse-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--astra-neutral); }
.ov-pulse-dot.is-ok { background: var(--astra-success); }
.ov-pulse-dot.is-warn { background: var(--astra-warning); }

/* ── Layer 2: 四大支柱 KPI ───────────────────────────────────────── */
.ov-kpis { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
.ov-kpi {
  display: flex; flex-direction: column; justify-content: space-between; gap: 8px;
  padding: 14px 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
  min-width: 0;
}
.ov-kpi__head { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.ov-kpi__label { color: var(--astra-text-secondary); font-size: 12px; font-weight: 500; }
.ov-kpi__tag {
  padding: 1px 6px;
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 11px;
}
.ov-kpi__value {
  display: flex; align-items: baseline; gap: 4px;
  color: var(--astra-text-primary);
  font-size: 28px;
  line-height: 34px;
  font-weight: 600;
  letter-spacing: -0.02em;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.ov-kpi__value small { color: var(--astra-text-muted); font-size: 13px; font-weight: 500; letter-spacing: 0; }
.ov-kpi__value.is-muted { color: var(--astra-text-muted); }
.ov-kpi__value.is-bad { color: var(--astra-danger); }
.ov-kpi__foot { display: grid; gap: 6px; color: var(--astra-text-muted); font-size: 12px; font-variant-numeric: tabular-nums; }
.ov-kpi__foot b { color: var(--astra-text-secondary); font-weight: 600; }
.ov-kpi__foot b.is-danger { color: var(--astra-danger); }

.ov-ratio-bar {
  display: flex; height: 4px; width: 100%; overflow: hidden;
  border-radius: 999px; background: var(--astra-bg-elevated);
}
.ov-ratio-bar__ok { background: var(--astra-success); }
.ov-ratio-bar__err { background: var(--astra-danger); }
.ov-ratio-bar__in { background: var(--astra-info); }
.ov-ratio-bar__out { background: #3f3f46; }

/* ── 卡片公共框架 ────────────────────────────────────────────────── */
.ov-page :deep(.ov-card) {
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}
.ov-page :deep(.ov-card) > .el-card__header { padding: 13px 16px; border-bottom: 1px solid var(--astra-border-subtle); }
.ov-page :deep(.ov-card) > .el-card__body { padding: 14px 16px; }

.ov-card__head { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px 12px; }
.ov-card__title { min-width: 0; }
.ov-card__title h4 { margin: 0; color: var(--astra-text-primary); font-size: 14px; font-weight: 600; line-height: 20px; }
.ov-card__sub { margin: 2px 0 0; color: var(--astra-text-muted); font-size: 12px; line-height: 16px; }
.ov-card__tools { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.ov-link { color: var(--astra-accent-text); font-size: 12px; font-weight: 500; text-decoration: none; white-space: nowrap; }
.ov-link:hover { color: var(--astra-accent-hover); text-decoration: underline; }
.ov-count-pill {
  padding: 2px 8px; border-radius: 999px;
  background: var(--astra-bg-subtle); border: 1px solid var(--astra-border);
  color: var(--astra-text-secondary); font-size: 12px; font-variant-numeric: tabular-nums;
}

/* ── Layer 3: 趋势图 + 延迟剖析双栏 ─────────────────────────────── */
.ov-main-grid { display: grid; grid-template-columns: minmax(0, 1.75fr) minmax(330px, 1fr); gap: 14px; align-items: stretch; }
.ov-trend-tools { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
.ov-select-sm { width: 102px; }
.ov-select-md { width: 144px; }

.ov-chart-empty {
  position: relative;
  display: flex; align-items: center; justify-content: center;
  height: 248px;
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  border: 1px dashed var(--astra-border);
  overflow: hidden;
}
.ov-chart-empty__grid {
  position: absolute; inset: 24px 20px;
  display: flex; flex-direction: column; justify-content: space-between;
  pointer-events: none;
}
.ov-chart-empty__grid span { display: block; height: 1px; border-top: 1px dashed var(--astra-border); opacity: 0.65; }
.ov-chart-empty__body {
  position: relative; z-index: 1;
  display: grid; gap: 6px; justify-items: center; text-align: center;
  padding: 12px 20px;
  border-radius: 8px;
  background: rgba(255, 255, 255, 0.9);
  border: 1px solid var(--astra-border-subtle);
}
.ov-chart-empty__title { margin: 0; color: var(--astra-text-secondary); font-size: 13px; font-weight: 500; }

/* 延迟剖析 */
.ov-latency-list { display: grid; gap: 11px; }
.ov-latency-row { display: grid; gap: 5px; }
.ov-latency-row__top { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; font-size: 12px; }
.ov-latency-row__label { color: var(--astra-text-secondary); }
.ov-latency-row__val { color: var(--astra-text-primary); font-family: var(--astra-font-mono); font-size: 12px; font-weight: 600; font-variant-numeric: tabular-nums; }
.ov-latency-row__val.is-muted { color: var(--astra-text-muted); font-weight: 400; }
.ov-latency-row__track { height: 6px; border-radius: 999px; background: var(--astra-bg-elevated); overflow: hidden; }
.ov-latency-row__fill { display: block; height: 100%; border-radius: 999px; background: #71717a; transition: width 0.2s ease; }
.ov-latency-row__fill.tone-1 { background: #a1a1aa; }
.ov-latency-row__fill.tone-2 { background: #71717a; }
.ov-latency-row__fill.tone-3 { background: var(--astra-info); }
.ov-latency-row__fill.tone-4 { background: var(--astra-primary); }

.ov-divider { height: 1px; background: var(--astra-border-subtle); margin: 14px 0 12px; }

.ov-stability { display: grid; gap: 8px; }
.ov-stability__head { display: flex; align-items: center; justify-content: space-between; font-size: 12px; color: var(--astra-text-secondary); font-weight: 500; }
.ov-stability__cap { color: var(--astra-text-muted); font-weight: 400; font-variant-numeric: tabular-nums; }
.ov-stability__grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.ov-stability__cell {
  display: grid; gap: 2px;
  padding: 8px 10px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}
.ov-stability__label { color: var(--astra-text-muted); font-size: 11px; }
.ov-stability__val { color: var(--astra-text-primary); font-size: 13px; font-weight: 600; font-variant-numeric: tabular-nums; }
.ov-stability__val.is-warn { color: var(--astra-warning); }

/* ── Layer 4A: 节点运行矩阵 ──────────────────────────────────────── */
.ov-nodes-card { scroll-margin-top: 8px; }
.ov-nodes-card :deep(.el-card__body) { padding-top: 6px; }
.ov-node-cell { display: grid; gap: 2px; min-width: 0; }
.ov-node-link { color: var(--astra-accent-text); font-weight: 600; font-size: 13px; text-decoration: none; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-node-link:hover { color: var(--astra-accent-hover); text-decoration: underline; }
.ov-node-pool { color: var(--astra-text-muted); font-size: 12px; }
.ov-state-tags { display: flex; align-items: center; gap: 5px; flex-wrap: nowrap; }

.ov-load-cell { display: flex; align-items: center; gap: 8px; }
.ov-load-bar { width: 52px; height: 6px; flex: none; border-radius: 999px; background: var(--astra-bg-elevated); overflow: hidden; }
.ov-load-bar i { display: block; height: 100%; border-radius: 999px; background: var(--astra-primary); }
.ov-load-bar i.is-warn { background: var(--astra-warning); }
.ov-load-bar i.is-danger { background: var(--astra-danger); }

.ov-num { font-size: 12.5px; font-variant-numeric: tabular-nums; color: var(--astra-text-secondary); white-space: nowrap; }
.ov-num.is-danger { color: var(--astra-danger); font-weight: 600; }
.ov-mono { font-family: var(--astra-font-mono); font-size: 12px; color: var(--astra-text-primary); }

/* ── Layer 4B: 底部双栏（实例 + 最近动态） ───────────────────────── */
.ov-bottom-grid { display: grid; grid-template-columns: minmax(0, 1.35fr) minmax(320px, 1fr); gap: 14px; align-items: stretch; }
.ov-instances-card { scroll-margin-top: 8px; }
.ov-instances-card :deep(.el-card__body),
.ov-events-card :deep(.el-card__body) { padding-top: 8px; }
.ov-inst-cell { display: grid; gap: 2px; min-width: 0; }
.ov-inst-id { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ov-inst-meta { color: var(--astra-text-muted); font-size: 11.5px; font-variant-numeric: tabular-nums; }
.ov-inst-status { display: flex; align-items: center; gap: 6px; white-space: nowrap; }

.ov-event-list {
  display: grid; margin: 0; padding: 0; list-style: none;
  max-height: 248px; overflow-y: auto;
}
.ov-event-list li { border-bottom: 1px solid var(--astra-border-subtle); }
.ov-event-list li:last-child { border-bottom: none; }
.ov-event-list button {
  display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px;
  width: 100%; padding: 7px 6px;
  border: 0; border-radius: 6px; background: transparent;
  color: var(--astra-text-primary); text-align: left; cursor: pointer;
}
.ov-event-list button:hover { background: var(--astra-bg-subtle); }
.ov-event-summary { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12.5px; }
.ov-event-list time { color: var(--astra-text-muted); font-size: 11px; font-variant-numeric: tabular-nums; white-space: nowrap; }
.ov-muted-empty { margin: 14px 0; color: var(--astra-text-muted); font-size: 13px; text-align: center; }

@media (max-width: 1240px) {
  .ov-kpis { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ov-main-grid, .ov-bottom-grid { grid-template-columns: 1fr; }
}
@media (max-width: 720px) {
  .ov-kpis { grid-template-columns: 1fr; }
}
</style>
