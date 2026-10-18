<template>
  <section class="gateway-pools-routes" data-testid="gateway-pools-routes">
    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-pools-routes-refresh" @click="load">刷新</ElButton>
    </GatewayHeaderActions>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-pools-routes-error">
      <ElButton type="primary" data-testid="gateway-pools-routes-retry" @click="load">重试</ElButton>
    </ElAlert>

    <ElCard shadow="never" data-testid="gateway-pool-card">
      <template #header>
        <div class="gateway-pools-routes__card-head">
          <div class="gateway-pools-routes__card-title">
            <h4>资源池</h4>
            <ElText type="info">仍被节点或路由引用的资源池无法删除。</ElText>
          </div>
          <ElButton type="primary" :icon="Plus" data-testid="gateway-pool-create" @click="openPoolCreate">新增资源池</ElButton>
        </div>
      </template>

      <AstraFilterPanel label="资源池筛选" testid="gateway-pool-filter">
        <ElSelect v-model="poolFilters.enabled" class="astra-filter__select" data-testid="pool-filter-enabled" aria-label="资源池启用筛选" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="启用" />
          <ElOption value="disabled" label="禁用" />
        </ElSelect>
        <ElButton v-if="poolHasFilters" text type="primary" data-testid="gateway-pool-clear" @click="clearPoolFilters">清除筛选</ElButton>
        <template #search>
          <ElInput
            v-model="poolFilters.keyword"
            class="astra-filter__keyword"
            data-testid="pool-filter-keyword"
            aria-label="搜索资源池"
            placeholder="搜索名称 / 描述"
            clearable
          >
            <template #prefix><Search :size="15" /></template>
          </ElInput>
          <ElButton type="primary" :icon="Search" data-testid="gateway-pool-search" @click="load">搜索</ElButton>
        </template>
      </AstraFilterPanel>

      <ElTable
        v-loading="loading"
        :data="pagedPools"
        row-key="id"
        element-loading-text="正在加载资源池"
        data-testid="gateway-pool-table"
        highlight-current-row
        :row-class-name="poolRowClass"
        @row-click="selectPool"
      >
        <ElTableColumn label="名称" min-width="220">
          <template #default="{ row }">
            <button
              type="button"
              class="pool-pick"
              tabindex="0"
              :aria-selected="row.id === routeFilters.pool"
              :aria-label="`选择 ${row.name} 的路由`"
              @click.stop="selectPool(row as GatewayPool)"
            >
              <strong>{{ row.name }}</strong>
              <span class="pool-cell__desc" :title="row.description">{{ row.description || '—' }}</span>
            </button>
          </template>
        </ElTableColumn>
        <ElTableColumn label="权重" prop="weight" width="90" />
        <ElTableColumn label="状态" width="90">
          <template #default="{ row }">
            <ElTag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="排序" prop="sort_order" width="80" />
        <ElTableColumn label="操作" width="120">
          <template #default="{ row }">
            <RowMenu primary="编辑" :aria-label="(row as GatewayPool).name" :disabled="busy" :items="[{ command: 'delete', label: '删除' }]" @primary="openPoolEdit(row as GatewayPool)" @command="removePool(row as GatewayPool)" />
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="!loading" :image-size="64" :description="poolEmptyText" />
        </template>
      </ElTable>

      <footer v-if="filteredPools.length" class="gateway-pools-routes__footer">
        <ElText type="info" data-testid="gateway-pool-total">共 {{ filteredPools.length }} 个</ElText>
        <ElPagination
          :current-page="poolPage"
          :page-size="pageSize"
          :total="filteredPools.length"
          layout="prev, pager, next"
          aria-label="资源池列表分页"
          data-testid="gateway-pool-pagination"
          @current-change="poolPage = $event"
        />
      </footer>
    </ElCard>

    <ElCard shadow="never" data-testid="gateway-route-card">
      <template #header>
        <div class="gateway-pools-routes__card-head">
          <div class="gateway-pools-routes__card-title">
            <h4>{{ selectedPoolName ? `${selectedPoolName} 的路由` : '模型路由' }}</h4>

          </div>
          <ElButton type="primary" :icon="Plus" data-testid="gateway-route-create" @click="openRouteCreate">新增路由</ElButton>
        </div>
      </template>

      <AstraFilterPanel label="路由筛选" testid="gateway-route-filter">
        <ElButton text type="primary" data-testid="route-filter-pool" @click="showAllRoutes">{{ routeFilters.pool ? '查看全部路由' : '正在看全部路由' }}</ElButton>
        <ElSelect v-model="routeFilters.enabled" class="astra-filter__select" data-testid="route-filter-enabled" aria-label="路由启用筛选" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="启用" />
          <ElOption value="disabled" label="禁用" />
        </ElSelect>
        <ElButton v-if="routeHasFilters" text type="primary" data-testid="gateway-route-clear" @click="clearRouteFilters">清除筛选</ElButton>
        <template #search>
          <ElInput
            v-model="routeFilters.keyword"
            class="astra-filter__keyword"
            data-testid="route-filter-keyword"
            aria-label="搜索路由"
            placeholder="搜索模型名 / 描述"
            clearable
          >
            <template #prefix><Search :size="15" /></template>
          </ElInput>
          <ElButton type="primary" :icon="Search" data-testid="gateway-route-search" @click="load">搜索</ElButton>
        </template>
      </AstraFilterPanel>

      <ElTable
        v-loading="loading"
        :data="pagedRoutes"
        row-key="id"
        element-loading-text="正在加载路由"
        data-testid="gateway-route-table"
      >
        <ElTableColumn label="模型名" min-width="220">
          <template #default="{ row }">
            <div class="pool-cell">
              <strong>{{ row.model }}</strong>
              <span class="pool-cell__desc" :title="row.description">{{ row.description || '—' }}</span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="资源池" width="150" show-overflow-tooltip>
          <template #default="{ row }">{{ poolName(row.pool_id) }}</template>
        </ElTableColumn>
        <ElTableColumn label="权重" prop="weight" width="90" />
        <ElTableColumn label="状态" width="90">
          <template #default="{ row }">
            <ElTag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="120">
          <template #default="{ row }">
            <RowMenu primary="编辑" :aria-label="(row as GatewayRoute).model" :disabled="busy" :items="[{ command: 'delete', label: '删除' }]" @primary="openRouteEdit(row as GatewayRoute)" @command="removeRoute(row as GatewayRoute)" />
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="!loading" :image-size="64" :description="routeEmptyText" />
        </template>
      </ElTable>

      <footer v-if="filteredRoutes.length" class="gateway-pools-routes__footer">
        <ElText type="info" data-testid="gateway-route-total">共 {{ filteredRoutes.length }} 条</ElText>
        <ElPagination
          :current-page="routePage"
          :page-size="pageSize"
          :total="filteredRoutes.length"
          layout="prev, pager, next"
          aria-label="路由列表分页"
          data-testid="gateway-route-pagination"
          @current-change="routePage = $event"
        />
      </footer>
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
        <ElFormItem label="权重" prop="weight">
          <ElInput v-model="poolForm.weight" type="number" min="0" />
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

    <ElDrawer
      v-if="routeDrawerOpen"
      class="gateway-drawer"
      data-testid="gateway-route-drawer"
      :model-value="true"
      :title="routeForm.id ? '编辑路由' : '新增路由'"
      size="min(460px, 100%)"
      :close-on-click-modal="false"
      :before-close="() => requestRouteClose()"
    >
      <ElForm ref="routeFormRef" :model="routeForm" :rules="routeRules" label-position="top" scroll-to-error :disabled="saving">
        <ElFormItem label="模型名" prop="model">
          <ElInput v-model="routeForm.model" placeholder="例如 gpt-4o-mini" />
        </ElFormItem>
        <ElFormItem label="资源池" prop="pool_id">
          <ElSelect v-model="routeForm.pool_id" placeholder="选择资源池" data-testid="gateway-route-pool">
            <ElOption
              v-for="pool in pools"
              :key="pool.id"
              :value="pool.id"
              :label="`${pool.name}${pool.enabled ? '' : '（已禁用）'}`"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="描述" prop="description">
          <ElInput v-model="routeForm.description" type="textarea" :rows="2" />
        </ElFormItem>
        <ElFormItem label="权重" prop="weight">
          <ElInput v-model="routeForm.weight" type="number" min="0" />
        </ElFormItem>
        <ElFormItem label="启用" prop="enabled">
          <ElSwitch v-model="routeForm.enabled" data-testid="gateway-route-enabled" />
        </ElFormItem>
        <ElAlert v-if="routeError" type="error" show-icon :closable="false" :title="routeError" data-testid="gateway-route-error" />
      </ElForm>
      <template #footer>
        <div class="gateway-drawer__footer">
          <ElTag v-if="routeDirty" type="warning" effect="light" data-testid="gateway-route-dirty">有未保存修改</ElTag>
          <span class="gateway-drawer__spacer" />
          <ElButton :disabled="saving" @click="requestRouteClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="gateway-route-save" @click="submitRoute">保存</ElButton>
        </div>
      </template>
    </ElDrawer>

    <ElCard shadow="never" data-testid="gateway-model-distribution">
      <template #header>
        <div class="gateway-pools-routes__card-title">
          <h4>声明模型分布</h4>
          <ElText>按节点声明模型计数</ElText>
        </div>
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
  ElSpace,
  ElSwitch,
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
  vLoading,
} from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import {
  gatewayApi,
  type GatewayNode,
  type GatewayPool,
  type GatewayRoute,
} from '@/api/gateway'
import AstraFilterPanel from '@/components/astra/AstraFilterPanel.vue'
import RowMenu from '@/components/astra/RowMenu.vue'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { errorMessage } from '@/utils/gateway'

const pools = ref<GatewayPool[]>([])
const routes = ref<GatewayRoute[]>([])
const nodes = ref<GatewayNode[]>([])
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
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const busy = computed(() => loading.value || saving.value)

const poolDrawerOpen = ref(false)
const routeDrawerOpen = ref(false)
const poolFormRef = ref<FormInstance>()
const routeFormRef = ref<FormInstance>()
const poolError = ref('')
const routeError = ref('')

const poolForm = reactive({ id: '', name: '', description: '', weight: '1', sort_order: '0', enabled: true })
const routeForm = reactive({ id: '', model: '', pool_id: '', description: '', weight: '1', enabled: true })

const pageSize = 10

const poolFilters = reactive({ keyword: '', enabled: '' })
const routeFilters = reactive({ keyword: '', pool: '', enabled: '' })
const poolPicked = ref(false)
const selectedPoolName = computed(() => pools.value.find((pool) => pool.id === routeFilters.pool)?.name ?? '')
function selectPool(row: GatewayPool) {
  poolPicked.value = true
  routeFilters.pool = row.id
}
function showAllRoutes() {
  poolPicked.value = true
  routeFilters.pool = ''
}
function poolRowClass({ row }: { row: GatewayPool }) {
  return row.id === routeFilters.pool ? 'is-selected-pool' : ''
}
const poolPage = ref(1)
const routePage = ref(1)

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
const filteredRoutes = computed(() => {
  const keyword = routeFilters.keyword.trim().toLowerCase()
  return routes.value.filter((route) => {
    if (keyword && !`${route.model} ${route.description}`.toLowerCase().includes(keyword)) return false
    if (routeFilters.pool && route.pool_id !== routeFilters.pool) return false
    return matchesEnabled(route.enabled, routeFilters.enabled)
  })
})
const pagedPools = computed(() => {
  const start = (poolPage.value - 1) * pageSize
  return filteredPools.value.slice(start, start + pageSize)
})
const pagedRoutes = computed(() => {
  const start = (routePage.value - 1) * pageSize
  return filteredRoutes.value.slice(start, start + pageSize)
})
const poolHasFilters = computed(() => Boolean(poolFilters.keyword.trim() || poolFilters.enabled))
const routeHasFilters = computed(() => Boolean(routeFilters.keyword.trim() || routeFilters.pool || routeFilters.enabled))
const poolEmptyText = computed(() => (poolHasFilters.value ? '没有匹配的资源池，调整筛选条件后重试。' : '暂无资源池，点击「新增资源池」开始配置。'))
const routeEmptyText = computed(() => (routeHasFilters.value ? '没有匹配的路由，调整筛选条件后重试。' : '暂无模型路由，点击「新增路由」开始配置。'))

watch(poolFilters, () => { poolPage.value = 1 })
watch(routeFilters, () => { routePage.value = 1 })
watch(pools, (list) => {
  if (!poolPicked.value && !routeFilters.pool && list.length) routeFilters.pool = list[0].id
})
watch(filteredPools, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize))
  if (poolPage.value > maxPage) poolPage.value = maxPage
})
watch(filteredRoutes, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize))
  if (routePage.value > maxPage) routePage.value = maxPage
})

function clearPoolFilters() {
  poolFilters.keyword = ''
  poolFilters.enabled = ''
}

function clearRouteFilters() {
  routeFilters.keyword = ''
  routeFilters.pool = ''
  routeFilters.enabled = ''
}

function poolName(poolId: string): string {
  return pools.value.find((pool) => pool.id === poolId)?.name ?? poolId
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [poolList, routeList, nodeList] = await Promise.all([
      gatewayApi.pools(), gatewayApi.routes(), gatewayApi.nodes(),
    ])
    pools.value = poolList
    routes.value = routeList
    nodes.value = nodeList
  } catch (reason) {
    error.value = errorMessage(reason, '资源池 / 路由加载失败')
  } finally {
    loading.value = false
  }
}

// ── 资源池：抽屉表单 + 未保存提醒 ──────────────────────────────────────

function poolSnapshot(): string {
  return JSON.stringify(poolForm)
}
const poolInitial = ref('')
const poolDirty = computed(() => poolDrawerOpen.value && poolSnapshot() !== poolInitial.value)

const poolRules: FormRules = {
  name: [{ required: true, message: '请填写资源池名称', trigger: 'blur' }],
}

function openPoolCreate() {
  Object.assign(poolForm, { id: '', name: '', description: '', weight: '1', sort_order: '0', enabled: true })
  poolError.value = ''
  poolInitial.value = poolSnapshot()
  poolDrawerOpen.value = true
}

function openPoolEdit(pool: GatewayPool) {
  Object.assign(poolForm, {
    id: pool.id,
    name: pool.name,
    description: pool.description,
    weight: String(pool.weight),
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
      weight: Number(poolForm.weight) || 0,
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

// ── 模型路由：抽屉表单 + 模型名唯一 + 未保存提醒 ───────────────────────

function routeSnapshot(): string {
  return JSON.stringify(routeForm)
}
const routeInitial = ref('')
const routeDirty = computed(() => routeDrawerOpen.value && routeSnapshot() !== routeInitial.value)

const routeRules: FormRules = {
  model: [
    { required: true, message: '请填写模型名', trigger: 'blur' },
    {
      validator: (_rule, value, callback) => {
        const text = String(value ?? '').trim()
        if (text && routes.value.some((route) => route.model === text && route.id !== routeForm.id)) {
          return callback(new Error(`模型「${text}」已有路由，模型名必须唯一`))
        }
        callback()
      },
      trigger: 'blur',
    },
  ],
  pool_id: [{ required: true, message: '请选择资源池', trigger: 'change' }],
}

function openRouteCreate() {
  Object.assign(routeForm, { id: '', model: '', pool_id: '', description: '', weight: '1', enabled: true })
  routeError.value = ''
  routeInitial.value = routeSnapshot()
  routeDrawerOpen.value = true
}

function openRouteEdit(route: GatewayRoute) {
  Object.assign(routeForm, {
    id: route.id,
    model: route.model,
    pool_id: route.pool_id,
    description: route.description,
    weight: String(route.weight),
    enabled: route.enabled,
  })
  routeError.value = ''
  routeInitial.value = routeSnapshot()
  routeDrawerOpen.value = true
}

async function submitRoute() {
  if (!(await routeFormRef.value?.validate().catch(() => false))) return
  saving.value = true
  routeError.value = ''
  try {
    const body = {
      model: routeForm.model.trim(),
      pool_id: routeForm.pool_id,
      description: routeForm.description,
      weight: Number(routeForm.weight) || 0,
      enabled: routeForm.enabled,
    }
    if (routeForm.id) await gatewayApi.updateRoute(routeForm.id, body)
    else await gatewayApi.createRoute(body)
    routeDrawerOpen.value = false
    notify('路由已保存')
    await load()
  } catch (reason) {
    routeError.value = errorMessage(reason, '保存路由失败')
  } finally {
    saving.value = false
  }
}

async function requestRouteClose() {
  if (saving.value) return
  if (routeDirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存修改',
      message: '当前路由配置尚未保存，确认放弃并关闭？',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    })
    if (!confirmed) return
  }
  routeDrawerOpen.value = false
}

async function removeRoute(route: GatewayRoute) {
  const confirmed = await confirmDialog({
    title: '删除路由',
    message: `确认删除模型「${route.model}」的路由？`,
    confirmLabel: '删除',
    danger: true,
  })
  if (!confirmed) return
  try {
    await gatewayApi.deleteRoute(route.id)
    notify('路由已删除')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '删除失败'), 'error')
  }
}

onMounted(load)
</script>

<style scoped>
.gateway-pools-routes { display: grid; gap: 12px; }
.gateway-model-dist { display: grid; gap: 9px; margin: 0; padding: 0; list-style: none; }
.gateway-model-dist li { display: grid; grid-template-columns: minmax(80px, 180px) minmax(60px, 1fr) 32px; align-items: center; gap: 10px; }
.gateway-model-dist span { overflow: hidden; color: var(--astra-text-secondary); text-overflow: ellipsis; white-space: nowrap; }
.gateway-model-dist div { height: 8px; overflow: hidden; border-radius: 999px; background: var(--astra-bg-elevated); }
.gateway-model-dist i { display: block; height: 100%; border-radius: 999px; background: #0d9e61; }
.gateway-model-dist b { color: var(--astra-text-primary); text-align: right; }
.gateway-pools-routes__card-head { display: flex; align-items: center; gap: 12px; }
.gateway-pools-routes__card-title { display: grid; gap: 2px; margin-right: auto; }
.gateway-pools-routes__card-title h4 { margin: 0; color: var(--astra-text-primary); }
.gateway-pools-routes__footer { display: flex; align-items: center; justify-content: space-between; gap: 16px; padding-top: 10px; }
.pool-pick { display: grid; gap: 2px; width: 100%; min-width: 0; margin: 0; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; text-align: left; cursor: pointer; }
.pool-pick:focus-visible { outline: 2px solid var(--astra-primary); outline-offset: 2px; }
.pool-cell { display: grid; gap: 2px; min-width: 0; }
.pool-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.pool-cell__desc { overflow: hidden; color: var(--astra-text-muted); font-size: var(--astra-font-helper); text-overflow: ellipsis; white-space: nowrap; }
.gateway-drawer__footer { display: flex; align-items: center; gap: 10px; }
.gateway-drawer__spacer { flex: 1; }
.gateway-pools-routes :deep(.is-selected-pool) { background: #f3faf6; }
</style>
