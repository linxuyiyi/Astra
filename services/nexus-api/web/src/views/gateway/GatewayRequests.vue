<template>
  <section class="admin-page admin-page--fill" data-testid="gateway-requests">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-requests-error">
      <ElButton type="primary" data-testid="gateway-requests-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-requests-refresh" @click="load">刷新</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar log-filter" role="search" aria-label="调用日志筛选" data-testid="gateway-requests-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="filters.model" class="admin-page__select" data-testid="log-filter-model" aria-label="模型筛选" placeholder="模型：全部" clearable>
          <ElOption v-for="model in models" :key="model" :value="model" :label="model" />
        </ElSelect>
        <ElSelect v-model="filters.node_id" class="admin-page__select" data-testid="log-filter-node" aria-label="节点筛选" placeholder="节点：全部" clearable>
          <ElOption v-for="node in nodes" :key="node.id" :value="node.id" :label="node.name" />
        </ElSelect>
        <ElSelect v-model="filters.key_id" class="admin-page__select" data-testid="log-filter-key" aria-label="Key 筛选" placeholder="密钥：全部" clearable>
          <ElOption v-for="key in keys" :key="key.id" :value="key.id" :label="key.name" />
        </ElSelect>
        <ElSelect v-model="filters.status_code" class="log-filter__status" data-testid="log-filter-status" aria-label="状态码筛选" placeholder="状态码" clearable>
          <ElOption :value="200" label="200 成功" />
          <ElOption :value="400" label="400 请求无效" />
          <ElOption :value="401" label="401 鉴权失败" />
          <ElOption :value="403" label="403 无权限" />
          <ElOption :value="429" label="429 限流" />
          <ElOption :value="500" label="500 服务端错误" />
          <ElOption :value="502" label="502 上游错误" />
          <ElOption :value="504" label="504 上游超时" />
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
          placeholder="请求编号"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="apply"
        />
        <ElButton type="primary" :icon="Search" :disabled="loading" data-testid="gateway-requests-apply" @click="apply">搜索</ElButton>
        <ElButton :disabled="loading" data-testid="gateway-requests-reset" @click="reset">重置</ElButton>
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="page.items"
        row-key="id"
        height="100%"
        element-loading-text="正在加载调用日志"
        data-testid="gateway-requests-table"
      >
      <ElTableColumn type="expand" width="40">
        <template #default="{ row }">
          <div class="log-detail-grid">
            <div class="log-detail-item">
              <span class="log-detail-label">请求 ID</span>
              <span class="log-detail-value mono">{{ row.id }}</span>
            </div>
            <div v-if="row.actual_model && row.actual_model !== row.model" class="log-detail-item">
              <span class="log-detail-label">模型映射</span>
              <span class="log-detail-value mono">{{ row.model }} → {{ row.actual_model }}</span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">模式与密钥</span>
              <span class="log-detail-value">
                {{ row.streaming ? '流式（SSE）' : '常规响应' }}{{ row.stream_interrupted ? '（异常中断）' : '' }} · Key：{{ keyName(row.key_id) }} · 重试 {{ row.retry_count }} 次
              </span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">Token 与输出速度</span>
              <span class="log-detail-value mono">
                {{ tokenDetailText(row as GatewayRequestLog) }}
                <template v-if="row.output_tps !== null"> · {{ row.output_tps.toFixed(1) }} token/s</template>
              </span>
            </div>
            <div class="log-detail-item">
              <span class="log-detail-label">阶段延迟</span>
              <span class="log-detail-value mono">
                连接 {{ latencyText(row.connect_ms) }} · 首字节 {{ latencyText(row.ttfb_ms) }} · 首 Token {{ latencyText(row.ttft_ms) }} · 总时长 {{ latencyText(row.duration_ms) }}
              </span>
            </div>
            <div v-if="row.error_code" class="log-detail-item log-detail-item--error">
              <span class="log-detail-label">错误原因</span>
              <span class="log-detail-value">{{ errorCause(row.error_code) }}（<span class="mono">{{ row.error_code }}</span>）</span>
            </div>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="时间" width="144">
        <template #default="{ row }"><span class="log-num">{{ formatDateTime(row.started_at) }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="模型" min-width="164">
        <template #default="{ row }">
          <div class="log-model-cell">
            <span class="log-model-name" :title="row.model ?? undefined">{{ row.model ?? '—' }}</span>
            <ElTag
              v-if="row.actual_model && row.actual_model !== row.model"
              size="small"
              type="info"
              class="astra-tag--neutral log-routed-tag"
              :title="row.actual_model"
            >→ {{ row.actual_model }}</ElTag>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="节点" min-width="164" show-overflow-tooltip>
        <template #default="{ row }">{{ nodeName(row.node_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="结果" min-width="132">
        <template #default="{ row }">
          <ElTag v-if="row.outcome === 'lost'" type="warning" data-testid="gateway-log-lost">结果未知</ElTag>
          <ElTag v-else-if="!row.finished_at && row.gateway_instance_id" type="info" data-testid="gateway-log-inflight">进行中</ElTag>
          <ElTag v-else-if="!row.finished_at" type="info" class="astra-tag--neutral" data-testid="gateway-log-legacy">历史未完成</ElTag>
          <ElTag v-else-if="row.error_code" type="danger" :title="row.error_code">{{ errorCause(row.error_code) }}</ElTag>
          <ElTag v-else :type="row.status_code && row.status_code < 400 ? 'success' : 'warning'">
            {{ row.status_code && row.status_code < 400 ? `成功 ${row.status_code}` : (row.status_code ?? '—') }}
          </ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="Token（入 / 出）" width="132" align="right">
        <template #default="{ row }">
          <span class="log-num" :class="{ 'log-muted': !row.usage_known }">{{ tokenBriefText(row as GatewayRequestLog) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="TTFT" width="88" align="right">
        <template #default="{ row }"><span class="log-num">{{ latencyText(row.ttft_ms) }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="总时长" width="92" align="right">
        <template #default="{ row }"><span class="log-num">{{ latencyText(row.duration_ms) }}</span></template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="emptyText" />
      </template>
    </ElTable>
    </div>

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
import type { TableInstance } from 'element-plus'
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
const tableRef = ref<TableInstance>()
const filters = reactive({
  request_id: '',
  model: '',
  node_id: '',
  key_id: '',
  status_code: undefined as number | undefined,
})
const timeRange = ref<[Date, Date] | null>(null)
let resettingPageSize = false

const hasFilters = computed(() =>
  Boolean(
    filters.request_id.trim()
    || filters.model
    || filters.node_id
    || filters.key_id
    || filters.status_code !== undefined
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
function tokenBriefText(row: GatewayRequestLog): string {
  if (!row.usage_known) return '—'
  return `${row.input_tokens ?? 0} / ${row.output_tokens ?? 0}`
}
function tokenDetailText(row: GatewayRequestLog): string {
  if (!row.usage_known) return '未知'
  return `入 ${row.input_tokens ?? 0} / 出 ${row.output_tokens ?? 0} / 总 ${row.total_tokens ?? 0}`
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
    key_id: filters.key_id || undefined,
    status_code: filters.status_code,
    from: timeRange.value ? timeRange.value[0].toISOString() : undefined,
    to: timeRange.value ? timeRange.value[1].toISOString() : undefined,
  }
}

async function loadPage(target = page.value.page) {
  loading.value = true
  error.value = ''
  try {
    page.value = await gatewayApi.requests(buildQuery(target))
    tableRef.value?.setScrollTop(0)
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
  resettingPageSize = true
  void loadPage(1).finally(() => { resettingPageSize = false })
}
function reset() {
  Object.assign(filters, { request_id: '', model: '', node_id: '', key_id: '', status_code: undefined })
  timeRange.value = null
  void loadPage(1)
}

function readQuery() {
  const text = (key: string) => (typeof route.query[key] === 'string' ? String(route.query[key]) : '')
  filters.node_id = text('node_id')
  filters.model = text('model')
  filters.key_id = text('key_id')
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
.log-filter .admin-page__select { width: 128px; }
.log-filter__status { width: 112px; }
.log-filter .admin-page__keyword { width: 172px; }
.log-filter__range { flex: none; --el-date-editor-datetimerange-width: 268px; }

.log-num { white-space: nowrap; font-size: 13px; font-variant-numeric: tabular-nums; color: var(--astra-text-secondary); }
.log-muted { color: var(--astra-text-muted); }

.log-detail-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px 18px;
  padding: 12px 16px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  font-size: 13px;
}
.log-detail-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.log-detail-label { color: var(--astra-text-muted); font-size: 12px; }
.log-detail-value { color: var(--astra-text-primary); word-break: break-word; }
.log-detail-value.mono, .mono { font-family: var(--astra-font-mono); font-size: 12px; }
.log-detail-item--error .log-detail-value { color: var(--astra-danger); }

.log-model-cell { display: flex; flex-direction: column; align-items: flex-start; gap: 3px; min-width: 0; }
.log-model-name {
  max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-family: var(--astra-font-mono); font-size: 12px; font-weight: 500; color: var(--astra-text-primary);
}
.log-routed-tag { max-width: 100%; font-family: var(--astra-font-mono); font-size: 11px; padding: 0 6px; height: 20px; line-height: 18px; }
.log-routed-tag :deep(.el-tag__content) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
</style>
