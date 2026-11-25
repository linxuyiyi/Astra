<template>
  <section class="admin-page admin-page--fill" data-testid="gateway-nodes">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-nodes-error">
      <ElButton type="primary" data-testid="gateway-nodes-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-nodes-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="gateway-node-create" @click="openCreate">新增节点</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="节点筛选" data-testid="gateway-nodes-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="filters.pool" class="admin-page__select" data-testid="node-filter-pool" aria-label="资源池筛选" placeholder="资源池：全部" clearable>
          <ElOption value="__none__" label="未分配" />
          <ElOption v-for="pool in pools" :key="pool.id" :value="pool.id" :label="pool.name" />
        </ElSelect>
        <ElSelect v-model="filters.health" class="admin-page__select" data-testid="node-filter-health" aria-label="健康状态筛选" placeholder="健康：全部" clearable>
          <ElOption v-for="item in healthOptions" :key="item.value" :value="item.value" :label="item.label" />
        </ElSelect>
        <ElSelect v-model="filters.scheduling" class="admin-page__select" data-testid="node-filter-scheduling" aria-label="调度状态筛选" placeholder="调度：全部" clearable>
          <ElOption v-for="item in schedulingOptions" :key="item.value" :value="item.value" :label="item.label" />
          <ElOption value="__disabled__" label="已禁用" />
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

    <div class="admin-page__table">
      <GatewayNodeTable
        ref="tableRef"
        :nodes="pagedNodes"
        :pools="pools"
        :busy-id="busyId"
        :loading="loading"
        :empty-text="hasFilters ? '没有匹配的上游节点' : '暂无上游节点'"
        @detail="openDetail"
        @probe="onProbe"
        @drain="onDrain"
        @restore="onRestore"
        @toggle-config="onToggleConfig"
        @edit="openEdit"
        @delete="onDelete"
      />
    </div>

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

    <GatewayNodeDrawer :open="drawerOpen" :node="drawerNode" :pools="pools" @close="drawerOpen = false" @saved="load" />
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElAlert, ElButton, ElInput, ElOption, ElPagination, ElSelect } from 'element-plus'
import {
  gatewayApi,
  type GatewayNode,
  type GatewayPool,
} from '@/api/gateway'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { errorMessage, healthStateLabel, schedulingStateLabel } from '@/utils/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import GatewayNodeDrawer from '@/views/gateway/GatewayNodeDrawer.vue'
import GatewayNodeTable from '@/views/gateway/GatewayNodeTable.vue'

const route = useRoute()
const router = useRouter()
const nodes = ref<GatewayNode[]>([])
const pools = ref<GatewayPool[]>([])
const loading = ref(false)
const error = ref('')
const busyId = ref('')
const drawerOpen = ref(false)
const drawerNode = ref<GatewayNode | null>(null)

const healthOptions = (['HEALTHY', 'DEGRADED', 'UNHEALTHY', 'CHECKING', 'UNKNOWN'] as const)
  .map((value) => ({ value, label: healthStateLabel(value) }))
const schedulingOptions = (['ACTIVE', 'DRAINING', 'BLOCKED'] as const)
  .map((value) => ({ value, label: schedulingStateLabel(value) }))

const filters = reactive({ keyword: '', health: '', scheduling: '', pool: '' })
const page = ref(1)
const pageSize = ref(20)
const tableRef = ref<InstanceType<typeof GatewayNodeTable> | null>(null)

const filteredNodes = computed(() => {
  const keyword = filters.keyword.trim().toLowerCase()
  return nodes.value.filter((node) => {
    if (keyword && !`${node.name} ${node.base_url} ${node.models.join(' ')}`.toLowerCase().includes(keyword)) return false
    if (filters.health && node.health_state !== filters.health) return false
    if (filters.scheduling === '__disabled__') {
      if (node.config_state !== 'DISABLED') return false
    } else if (filters.scheduling && node.scheduling_state !== filters.scheduling) {
      return false
    }
    if (filters.pool && (node.pool_id ?? '__none__') !== filters.pool) return false
    return true
  })
})
const pagedNodes = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredNodes.value.slice(start, start + pageSize.value)
})
const hasFilters = computed(() => Boolean(
  filters.keyword.trim() || filters.health || filters.scheduling || filters.pool,
))

watch(filteredNodes, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})
watch(filters, () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })
watch([page, pageSize], () => { tableRef.value?.scrollToTop() })
watch(() => route.query.pool, (pool) => {
  if (typeof pool === 'string') filters.pool = pool
})

function applySearch() {
  page.value = 1
  void load()
}

function clearFilters() {
  filters.keyword = ''
  filters.health = ''
  filters.scheduling = ''
  filters.pool = ''
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [nodeList, poolList] = await Promise.all([gatewayApi.nodes(), gatewayApi.pools()])
    nodes.value = nodeList
    pools.value = poolList
  } catch (reason) {
    error.value = errorMessage(reason, '节点列表加载失败')
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
  const confirmed = await confirmDialog({
    title: '安全下线节点',
    message: `确认让「${node.name}」进入安全下线？正在处理的请求会继续完成，新请求将转发至其它节点。`,
    confirmLabel: '安全下线',
    danger: true,
  })
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
  const confirmed = await confirmDialog({
    title: '删除节点',
    message: `确认删除「${node.name}」？请先确保节点已安全下线且无活跃请求。`,
    confirmLabel: '删除节点',
    danger: true,
  })
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

onMounted(() => {
  if (typeof route.query.pool === 'string') filters.pool = route.query.pool
  void load()
})
</script>
