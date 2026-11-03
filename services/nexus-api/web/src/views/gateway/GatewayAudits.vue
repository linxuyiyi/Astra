<template>
  <section class="admin-page gateway-audits" data-testid="gateway-audits">
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

    <ElTable
      v-loading="loading"
      :data="pagedRows"
      row-key="id"
      data-testid="gateway-audits-table"
      @row-click="openLogs"
    >
      <ElTableColumn label="时间" width="168">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </ElTableColumn>
      <ElTableColumn label="来源" width="88">
        <template #default><ElTag type="info">配置</ElTag></template>
      </ElTableColumn>
      <ElTableColumn label="操作" prop="action" min-width="140" show-overflow-tooltip />
      <ElTableColumn label="对象" min-width="180" show-overflow-tooltip>
        <template #default="{ row }">{{ row.resource_type }} {{ row.resource_id ?? '' }}</template>
      </ElTableColumn>
      <ElTableColumn label="结果" width="100" prop="result" />
      <ElTableColumn label="操作者" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ row.actor }}<span v-if="row.actor_role"> · {{ row.actor_role }}</span></template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="emptyText" />
      </template>
    </ElTable>

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
import { gatewayApi, type GatewayAuditEvent } from '@/api/gateway'
import { errorMessage, formatDateTime } from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'

const KNOWN_RESOURCE_TYPES = ['node', 'pool', 'key', 'route']
const resourceTypeOptions = [
  { value: 'node', label: '节点' },
  { value: 'pool', label: '资源池' },
  { value: 'key', label: 'Key' },
  { value: 'route', label: '路由' },
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

const filteredRows = computed(() => {
  const needle = keyword.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (resourceType.value === 'other') {
      if (KNOWN_RESOURCE_TYPES.includes(row.resource_type)) return false
    } else if (resourceType.value && row.resource_type !== resourceType.value) {
      return false
    }
    if (!needle) return true
    return `${row.action} ${row.resource_type} ${row.resource_id ?? ''} ${row.actor} ${row.actor_role ?? ''}`
      .toLowerCase()
      .includes(needle)
  })
})

const pagedRows = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredRows.value.slice(start, start + pageSize.value)
})

const emptyText = computed(() => (
  keyword.value.trim() || resourceType.value
    ? '没有匹配的审计事件'
    : '暂无配置审计'
))

watch([keyword, resourceType], () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })
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
  const model = row.after.model ?? row.before.model
  if (typeof model === 'string') query.model = model
  void router.push({ path: '/gateway/logs', query })
}

onMounted(() => { void load() })
</script>

<style scoped>
.gateway-audits :deep(.el-table__row) { cursor: pointer; }
</style>
