<template>
  <section class="admin-page admin-page--fill gateway-audits" data-testid="gateway-audits">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error">
      <ElButton type="primary" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-audits-refresh" @click="load">刷新</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="审计筛选" data-testid="gateway-audits-filter">
      <div class="admin-page__filters">
        <ElSelect
          v-model="resourceType"
          class="admin-page__select"
          data-testid="audit-filter-resource"
          aria-label="资源类型筛选"
          placeholder="资源类型：全部"
          clearable
        >
          <ElOption v-for="item in resourceTypeOptions" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="keyword"
          class="admin-page__keyword"
          data-testid="audit-filter-keyword"
          aria-label="搜索审计"
          placeholder="搜索操作 / 对象 / 操作者"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="gateway-audits-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="pagedRows"
        row-key="id"
        height="100%"
        data-testid="gateway-audits-table"
      >
      <ElTableColumn type="expand" width="40">
        <template #default="{ row }">
          <div class="audit-diff">
            <div class="audit-diff__col">
              <div class="audit-diff__title">变更前（Before）</div>
              <pre v-if="hasSnapshot(row.before)" class="audit-diff__pre">{{ formatJson(row.before) }}</pre>
              <p v-else class="audit-diff__empty">无变更前快照</p>
            </div>
            <div class="audit-diff__col">
              <div class="audit-diff__title">变更后（After）</div>
              <pre v-if="hasSnapshot(row.after)" class="audit-diff__pre">{{ formatJson(row.after) }}</pre>
              <p v-else class="audit-diff__empty">无变更后快照</p>
            </div>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="时间" width="150">
        <template #default="{ row }"><span class="audit-num">{{ formatDateTime(row.created_at) }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="操作" min-width="148">
        <template #default="{ row }">
          <span class="audit-action" :title="row.action">{{ auditActionLabel(row.action) }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="对象" min-width="230">
        <template #default="{ row }">
          <div class="audit-target">
            <ElTag size="small" type="info" class="astra-tag--neutral">{{ auditResourceTypeLabel(row.resource_type) }}</ElTag>
            <span v-if="targetDisplayName(row as GatewayAuditEvent)" class="audit-target__name" :title="targetDisplayName(row as GatewayAuditEvent)">
              {{ targetDisplayName(row as GatewayAuditEvent) }}
            </span>
            <span v-if="row.resource_id" class="audit-target__id" :title="row.resource_id">{{ shortId(row.resource_id) }}</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="结果" width="96" align="center">
        <template #default="{ row }">
          <ElTag
            size="small"
            :type="resultTagType(row.result)"
            :class="{ 'astra-tag--neutral': resultTagType(row.result) === 'info' }"
          >{{ auditResultLabel(row.result) }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作者" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="audit-actor">{{ row.actor || '系统' }}</span>
          <span v-if="row.actor_role" class="audit-role"> · {{ row.actor_role }}</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="96" align="center" fixed="right">
        <template #default="{ row }">
          <ElButton link type="primary" @click="openLogs(row as GatewayAuditEvent)">关联日志</ElButton>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="emptyText" />
      </template>
    </ElTable>
    </div>

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filteredRows.length"
        layout="total, sizes, prev, pager, next"
        aria-label="审计分页"
        data-testid="gateway-audits-pagination"
      />
    </div>
  </section>
</template>

<script setup lang="ts">
import { RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElAlert, ElButton, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTag, vLoading } from 'element-plus'
import type { TableInstance } from 'element-plus'
import { gatewayApi, type GatewayAuditEvent } from '@/api/gateway'
import {
  auditActionLabel,
  auditResourceTypeLabel,
  auditResultLabel,
  errorMessage,
  formatDateTime,
} from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'

const KNOWN_RESOURCE_TYPES = ['node', 'pool', 'key', 'route']
const resourceTypeOptions = [
  { value: 'node', label: '上游节点' },
  { value: 'pool', label: '资源池' },
  { value: 'route', label: '模型路由' },
  { value: 'key', label: 'API Key' },
  { value: 'other', label: '其他' },
]

const router = useRouter()
const rows = ref<GatewayAuditEvent[]>([])
const loading = ref(false)
const error = ref('')
const keyword = ref('')
const resourceType = ref('')
const page = ref(1)
const pageSize = ref(20)
const tableRef = ref<TableInstance>()

function targetDisplayName(row: GatewayAuditEvent): string {
  const after = row.after ?? {}
  const before = row.before ?? {}
  const candidate = after.name ?? before.name ?? after.model ?? before.model
  return typeof candidate === 'string' ? candidate : ''
}

function shortId(id: string): string {
  return id.length > 14 ? `${id.slice(0, 12)}…` : id
}

function hasSnapshot(value: Record<string, unknown> | null | undefined): boolean {
  return Boolean(value && Object.keys(value).length > 0)
}

function formatJson(value: Record<string, unknown>): string {
  return JSON.stringify(value, null, 2)
}

function resultTagType(result: string): 'success' | 'danger' | 'info' {
  const upper = result.toUpperCase()
  if (upper === 'SUCCESS' || upper === 'OK') return 'success'
  if (upper === 'FAILED' || upper === 'ERROR' || upper === 'DENIED') return 'danger'
  return 'info'
}

const filteredRows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (resourceType.value === 'other') {
      if (KNOWN_RESOURCE_TYPES.includes(row.resource_type)) return false
    } else if (resourceType.value && row.resource_type !== resourceType.value) {
      return false
    }
    if (!needle) return true
    const searchable = [
      row.action,
      auditActionLabel(row.action),
      row.resource_type,
      auditResourceTypeLabel(row.resource_type),
      targetDisplayName(row),
      row.resource_id ?? '',
      row.actor,
      row.actor_role ?? '',
    ].join(' ').toLowerCase()
    return searchable.includes(needle)
  })
})

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredRows.value.slice(start, start + pageSize.value)
})

const emptyText = computed(() => (
  keyword.value.trim() || resourceType.value
    ? '没有匹配的审计事件'
    : '暂无配置审计记录'
))

watch([keyword, resourceType], () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })
watch([page, pageSize], () => { tableRef.value?.setScrollTop(0) })
watch(filteredRows, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})

async function load() {
  loading.value = true
  error.value = ''
  try {
    rows.value = await gatewayApi.audits({ limit: 200 })
  } catch (reason) {
    error.value = errorMessage(reason, '审计加载失败')
  } finally {
    loading.value = false
  }
}

function applySearch() {
  page.value = 1
  void load()
}

function openLogs(row: GatewayAuditEvent) {
  const at = new Date(row.created_at).getTime()
  const query: Record<string, string> = {
    from: new Date(at - 5 * 60 * 1000).toISOString(),
    to: new Date(at + 5 * 60 * 1000).toISOString(),
  }
  if (row.resource_type === 'node' && row.resource_id) query.node_id = row.resource_id
  if (row.resource_type === 'key' && row.resource_id) query.key_id = row.resource_id
  const model = row.after.model ?? row.before.model
  if (typeof model === 'string') query.model = model
  void router.push({ path: '/gateway/logs', query })
}

onMounted(() => { void load() })
</script>

<style scoped>
.audit-num { white-space: nowrap; font-size: 13px; font-variant-numeric: tabular-nums; color: var(--astra-text-secondary); }
.audit-action { font-weight: 600; color: var(--astra-text-primary); font-size: 13px; }
.audit-target { display: flex; align-items: center; gap: 6px; min-width: 0; flex-wrap: nowrap; }
.audit-target__name { color: var(--astra-text-primary); font-size: 13px; font-weight: 500; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.audit-target__id { flex: none; color: var(--astra-text-muted); font-family: var(--astra-font-mono); font-size: 12px; white-space: nowrap; }
.audit-actor { color: var(--astra-text-primary); font-size: 13px; }
.audit-role { color: var(--astra-text-muted); font-size: 12px; }

.audit-diff {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px;
  padding: 12px 16px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}
.audit-diff__col { display: grid; gap: 6px; min-width: 0; align-content: start; }
.audit-diff__title { color: var(--astra-text-muted); font-size: 12px; font-weight: 600; }
.audit-diff__pre {
  margin: 0;
  padding: 10px 12px;
  max-height: 220px;
  overflow: auto;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}
.audit-diff__empty { margin: 0; color: var(--astra-text-muted); font-size: 12px; }

@media (max-width: 760px) {
  .audit-diff { grid-template-columns: 1fr; }
}
</style>
