<template>
  <section class="admin-page" data-testid="gateway-pools">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-pools-error">
      <ElButton type="primary" data-testid="gateway-pools-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-pools-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="gateway-pool-create" @click="openPoolCreate">新增资源池</ElButton>
    </GatewayHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="资源池筛选" data-testid="gateway-pool-filter">
      <div class="admin-page__filters">
        <ElSelect
          v-model="poolFilters.enabled"
          class="admin-page__select"
          data-testid="pool-filter-enabled"
          aria-label="资源池启用筛选"
          placeholder="状态：全部"
          clearable
        >
          <ElOption value="enabled" label="启用" />
          <ElOption value="disabled" label="禁用" />
        </ElSelect>
        <ElButton v-if="poolHasFilters" text type="primary" data-testid="gateway-pool-clear" @click="clearPoolFilters">清除筛选</ElButton>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="poolFilters.keyword"
          class="admin-page__keyword"
          data-testid="pool-filter-keyword"
          aria-label="搜索资源池"
          placeholder="搜索名称 / 描述"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="gateway-pool-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <ElTable
      v-loading="loading"
      :data="pagedPools"
      row-key="id"
      :max-height="TABLE_MAX_HEIGHT"
      element-loading-text="正在加载资源池"
      data-testid="gateway-pool-table"
    >
      <ElTableColumn label="名称" min-width="200">
        <template #default="{ row }">
          <div class="pool-cell">
            <strong>{{ row.name }}</strong>
            <span class="pool-cell__desc" :title="row.description">{{ row.description || '—' }}</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="排序" prop="sort_order" width="90" align="center" />
      <ElTableColumn label="状态" width="100" align="center">
        <template #default="{ row }">
          <ElTag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="140" align="center" fixed="right">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" :disabled="busy" @click="openPoolEdit(row as GatewayPool)">编辑</ElButton>
            <ElButton link type="danger" :disabled="busy" @click="removePool(row as GatewayPool)">删除</ElButton>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="poolEmptyText" />
      </template>
    </ElTable>

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="poolPage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filteredPools.length"
        layout="total, sizes, prev, pager, next"
        aria-label="资源池列表分页"
        data-testid="gateway-pool-pagination"
      />
    </div>

    <ElCard shadow="never" data-testid="gateway-model-distribution">
      <template #header>
        <h4 class="gateway-model-dist__title">声明模型分布</h4>
      </template>
      <ul v-if="modelRows.length" class="gateway-model-dist">
        <li v-for="item in modelRows" :key="item.label">
          <span :title="item.label">{{ item.label }}</span>
          <div><i :style="{ width: `${item.width}%` }" /></div>
          <b>{{ item.value }}</b>
        </li>
      </ul>
      <ElEmpty v-else :image-size="52" description="暂无节点配置支持模型" />
    </ElCard>

    <ElDrawer
      v-if="poolDrawerOpen"
      class="gateway-drawer"
      data-testid="gateway-pool-drawer"
      :model-value="true"
      :title="poolForm.id ? '编辑资源池' : '新增资源池'"
      size="min(460px, 100%)"
      :close-on-click-modal="false"
      :before-close="() => requestPoolClose()"
    >
      <ElForm ref="poolFormRef" :model="poolForm" :rules="poolRules" label-position="top" scroll-to-error :disabled="saving">
        <ElFormItem label="名称" prop="name">
          <ElInput v-model="poolForm.name" placeholder="例如 默认资源池" />
        </ElFormItem>
        <ElFormItem label="描述" prop="description">
          <ElInput v-model="poolForm.description" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="排序" prop="sort_order">
          <ElInput v-model="poolForm.sort_order" type="number" />
        </ElFormItem>
        <ElFormItem label="启用" prop="enabled">
          <ElSwitch v-model="poolForm.enabled" data-testid="gateway-pool-enabled" />
        </ElFormItem>
        <ElAlert v-if="poolError" type="error" show-icon :closable="false" :title="poolError" data-testid="gateway-pool-error" />
      </ElForm>
      <template #footer>
        <div class="gateway-drawer__footer">
          <ElTag v-if="poolDirty" type="warning" effect="light" data-testid="gateway-pool-dirty">有未保存修改</ElTag>
          <span class="gateway-drawer__spacer" />
          <ElButton :disabled="saving" @click="requestPoolClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="gateway-pool-save" @click="submitPool">保存</ElButton>
        </div>
      </template>
    </ElDrawer>
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElDrawer,
  ElEmpty,
  ElForm,
  ElFormItem,
  ElInput,
  ElOption,
  ElPagination,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  vLoading,
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  gatewayApi,
  type GatewayNode,
  type GatewayPool,
} from '@/api/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { errorMessage } from '@/utils/gateway'
import { TABLE_MAX_HEIGHT } from '@/utils/table'

const pools = ref<GatewayPool[]>([])
const nodes = ref<GatewayNode[]>([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const busy = computed(() => loading.value || saving.value)

const poolDrawerOpen = ref(false)
const poolFormRef = ref<FormInstance>()
const poolError = ref('')

const poolForm = reactive({ id: '', name: '', description: '', sort_order: '0', enabled: true })
const pageSize = ref(20)
const poolFilters = reactive({ keyword: '', enabled: '' })
const poolPage = ref(1)

function matchesEnabled(enabled: boolean, filter: string): boolean {
  if (!filter) return true
  return filter === 'enabled' ? enabled : !enabled
}

const filteredPools = computed(() => {
  const keyword = poolFilters.keyword.trim().toLowerCase()
  return pools.value.filter((pool) => {
    if (keyword && !`${pool.name} ${pool.description}`.toLowerCase().includes(keyword)) return false
    return matchesEnabled(pool.enabled, poolFilters.enabled)
  })
})

const pagedPools = computed(() => {
  const start = (poolPage.value - 1) * pageSize.value
  return filteredPools.value.slice(start, start + pageSize.value)
})

const poolHasFilters = computed(() => Boolean(poolFilters.keyword.trim() || poolFilters.enabled))
const poolEmptyText = computed(() => (poolHasFilters.value ? '没有匹配的资源池' : '暂无资源池'))

const modelRows = computed(() => {
  const counter = new Map<string, number>()
  for (const node of nodes.value) {
    for (const model of node.models ?? []) counter.set(model, (counter.get(model) ?? 0) + 1)
  }
  const rows = [...counter.entries()]
    .map(([label, value]) => ({ label, value }))
    .sort((left, right) => right.value - left.value)
  const max = Math.max(1, ...rows.map((item) => item.value))
  return rows.map((item) => ({ ...item, width: item.value ? Math.max(8, Math.round((item.value / max) * 100)) : 0 }))
})

watch(poolFilters, () => { poolPage.value = 1 })
watch(pageSize, () => { poolPage.value = 1 })
watch(filteredPools, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (poolPage.value > maxPage) poolPage.value = maxPage
})

function applySearch() {
  poolPage.value = 1
  void load()
}

function clearPoolFilters() {
  poolFilters.keyword = ''
  poolFilters.enabled = ''
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [poolList, nodeList] = await Promise.all([
      gatewayApi.pools(),
      gatewayApi.nodes(),
    ])
    pools.value = poolList
    nodes.value = nodeList
  } catch (reason) {
    error.value = errorMessage(reason, '资源池加载失败')
  } finally {
    loading.value = false
  }
}

function poolSnapshot(): string {
  return JSON.stringify(poolForm)
}
const poolInitial = ref('')
const poolDirty = computed(() => poolDrawerOpen.value && poolSnapshot() !== poolInitial.value)

const poolRules: FormRules = {
  name: [{ required: true, message: '请填写资源池名称', trigger: 'blur' }],
}

function openPoolCreate() {
  Object.assign(poolForm, { id: '', name: '', description: '', sort_order: '0', enabled: true })
  poolError.value = ''
  poolInitial.value = poolSnapshot()
  poolDrawerOpen.value = true
}

function openPoolEdit(pool: GatewayPool) {
  Object.assign(poolForm, {
    id: pool.id,
    name: pool.name,
    description: pool.description,
   
    sort_order: String(pool.sort_order),
    enabled: pool.enabled,
  })
  poolError.value = ''
  poolInitial.value = poolSnapshot()
  poolDrawerOpen.value = true
}

async function submitPool() {
  if (!(await poolFormRef.value?.validate().catch(() => false))) return
  saving.value = true
  poolError.value = ''
  try {
    const body = {
      name: poolForm.name.trim(),
      description: poolForm.description,
      sort_order: Number(poolForm.sort_order) || 0,
      enabled: poolForm.enabled,
    }
    if (poolForm.id) await gatewayApi.updatePool(poolForm.id, body)
    else await gatewayApi.createPool(body)
    poolDrawerOpen.value = false
    notify('资源池已保存')
    await load()
  } catch (reason) {
    poolError.value = errorMessage(reason, '保存资源池失败')
  } finally {
    saving.value = false
  }
}

async function requestPoolClose() {
  if (saving.value) return
  if (poolDirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存修改',
      message: '当前资源池配置尚未保存，确认放弃并关闭？',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    })
    if (!confirmed) return
  }
  poolDrawerOpen.value = false
}

async function removePool(pool: GatewayPool) {
  const confirmed = await confirmDialog({
    title: '删除资源池',
    message: `确认删除「${pool.name}」？仍被节点或路由引用的资源池无法删除。`,
    confirmLabel: '删除',
    danger: true,
  })
  if (!confirmed) return
  try {
    await gatewayApi.deletePool(pool.id)
    notify('资源池已删除')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '删除失败'), 'error')
  }
}

onMounted(load)
</script>

<style scoped>
.pool-cell { display: grid; gap: 2px; min-width: 0; }
.pool-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.pool-cell__desc { overflow: hidden; color: var(--astra-text-muted); font-size: var(--astra-font-helper); text-overflow: ellipsis; white-space: nowrap; }
.gateway-model-dist__title { margin: 0; color: var(--astra-text-primary); }
.gateway-model-dist { display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }
.gateway-model-dist li { display: grid; grid-template-columns: minmax(80px, 180px) minmax(60px, 1fr) 32px; align-items: center; gap: 10px; }
.gateway-model-dist span { overflow: hidden; color: var(--astra-text-secondary); text-overflow: ellipsis; white-space: nowrap; }
.gateway-model-dist div { height: 8px; overflow: hidden; border-radius: 999px; background: var(--astra-bg-elevated); }
.gateway-model-dist i { display: block; height: 100%; border-radius: 999px; background: #0d9e61; }
.gateway-model-dist b { color: var(--astra-text-primary); text-align: right; }
.gateway-drawer__footer { display: flex; align-items: center; gap: 10px; }
.gateway-drawer__spacer { flex: 1; }
</style>
