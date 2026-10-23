<template>
  <section class="admin-page" data-testid="gateway-routes">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-routes-error">
      <ElButton type="primary" data-testid="gateway-routes-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-routes-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="gateway-route-create" @click="openRouteCreate">新增路由</ElButton>
    </GatewayHeaderActions>

    <ElForm label-position="top" class="admin-page__toolbar" data-testid="speech-input-config">
      <ElFormItem label="数字员工语音输入">
        <ElSelect v-model="speechRouteId" clearable filterable placeholder="选择已启用的语音识别路由" style="width: min(400px, 70vw)" :disabled="loading || savingSpeech">
          <ElOption v-for="route in speechRoutes" :key="route.id" :value="route.id" :label="route.model" />
        </ElSelect>
      </ElFormItem>
      <ElButton type="primary" :disabled="loading" :loading="savingSpeech" @click="saveSpeech">保存语音输入</ElButton>
      <span>私聊与群聊共用此路由，识别文字回填草稿后手动发送。</span>
      <ElAlert v-if="speechRouteUnavailable" type="warning" :closable="false" title="已保存的语音路由不可用，请重新选择已启用的 ASR 路由。" />
    </ElForm>

    <div class="admin-page__toolbar" role="search" aria-label="路由筛选" data-testid="gateway-route-filter">
      <div class="admin-page__filters">
        <ElSelect
          v-model="routeFilters.pool"
          class="admin-page__select"
          data-testid="route-filter-pool"
          aria-label="资源池筛选"
          placeholder="资源池：全部"
          clearable
        >
          <ElOption v-for="pool in pools" :key="pool.id" :value="pool.id" :label="pool.name" />
        </ElSelect>
        <ElSelect
          v-model="routeFilters.enabled"
          class="admin-page__select"
          data-testid="route-filter-enabled"
          aria-label="路由启用筛选"
          placeholder="状态：全部"
          clearable
        >
          <ElOption value="enabled" label="启用" />
          <ElOption value="disabled" label="禁用" />
        </ElSelect>
        <ElButton v-if="routeHasFilters" text type="primary" data-testid="gateway-route-clear" @click="clearRouteFilters">清除筛选</ElButton>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="routeFilters.keyword"
          class="admin-page__keyword"
          data-testid="route-filter-keyword"
          aria-label="搜索路由"
          placeholder="搜索模型名 / 目标模型 / 描述"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="gateway-route-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <ElTable
      v-loading="loading"
      :data="pagedRoutes"
      row-key="id"
      :max-height="TABLE_MAX_HEIGHT"
      element-loading-text="正在加载路由"
      data-testid="gateway-route-table"
    >
      <ElTableColumn label="请求模型" min-width="180">
        <template #default="{ row }">
          <div class="route-cell">
            <strong>{{ row.model }}</strong>
            <span class="route-cell__desc" :title="row.description">{{ row.description || '—' }}</span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="目标实际模型" min-width="180">
        <template #default="{ row }">
          <template v-if="row.target_model && row.target_model !== row.model">
            <ElTag type="success" effect="plain" class="route-target-tag">
              <span class="route-target-arrow">→</span> {{ row.target_model }}
            </ElTag>
          </template>
          <span v-else class="route-same-muted">与请求一致</span>
        </template>
      </ElTableColumn>
      <ElTableColumn label="模型类型" width="120">
        <template #default="{ row }">{{ modelTypeLabel(row.model_type) }}</template>
      </ElTableColumn>
      <ElTableColumn label="关联资源池" min-width="140" show-overflow-tooltip>
        <template #default="{ row }">{{ poolName(row.pool_id) }}</template>
      </ElTableColumn>
      <ElTableColumn label="状态" width="100" align="center">
        <template #default="{ row }">
          <ElTag :type="row.enabled ? 'success' : 'info'">{{ row.enabled ? '启用' : '禁用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="140" align="center" fixed="right">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" :disabled="busy" @click="openRouteEdit(row as GatewayRoute)">编辑</ElButton>
            <ElButton link type="danger" :disabled="busy" @click="removeRoute(row as GatewayRoute)">删除</ElButton>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="!loading" :image-size="64" :description="routeEmptyText" />
      </template>
    </ElTable>

    <div class="admin-page__footer">
      <ElPagination
        v-model:current-page="routePage"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filteredRoutes.length"
        layout="total, sizes, prev, pager, next"
        aria-label="路由列表分页"
        data-testid="gateway-route-pagination"
      />
    </div>

    <ElDrawer
      v-if="routeDrawerOpen"
      class="gateway-drawer"
      data-testid="gateway-route-drawer"
      :model-value="true"
      :title="routeForm.id ? '编辑模型路由' : '新增模型路由'"
      size="min(480px, 100%)"
      :close-on-click-modal="false"
      :before-close="() => requestRouteClose()"
    >
      <ElForm ref="routeFormRef" :model="routeForm" :rules="routeRules" label-position="top" scroll-to-error :disabled="saving">
        <ElFormItem label="模型类型" prop="model_type">
          <ElSelect v-model="routeForm.model_type">
            <ElOption value="CHAT" label="对话" />
            <ElOption value="EMBEDDING" label="向量嵌入" />
            <ElOption value="ASR" label="语音识别（ASR）" />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="请求模型名" prop="model">
          <ElInput v-model="routeForm.model" placeholder="客户端调用的模型名称，如 GLM-5.2 或 gpt-4o" />
        </ElFormItem>
        <ElFormItem prop="target_model">
          <template #label>目标模型（可选）</template>
          <ElInput
            v-model="routeForm.target_model"
            placeholder="留空表示与请求模型一致（如上游实际为 GLM-5.3）"
            clearable
          />
        </ElFormItem>
        <ElFormItem label="关联资源池" prop="pool_id">
          <ElSelect v-model="routeForm.pool_id" placeholder="选择资源池" data-testid="gateway-route-pool" style="width: 100%">
            <ElOption
              v-for="pool in pools"
              :key="pool.id"
              :value="pool.id"
              :label="`${pool.name}${pool.enabled ? '' : '（已禁用）'}`"
            />
          </ElSelect>
        </ElFormItem>
        <ElFormItem label="描述" prop="description">
          <ElInput v-model="routeForm.description" type="textarea" :rows="2" placeholder="例如：研发团队" />
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
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
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
  type GatewayPool,
  type GatewayRoute,
} from '@/api/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { errorMessage } from '@/utils/gateway'
import { TABLE_MAX_HEIGHT } from '@/utils/table'

const pools = ref<GatewayPool[]>([])
const routes = ref<GatewayRoute[]>([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const busy = computed(() => loading.value || saving.value)

const routeDrawerOpen = ref(false)
const routeFormRef = ref<FormInstance>()
const routeError = ref('')

const routeForm = reactive({
  id: '',
  model: '',
  model_type: 'CHAT' as 'CHAT' | 'EMBEDDING' | 'ASR',
  target_model: '',
  pool_id: '',
  description: '',
  enabled: true,
})

const speechRouteId = ref<string | undefined>()
const savingSpeech = ref(false)
const speechRoutes = computed(() => routes.value.filter((route) =>
  route.enabled && route.model_type === 'ASR' && pools.value.some((pool) => pool.id === route.pool_id && pool.enabled),
))
const speechRouteUnavailable = computed(() =>
  Boolean(speechRouteId.value && !speechRoutes.value.some((route) => route.id === speechRouteId.value)),
)

function modelTypeLabel(value: string): string {
  return ({ CHAT: '对话', EMBEDDING: '向量嵌入', ASR: '语音识别' } as Record<string, string>)[value] ?? value
}

async function saveSpeech() {
  savingSpeech.value = true
  try {
    await gatewayApi.saveSpeechInput(speechRouteId.value || null)
    notify('语音输入配置已保存')
  } catch (reason) {
    notify(errorMessage(reason, '语音输入配置保存失败'), 'error')
  } finally {
    savingSpeech.value = false
  }
}

const pageSize = ref(20)
const routeFilters = reactive({ keyword: '', pool: '', enabled: '' })
const routePage = ref(1)

function matchesEnabled(enabled: boolean, filter: string): boolean {
  if (!filter) return true
  return filter === 'enabled' ? enabled : !enabled
}

const filteredRoutes = computed(() => {
  const keyword = routeFilters.keyword.trim().toLowerCase()
  return routes.value.filter((route) => {
    if (
      keyword &&
      !`${route.model} ${route.target_model ?? ''} ${route.description}`.toLowerCase().includes(keyword)
    ) {
      return false
    }
    if (routeFilters.pool && route.pool_id !== routeFilters.pool) return false
    return matchesEnabled(route.enabled, routeFilters.enabled)
  })
})

const pagedRoutes = computed(() => {
  const start = (routePage.value - 1) * pageSize.value
  return filteredRoutes.value.slice(start, start + pageSize.value)
})

const routeHasFilters = computed(() => Boolean(routeFilters.keyword.trim() || routeFilters.pool || routeFilters.enabled))
const routeEmptyText = computed(() => (routeHasFilters.value ? '没有匹配的路由' : '暂无模型路由'))

watch(routeFilters, () => { routePage.value = 1 })
watch(pageSize, () => { routePage.value = 1 })
watch(filteredRoutes, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (routePage.value > maxPage) routePage.value = maxPage
})

function applySearch() {
  routePage.value = 1
  void load()
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
    const [poolList, routeList, speech] = await Promise.all([
      gatewayApi.pools(),
      gatewayApi.routes(),
      gatewayApi.speechInput(),
    ])
    pools.value = poolList
    routes.value = routeList
    speechRouteId.value = speech.route_id ?? undefined
  } catch (reason) {
    error.value = errorMessage(reason, '路由加载失败')
  } finally {
    loading.value = false
  }
}

function routeSnapshot(): string {
  return JSON.stringify(routeForm)
}
const routeInitial = ref('')
const routeDirty = computed(() => routeDrawerOpen.value && routeSnapshot() !== routeInitial.value)

const routeRules: FormRules = {
  model: [
    { required: true, message: '请填写请求模型名', trigger: 'blur' },
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
  Object.assign(routeForm, {
    id: '',
    model: '',
    model_type: 'CHAT',
    target_model: '',
    pool_id: pools.value[0]?.id ?? '',
    description: '',
      enabled: true,
  })
  routeError.value = ''
  routeInitial.value = routeSnapshot()
  routeDrawerOpen.value = true
}

function openRouteEdit(route: GatewayRoute) {
  Object.assign(routeForm, {
    id: route.id,
    model: route.model,
    model_type: route.model_type,
    target_model: route.target_model ?? '',
    pool_id: route.pool_id,
    description: route.description,
   
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
    const target = routeForm.target_model.trim()
    const body = {
      model: routeForm.model.trim(),
      model_type: routeForm.model_type,
      target_model: target ? target : null,
      pool_id: routeForm.pool_id,
      description: routeForm.description,
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
.route-cell { display: grid; gap: 2px; min-width: 0; }
.route-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.route-cell__desc { overflow: hidden; color: var(--astra-text-muted); font-size: var(--astra-font-helper); text-overflow: ellipsis; white-space: nowrap; }
.route-target-tag { font-family: monospace; font-size: 13px; }
.route-target-arrow { color: #0d9e61; font-weight: bold; margin-right: 2px; }
.route-same-muted { color: var(--astra-text-muted); font-size: 13px; }
.gateway-drawer__footer { display: flex; align-items: center; gap: 10px; }
.gateway-drawer__spacer { flex: 1; }
</style>
