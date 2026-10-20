<template>
  <section class="admin-page" data-testid="gateway-requests">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-requests-error">
      <ElButton type="primary" data-testid="gateway-requests-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-requests-refresh" @click="load">刷新</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="调用日志筛选" data-testid="gateway-requests-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="filters.model" class="admin-page__select" data-testid="log-filter-model" aria-label="模型筛选" placeholder="模型：全部" clearable>
          <ElOption v-for="model in models" :key="model" :value="model" :label="model" />
        </ElSelect>
        <ElSelect v-model="filters.node_id" class="admin-page__select" data-testid="log-filter-node" aria-label="节点筛选" placeholder="节点：全部" clearable>
          <ElOption v-for="node in nodes" :key="node.id" :value="node.id" :label="node.name" />
        </ElSelect>
        <ElDatePicker
          v-model="timeRange"
          class="log-filter__range"
          type="datetimerange"
          data-testid="log-filter-range"
          aria-label="时间范围"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
        />
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="filters.request_id"
          class="admin-page__keyword"
          data-testid="log-filter-request"
          aria-label="Request ID 筛选"
          placeholder="请求编号精确匹配"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="apply"
        />
        <ElButton type="primary" :icon="Search" :disabled="loading" data-testid="gateway-requests-apply" @click="apply">搜索</ElButton>
        <ElButton :disabled="loading" data-testid="gateway-requests-reset" @click="reset">重置</ElButton>
      </div>
    </div>

    <ElTable
      v-loading="loading"
      :data="page.items"
      row-key="id"
      element-loading-text="正在加载调用日志"
      data-testid="gateway-requests-table"
    >
      <ElTableColumn type="expand" width="42">
        <template #default="{ row }">
          <div class="log-detail-grid">
            <div class="log-detail-item">
              <span class="log-detail-label">请求 ID</span>
              <span class="log-detail-value mono">{{ row.id }}</span>
            </div>
            <div v-if="row.actual_model && row.actual_model !== row.model" class="log-detail-item">
              <span class="log-detail-label">透明路由</span>
              <span class="log-detail-value highlight">{{ row.model }} → {{ row.actual_model }}</span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">模式与密钥</span>
              <span class="log-detail-value">
                {{ row.streaming ? '流式传输' : '常规响应' }}{{ row.stream_interrupted ? '（异常中断）' : '' }} · Key: {{ keyName(row.key_id) }} · 重试 {{ row.retry_count }} 次
              </span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">Token 与速率</span>
              <span class="log-detail-value">
                {{ tokenText(row as GatewayRequestLog) }}
                <template v-if="row.output_tps !== null"> · {{ row.output_tps.toFixed(1) }} token/s</template>
              </span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">阶段延迟</span>
              <span class="log-detail-value">
                首字节 {{ latencyText(row.ttfb_ms) }} · 首 Token {{ latencyText(row.ttft_ms) }} · 总时长 {{ latencyText(row.duration_ms) }}
              </span>
            </div>
            <div v-if="row.error_code" class="log-detail-item log-detail-item--error">
              <span class="log-detail-label">错误原因</span>
              <span class="log-detail-value">{{ row.error_code }} ({{ errorCause(row.error_code) }})</span>
            </div>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="时间" width="148">
        <template #default="{ row }"><span class="log-num">{{ formatDateTime(row.started_at) }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="模型" min-width="170">
        <template #default="{ row }">
          <div class="log-model-cell">
            <span>{{ row.model ?? '—' }}</span>
            <ElTag v-if="row.actual_model && row.actual_model !== row.model" size="small" type="info" class="log-routed-tag">
              → {{ row.actual_model }}
            </ElTag>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="节点" min-width="120" show-overflow-tooltip>
        <template #default="{ row }">{{ nodeName(row.node_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="结果" min-width="160">
        <template #default="{ row }">
          <ElTag v-if="row.outcome === 'lost'" type="warning" data-testid="gateway-log-lost">结果未知</ElTag>
          <ElTag v-else-if="!row.finished_at && row.gateway_instance_id" type="info" data-testid="gateway-log-inflight">进行中</ElTag>
          <ElTag v-else-if="!row.finished_at" type="info" data-testid="gateway-log-legacy">历史未完成</ElTag>
          <template v-else-if="row.error_code">
            <ElTag type="danger">{{ errorCause(row.error_code) }}</ElTag>
          </template>
          <ElTag v-else :type="row.status_code && row.status_code < 400 ? 'success' : 'warning'">成功 {{ row.status_code ?? '' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="时长" width="96">
        <template #default="{ row }"><span class="log-num">{{ latencyText(row.duration_ms) }}</span></template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="emptyText" />
      </template>
    </ElTable>

    <div class="admin-page__footer">
      <ElPagination
        :current-page="page.page"
        :page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="page.total"
        layout="total, sizes, prev, pager, next"
        :disabled="loading"
        aria-label="调用日志分页"
        data-testid="gateway-requests-pagination"
        @current-change="go"
        @size-change="changeSize"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElDatePicker,
  ElEmpty,
  ElInput,
  ElOption,
  ElPagination,
  ElSelect,
  ElTable,
  ElTableColumn,
  ElTag,
  vLoading,
} from 'element-plus'
import {
  gatewayApi,
  type GatewayKey,
  type GatewayNode,
  type GatewayRequestLog,
  type GatewayRequestPage,
  type GatewayRequestQuery,
} from '@/api/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import { errorCause, errorMessage, formatDateTime } from '@/utils/gateway'

const route = useRoute()
const page = ref<GatewayRequestPage>({ items: [], total: 0, page: 1, page_size: 20 })
const nodes = ref<GatewayNode[]>([])
const keys = ref<GatewayKey[]>([])
const models = ref<string[]>([])
const loading = ref(false)
const error = ref('')
const pageSize = ref(20)
const filters = reactive({ request_id: '', model: '', node_id: '' })
const timeRange = ref<[Date, Date] | null>(null)
let resettingPageSize = false

const hasFilters = computed(() =>
  Boolean(
    filters.request_id.trim()
    || filters.model
    || filters.node_id
    || timeRange.value,
  ),
)
const emptyText = computed(() => (hasFilters.value
  ? '没有匹配的调用记录'
  : '暂无调用记录'))

function keyName(id: string | null): string {
  if (!id) return '—'
  return keys.value.find((key) => key.id === id)?.name ?? id.slice(0, 12)
}
function nodeName(id: string | null): string {
  if (!id) return '—'
  return nodes.value.find((node) => node.id === id)?.name ?? id.slice(0, 12)
}
function tokenText(row: GatewayRequestLog): string {
  if (!row.usage_known) return 'unknown'
  return `${row.input_tokens ?? 0} / ${row.output_tokens ?? 0} / ${row.total_tokens ?? 0}`
}
function latencyText(value: number | null): string {
  return value === null || value === undefined ? '—' : `${value} ms`
}

function buildQuery(target: number): GatewayRequestQuery {
  return {
    page: target,
    page_size: pageSize.value,
    request_id: filters.request_id.trim() || undefined,
    model: filters.model || undefined,
    node_id: filters.node_id || undefined,
    from: timeRange.value ? timeRange.value[0].toISOString() : undefined,
    to: timeRange.value ? timeRange.value[1].toISOString() : undefined,
  }
}

async function loadPage(target = page.value.page) {
  loading.value = true
  error.value = ''
  try {
    page.value = await gatewayApi.requests(buildQuery(target))
  } catch (reason) {
    error.value = errorMessage(reason, '调用日志加载失败')
  } finally {
    loading.value = false
  }
}

function load() { void loadPage(page.value.page) }
function apply() { void loadPage(1) }
function go(target: number) {
  if (resettingPageSize) return
  void loadPage(target)
}
function changeSize(size: number) {
  pageSize.value = size
  page.value = { ...page.value, page: 1, page_size: size }
  // 切换每页条数统一从第 1 页重新请求；Element Plus 在页码越界时还会补发
  // current-change，这里用标记挡住，避免同一操作触发两次请求并停在第 2 页。
  resettingPageSize = true
  void loadPage(1).finally(() => { resettingPageSize = false })
}
function reset() {
  Object.assign(filters, { request_id: '', model: '', node_id: '' })
  timeRange.value = null
  void loadPage(1)
}

function readQuery() {
  const text = (key: string) => (typeof route.query[key] === 'string' ? String(route.query[key]) : '')
  filters.node_id = text('node_id')
  filters.model = text('model')
  const from = text('from')
  const to = text('to')
  const start = from ? new Date(from) : null
  const end = to ? new Date(to) : null
  timeRange.value = start && end && !Number.isNaN(start.getTime()) && !Number.isNaN(end.getTime()) ? [start, end] : null
}

async function loadFilterOptions() {
  try {
    const [nodeList, keyList, routeList] = await Promise.all([
      gatewayApi.nodes(), gatewayApi.keys(), gatewayApi.routes(),
    ])
    nodes.value = nodeList
    keys.value = keyList
    models.value = routeList.map((item) => item.model)
  } catch {
    /* 过滤选项加载失败不影响日志本身 */
  }
}

watch(() => route.query, () => {
  readQuery()
  void loadPage(1)
})
onMounted(() => {
  readQuery()
  void loadPage(1)
  void loadFilterOptions()
})
</script>

<style scoped>
.log-filter__range { flex: none; --el-date-editor-datetimerange-width: 300px; }
.log-num { white-space: nowrap; font-size: var(--astra-font-helper); font-variant-numeric: tabular-nums; }
.log-detail-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px 16px; padding: 10px 14px; background: var(--astra-bg-elevated); border-radius: 8px; font-size: var(--astra-font-helper); }
.log-detail-item { display: flex; flex-direction: column; gap: 2px; }
.log-detail-label { color: var(--astra-text-muted); font-size: 11px; }
.log-detail-value { color: var(--astra-text-primary); }
.log-detail-value.mono { font-family: var(--astra-font-mono, monospace); font-size: 12px; }
.log-detail-value.highlight { color: var(--astra-primary); font-weight: var(--astra-weight-bold); }
.log-detail-item--error .log-detail-value { color: var(--astra-danger); }
.log-model-cell { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
.log-routed-tag { font-size: 11px; padding: 0 6px; height: 20px; line-height: 18px; }
.log-interrupted { margin-left: 6px; }
</style>
