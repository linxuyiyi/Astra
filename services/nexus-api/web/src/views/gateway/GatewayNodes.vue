<template>
  <section class="admin-page" data-testid="gateway-nodes">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-nodes-error">
      <ElButton type="primary" data-testid="gateway-nodes-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-nodes-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="gateway-node-create" @click="openCreate">新增节点</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="节点筛选" data-testid="gateway-nodes-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="filters.health" class="admin-page__select" data-testid="node-filter-health" aria-label="健康状态筛选" placeholder="健康：全部" clearable>
          <ElOption v-for="item in healthOptions" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
        <ElSelect v-model="filters.scheduling" class="admin-page__select" data-testid="node-filter-scheduling" aria-label="调度状态筛选" placeholder="调度：全部" clearable>
          <ElOption v-for="item in schedulingOptions" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
        <ElSelect v-model="filters.config" class="admin-page__select" data-testid="node-filter-config" aria-label="配置状态筛选" placeholder="配置：全部" clearable>
          <ElOption value="ENABLED" label="启用" />
          <ElOption value="DISABLED" label="禁用" />
        </ElSelect>
        <ElSelect v-model="filters.pool" class="admin-page__select" data-testid="node-filter-pool" aria-label="资源池筛选" placeholder="资源池：全部" clearable>
          <ElOption value="__none__" label="未分配" />
          <ElOption v-for="pool in pools" :key="pool.id" :value="pool.id" :label="pool.name" />
        </ElSelect>
        <ElButton v-if="hasFilters" text type="primary" data-testid="gateway-nodes-clear" @click="clearFilters">清除筛选</ElButton>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="filters.keyword"
          class="admin-page__keyword"
          data-testid="node-filter-keyword"
          aria-label="搜索节点"
          placeholder="搜索名称 / 地址 / 模型"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="gateway-nodes-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <GatewayNodeTable
      :nodes="pagedNodes"
      :pools="pools"
      :busy-id="busyId"
      :loading="loading"
      :empty-text="hasFilters ? '没有匹配的节点' : '暂无节点'"
      @detail="openDetail"
      @probe="onProbe"
      @drain="onDrain"
      @restore="onRestore"
      @toggle-config="onToggleConfig"
      @edit="openEdit"
      @delete="onDelete"
    />

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filteredNodes.length"
        layout="total, sizes, prev, pager, next"
        aria-label="节点列表分页"
        data-testid="gateway-nodes-pagination"
      />
    </div>

    <ElCard shadow="never" data-testid="gateway-node-instances">
      <template #header>
        <div class="gateway-nodes__card-head">
          <h4>Gateway 实例</h4>
        </div>
      </template>
      <ElAlert v-if="instanceError" type="warning" show-icon :closable="false" :title="instanceError" />
      <ElTable v-else :data="instances" empty-text="暂无实例心跳。">
        <ElTableColumn label="实例" prop="instance_id" min-width="150" show-overflow-tooltip />
        <ElTableColumn label="状态" width="90">
          <template #default="{ row }"><ElTag :type="row.status === 'ACTIVE' ? 'success' : 'info'">{{ row.status === 'ACTIVE' ? '在线' : '已过期' }}</ElTag></template>
        </ElTableColumn>
        <ElTableColumn label="应用版本" min-width="110">
          <template #default="{ row }">{{ row.app_version ?? '—' }}</template>
        </ElTableColumn>
        <ElTableColumn label="运行时长" width="100">
          <template #default="{ row }">{{ formatSeconds(row.uptime_seconds) }}</template>
        </ElTableColumn>
        <ElTableColumn label="活跃" width="72">
          <template #default="{ row }">{{ row.active ?? '—' }}</template>
        </ElTableColumn>
        <ElTableColumn label="请求 / 错误" width="120">
          <template #default="{ row }">{{ row.requests ?? '—' }} / {{ row.errors ?? '—' }}</template>
        </ElTableColumn>
        <ElTableColumn label="CPU" width="80">
          <template #default="{ row }">{{ row.cpu_percent !== null && row.cpu_percent !== undefined ? `${row.cpu_percent.toFixed(1)}%` : '—' }}</template>
        </ElTableColumn>
        <ElTableColumn label="内存" width="90">
          <template #default="{ row }">{{ formatBytes(row.memory_bytes) }}</template>
        </ElTableColumn>
        <ElTableColumn label="心跳" width="120">
          <template #default="{ row }">{{ formatRelativeTime(row.updated_at) }}</template>
        </ElTableColumn>
      </ElTable>
    </ElCard>

    <GatewayNodeDrawer :open="drawerOpen" :node="drawerNode" :pools="pools" @close="drawerOpen = false" @saved="load" />
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElAlert, ElButton, ElCard, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTag } from 'element-plus'
import {
  gatewayApi,
  type GatewayInstance,
  type GatewayNode,
  type GatewayPool,
} from '@/api/gateway'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { errorMessage, formatBytes, formatRelativeTime, formatSeconds, healthStateLabel, schedulingStateLabel } from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import GatewayNodeDrawer from '@/views/gateway/GatewayNodeDrawer.vue'
import GatewayNodeTable from '@/views/gateway/GatewayNodeTable.vue'

const router = useRouter()
const nodes = ref<GatewayNode[]>([])
const pools = ref<GatewayPool[]>([])
const instances = ref<GatewayInstance[]>([])
const loading = ref(false)
const error = ref('')
const instanceError = ref('')
const busyId = ref('')
const drawerOpen = ref(false)
const drawerNode = ref<GatewayNode | null>(null)

const healthOptions = (['HEALTHY', 'DEGRADED', 'UNHEALTHY', 'CHECKING', 'UNKNOWN'] as const)
  .map((value) => ({ value, label: healthStateLabel(value) }))
const schedulingOptions = (['ACTIVE', 'DRAINING', 'BLOCKED'] as const)
  .map((value) => ({ value, label: schedulingStateLabel(value) }))

const filters = reactive({ keyword: '', health: '', scheduling: '', config: '', pool: '' })
const page = ref(1)
const pageSize = ref(20)

const filteredNodes = computed(() => {
  const keyword = filters.keyword.trim().toLowerCase()
  return nodes.value.filter((node) => {
    if (keyword && !`${node.name} ${node.base_url} ${node.models.join(' ')}`.toLowerCase().includes(keyword)) return false
    if (filters.health && node.health_state !== filters.health) return false
    if (filters.scheduling && node.scheduling_state !== filters.scheduling) return false
    if (filters.config && node.config_state !== filters.config) return false
    if (filters.pool && (node.pool_id ?? '__none__') !== filters.pool) return false
    return true
  })
})
const pagedNodes = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredNodes.value.slice(start, start + pageSize.value)
})
const hasFilters = computed(() => Boolean(
  filters.keyword.trim() || filters.health || filters.scheduling || filters.config || filters.pool,
))

watch(filteredNodes, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})
watch(filters, () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })

function applySearch() {
  page.value = 1
  void load()
}

function clearFilters() {
  filters.keyword = ''
  filters.health = ''
  filters.scheduling = ''
  filters.config = ''
  filters.pool = ''
}

async function load() {
  loading.value = true
  error.value = ''
  instanceError.value = ''
  try {
    const [nodeList, poolList] = await Promise.all([gatewayApi.nodes(), gatewayApi.pools()])
    nodes.value = nodeList
    pools.value = poolList
  } catch (reason) {
    error.value = errorMessage(reason, '节点列表加载失败')
  }
  try {
    instances.value = await gatewayApi.instances()
  } catch (reason) {
    instances.value = []
    instanceError.value = errorMessage(reason, '实例心跳加载失败')
  } finally {
    loading.value = false
  }
}

function openCreate() {
  drawerNode.value = null
  drawerOpen.value = true
}

function openEdit(node: GatewayNode) {
  drawerNode.value = node
  drawerOpen.value = true
}

function openDetail(node: GatewayNode) {
  router.push(`/gateway/nodes/${node.id}`)
}

async function onProbe(node: GatewayNode) {
  busyId.value = node.id
  try {
    const result = await gatewayApi.probeNode(node.id, [])
    const passed = result.checks.filter((check) => check.success).length
    notify(`${node.name} 探测完成：${passed}/${result.checks.length} 项通过`, passed === result.checks.length ? 'success' : 'info')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '探测失败'), 'error')
  } finally {
    busyId.value = ''
  }
}

async function onDrain(node: GatewayNode) {
  const confirmed = await confirmDialog({ title: '安全下线节点', message: `确认让「${node.name}」进入 DRAINING？活跃请求会继续完成，新请求改走其它节点。`, confirmLabel: '安全下线', danger: true })
  if (!confirmed) return
  busyId.value = node.id
  try {
    await gatewayApi.drainNode(node.id)
    notify('节点已进入安全下线')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '下线失败'), 'error')
  } finally {
    busyId.value = ''
  }
}

async function onRestore(node: GatewayNode) {
  busyId.value = node.id
  try {
    await gatewayApi.activateNode(node.id)
    notify('节点已恢复调度')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '恢复失败'), 'error')
  } finally {
    busyId.value = ''
  }
}

async function onToggleConfig(node: GatewayNode) {
  const enable = node.config_state !== 'ENABLED'
  busyId.value = node.id
  try {
    await gatewayApi.updateNode(node.id, { config_state: enable ? 'ENABLED' : 'DISABLED' })
    notify(enable ? '节点已启用' : '节点已禁用')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '状态更新失败'), 'error')
  } finally {
    busyId.value = ''
  }
}

async function onDelete(node: GatewayNode) {
  const confirmed = await confirmDialog({ title: '删除节点', message: `确认删除「${node.name}」？仅当节点已 DRAINING 且活跃并发为 0 时才允许删除。`, confirmLabel: '删除节点', danger: true })
  if (!confirmed) return
  busyId.value = node.id
  try {
    await gatewayApi.deleteNode(node.id)
    notify('节点已删除')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '删除失败'), 'error')
  } finally {
    busyId.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.gateway-nodes__card-head { display: grid; gap: 2px; }
.gateway-nodes__card-head h4 { margin: 0; color: var(--astra-text-primary); }
</style>
