<template>
  <section class="admin-page admin-page--fill" data-testid="gateway-keys">
    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="gateway-keys-error">
      <ElButton type="primary" data-testid="gateway-keys-retry" @click="load">重试</ElButton>
    </ElAlert>

    <GatewayHeaderActions>
      <ElButton :loading="loading" :icon="RefreshCw" data-testid="gateway-keys-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="gateway-keys-create" @click="openCreate">新增 Key</ElButton>
    </GatewayHeaderActions>

    <div class="gateway-endpoint-strip" data-testid="gateway-endpoint-strip">
      <div class="gateway-endpoint-strip__left">
        <Link2 :size="15" class="gateway-endpoint-strip__icon" />
        <span class="gateway-endpoint-strip__label">接入地址</span>
        <code class="gateway-endpoint-strip__url">{{ endpointUrl }}</code>
        <span class="gateway-endpoint-strip__hint">OpenAI 兼容接口，使用下方 API Key 鉴权</span>
      </div>
      <ElButton size="small" :icon="Copy" @click="copyEndpoint">复制地址</ElButton>
    </div>

    <div class="admin-page__toolbar" role="search" aria-label="Key 筛选" data-testid="gateway-keys-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="filters.status" class="admin-page__select" data-testid="key-filter-status" aria-label="Key 状态筛选" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="启用" />
          <ElOption value="disabled" label="停用" />
          <ElOption value="revoked" label="已吊销" />
        </ElSelect>
        <ElButton v-if="hasFilters" text type="primary" data-testid="gateway-keys-clear" @click="clearFilters">清除筛选</ElButton>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="filters.keyword"
          class="admin-page__keyword"
          data-testid="key-filter-keyword"
          aria-label="搜索 Key"
          placeholder="搜索名称 / 前缀 / 归属"
          clearable
          :prefix-icon="Search"
          @keydown.enter.prevent="applySearch"
        />
        <ElButton type="primary" :icon="Search" data-testid="gateway-keys-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="pagedKeys"
        row-key="id"
        height="100%"
        element-loading-text="正在加载 Key"
        data-testid="gateway-keys-table"
      >
        <ElTableColumn label="名称 / 前缀" min-width="220">
          <template #default="{ row }">
            <div class="key-cell">
              <strong :title="row.name">{{ row.name }}</strong>
              <div class="key-cell__meta">
                <span class="key-prefix-chip">{{ row.key_prefix }}…</span>
                <span class="key-cell__models" :title="row.allowed_models.length ? row.allowed_models.join('、') : '全部模型'">
                  {{ row.allowed_models.length ? row.allowed_models.join('、') : '全部模型' }}
                </span>
              </div>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="状态" width="92" align="center">
          <template #default="{ row }">
            <ElTag v-if="row.revoked_at" type="danger">已吊销</ElTag>
            <ElTag v-else-if="isExpired(row.expires_at)" type="warning">已过期</ElTag>
            <ElTag
              v-else
              :type="row.enabled ? 'success' : 'info'"
              :class="{ 'astra-tag--neutral': !row.enabled }"
            >{{ row.enabled ? '启用' : '停用' }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="归属" min-width="100" show-overflow-tooltip>
          <template #default="{ row }">
            <span :class="{ 'key-muted': !row.owner_id }">{{ row.owner_id || '—' }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="并发 / Token 限额" min-width="165" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="key-num">并发 {{ row.max_concurrency || '不限' }} · 时/日 {{ tokenLimitText(row.hourly_token_limit) }}/{{ tokenLimitText(row.daily_token_limit) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最近使用" width="110">
          <template #default="{ row }">
            <span class="key-num" :class="{ 'key-muted': !row.last_used_at }">
              {{ row.last_used_at ? formatRelativeTime(row.last_used_at) : '未使用' }}
            </span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="有效期" width="150">
          <template #default="{ row }">
            <span
              class="key-num"
              :class="{
                'key-muted': !row.expires_at,
                'key-expired': isExpired(row.expires_at),
              }"
            >{{ formatShortDate(row.expires_at) }}</span>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="118" align="center" fixed="right">
          <template #default="{ row }">
            <div class="admin-actions">
              <ElButton link type="primary" :disabled="busy" @click="openEdit(row as GatewayKey)">编辑</ElButton>
              <ElDropdown trigger="click" :disabled="busy" @command="(cmd) => onRowCommand(String(cmd), row as GatewayKey)">
                <ElButton link type="primary" :disabled="busy">更多</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="revoke" :disabled="Boolean(row.revoked_at)">吊销密钥</ElDropdownItem>
                    <ElDropdownItem command="delete" divided class="key-danger-item">删除密钥</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
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
        :total="filteredKeys.length"
        layout="total, sizes, prev, pager, next"
        aria-label="Key 列表分页"
        data-testid="gateway-keys-pagination"
      />
    </div>

    <ElDrawer
      v-if="drawerOpen"
      class="gateway-drawer"
      data-testid="gateway-key-drawer"
      :model-value="true"
      :title="form.id ? '编辑 API Key' : '新增 API Key'"
      size="var(--astra-drawer-sm)"
      :close-on-click-modal="false"
      :before-close="() => requestClose()"
    >
      <ElForm ref="formRef" class="key-form" :model="form" :rules="rules" label-position="top" scroll-to-error :disabled="saving">
        <div class="key-form-row">
          <ElFormItem label="名称" prop="name">
            <ElInput v-model="form.name" maxlength="160" placeholder="例如：生产应用 Key" />
          </ElFormItem>
          <ElFormItem label="归属 ID（可选）" prop="owner_id">
            <ElInput v-model="form.owner_id" placeholder="例如：team-ops" />
          </ElFormItem>
        </div>
        <ElFormItem label="允许访问的模型" prop="allowed_models">
          <ElSelect
            v-model="form.allowed_models"
            multiple
            filterable
            allow-create
            default-first-option
            clearable
            placeholder="留空表示允许全部模型，可选择或输入模型名"
            style="width: 100%"
          >
            <ElOption v-for="model in routeModelOptions" :key="model" :value="model" :label="model" />
          </ElSelect>
        </ElFormItem>
        <div class="key-form-row key-form-row--three">
          <ElFormItem label="最大并发" prop="max_concurrency">
            <ElInput v-model="form.max_concurrency" type="number" min="0" placeholder="0 为不限" />
          </ElFormItem>
          <ElFormItem label="每小时 Token 限额" prop="hourly">
            <ElInput v-model="form.hourly" type="number" min="0" placeholder="不限" />
          </ElFormItem>
          <ElFormItem label="每日 Token 限额" prop="daily">
            <ElInput v-model="form.daily" type="number" min="0" placeholder="不限" />
          </ElFormItem>
        </div>
        <div class="key-form-row">
          <ElFormItem label="有效期至" prop="expires_at">
            <ElDatePicker
              v-model="form.expires_at"
              type="datetime"
              value-format="YYYY-MM-DDTHH:mm"
              format="YYYY-MM-DD HH:mm"
              placeholder="长期有效"
              clearable
              data-testid="gateway-key-expires"
            />
          </ElFormItem>
          <ElFormItem v-if="form.id" label="启用状态" prop="enabled">
            <ElSwitch v-model="form.enabled" data-testid="gateway-key-enabled" />
          </ElFormItem>
        </div>
        <ElAlert v-if="formError" type="error" show-icon :closable="false" :title="formError" data-testid="gateway-key-error" />
      </ElForm>
      <template #footer>
        <div class="gateway-drawer__footer">
          <ElTag v-if="dirty" type="warning" effect="light" data-testid="gateway-key-dirty">有未保存修改</ElTag>
          <span class="gateway-drawer__spacer" />
          <ElButton :disabled="saving" @click="requestClose">取消</ElButton>
          <ElButton type="primary" :loading="saving" data-testid="gateway-key-save" @click="submit">保存</ElButton>
        </div>
      </template>
    </ElDrawer>

    <ElDialog
      v-if="createdKey"
      :model-value="true"
      class="gateway-key-plaintext"
      data-testid="gateway-key-plaintext"
      title="API Key 已创建"
      width="min(560px, 92%)"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      @close="closePlaintext"
    >
      <ElAlert
        type="warning"
        show-icon
        :closable="false"
        title="请立即复制保存，系统仅保存哈希摘要，关闭弹窗后无法再次查看原文。"
      />
      <div v-if="createdKey" class="gateway-key-plaintext__row">
        <ElInput
          class="gateway-key-plaintext__value"
          :model-value="createdKey.key"
          readonly
          data-testid="gateway-key-plaintext-value"
        />
        <ElButton :icon="Copy" data-testid="gateway-key-copy" @click="copyCreatedKey">复制</ElButton>
      </div>
      <div class="gateway-key-plaintext__endpoint">
        <span>接入地址</span>
        <code>{{ endpointUrl }}</code>
      </div>
      <template #footer>
        <ElButton type="primary" data-testid="gateway-key-plaintext-done" @click="closePlaintext">我已保存</ElButton>
      </template>
    </ElDialog>
  </section>
</template>

<script setup lang="ts">
import { onBeforeRouteLeave } from 'vue-router'
import { Copy, Link2, Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch } from 'vue'
import {
  ElAlert,
  ElButton,
  ElDatePicker,
  ElDialog,
  ElDrawer,
  ElDropdown,
  ElDropdownItem,
  ElDropdownMenu,
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
import type { FormInstance, FormRules, TableInstance } from 'element-plus'
import {
  gatewayApi,
  type GatewayKey,
  type GatewayKeyCreated,
  type GatewayRoute,
} from '@/api/gateway'
import GatewayHeaderActions from '@/views/gateway/GatewayHeaderActions.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import {
  errorMessage,
  formatRelativeTime,
  formatShortDate,
  isExpired,
  tokenLimitText,
} from '@/utils/gateway'

const keys = ref<GatewayKey[]>([])
const routes = ref<GatewayRoute[]>([])
const loading = ref(false)
const error = ref('')
const saving = ref(false)
const revokingId = ref('')
const deletingId = ref('')
const busy = computed(() => loading.value || saving.value || Boolean(revokingId.value) || Boolean(deletingId.value))

const endpointUrl = computed(() => {
  if (typeof window === 'undefined') return '/v1'
  return `${window.location.origin}/v1`
})

const routeModelOptions = computed(() => {
  const set = new Set<string>()
  for (const route of routes.value) set.add(route.model)
  for (const model of form.allowed_models) set.add(model)
  return [...set].sort()
})

const drawerOpen = ref(false)
const formRef = ref<FormInstance>()
const formError = ref('')
const createdKey = ref<GatewayKeyCreated | null>(null)

const form = reactive({
  id: '',
  name: '',
  owner_id: '',
  allowed_models: [] as string[],
  max_concurrency: '0',
  hourly: '',
  daily: '',
  expires_at: '',
  enabled: true,
})

const pageSize = ref(20)
const filters = reactive({ keyword: '', status: '' })
const page = ref(1)
const tableRef = ref<TableInstance>()

const filteredKeys = computed(() => {
  const keyword = filters.keyword.trim().toLowerCase()
  return keys.value.filter((key) => {
    if (keyword && !`${key.name} ${key.key_prefix} ${key.owner_id ?? ''}`.toLowerCase().includes(keyword)) return false
    if (filters.status === 'revoked' && !key.revoked_at) return false
    if (filters.status === 'enabled' && (key.revoked_at || !key.enabled)) return false
    if (filters.status === 'disabled' && (key.revoked_at || key.enabled)) return false
    return true
  })
})
const pagedKeys = computed(() => {
  const start = (page.value - 1) * pageSize.value
  return filteredKeys.value.slice(start, start + pageSize.value)
})
const hasFilters = computed(() => Boolean(filters.keyword.trim() || filters.status))
const emptyText = computed(() => (hasFilters.value
  ? '没有匹配的 Key'
  : '暂无 API Key'))

watch(filters, () => { page.value = 1 })
watch(pageSize, () => { page.value = 1 })
watch([page, pageSize], () => { tableRef.value?.setScrollTop(0) })
watch(filteredKeys, (list) => {
  const maxPage = Math.max(1, Math.ceil(list.length / pageSize.value))
  if (page.value > maxPage) page.value = maxPage
})

function applySearch() {
  page.value = 1
  void load()
}

function clearFilters() {
  filters.keyword = ''
  filters.status = ''
}

function intOrNull(value: string): number | null {
  const text = value.trim()
  if (!text) return null
  const parsed = Number(text)
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null
}

function expiresIso(value: string): string | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}

async function copyEndpoint() {
  try {
    await navigator.clipboard.writeText(endpointUrl.value)
    notify('接入地址已复制')
  } catch {
    notify('复制失败，请手动选中复制', 'error')
  }
}

async function load() {
  loading.value = true
  error.value = ''
  try {
    const [keyList, routeList] = await Promise.all([
      gatewayApi.keys(),
      gatewayApi.routes().catch(() => [] as GatewayRoute[]),
    ])
    keys.value = keyList
    routes.value = routeList
  } catch (reason) {
    error.value = errorMessage(reason, 'Key 列表加载失败')
  } finally {
    loading.value = false
  }
}

function formSnapshot(): string {
  return JSON.stringify(form)
}
const initialSnapshot = ref('')
const dirty = computed(() => drawerOpen.value && formSnapshot() !== initialSnapshot.value)

const rules: FormRules = {
  name: [{ required: true, message: '请填写 Key 名称', trigger: 'blur' }],
}

function openCreate() {
  Object.assign(form, {
    id: '',
    name: '',
    owner_id: '',
    allowed_models: [],
    max_concurrency: '0',
    hourly: '',
    daily: '',
    expires_at: '',
    enabled: true,
  })
  formError.value = ''
  initialSnapshot.value = formSnapshot()
  drawerOpen.value = true
}

function openEdit(key: GatewayKey) {
  Object.assign(form, {
    id: key.id,
    name: key.name,
    owner_id: key.owner_id ?? '',
    allowed_models: [...key.allowed_models],
    max_concurrency: String(key.max_concurrency),
    hourly: key.hourly_token_limit?.toString() ?? '',
    daily: key.daily_token_limit?.toString() ?? '',
    expires_at: key.expires_at ? key.expires_at.slice(0, 16) : '',
    enabled: key.enabled,
  })
  formError.value = ''
  initialSnapshot.value = formSnapshot()
  drawerOpen.value = true
}

function onRowCommand(command: string, key: GatewayKey) {
  if (command === 'revoke') void revoke(key)
  else if (command === 'delete') void removeKey(key)
}

async function submit() {
  if (!(await formRef.value?.validate().catch(() => false))) return
  saving.value = true
  formError.value = ''
  try {
    const allowedModels = [...new Set(form.allowed_models.map((item) => item.trim()).filter(Boolean))]
    const body = {
      name: form.name.trim(),
      owner_id: form.owner_id.trim() || null,
      allowed_models: allowedModels,
      max_concurrency: Number(form.max_concurrency) || 0,
      hourly_token_limit: intOrNull(form.hourly),
      daily_token_limit: intOrNull(form.daily),
      expires_at: expiresIso(form.expires_at),
    }
    if (form.id) {
      await gatewayApi.updateKey(form.id, { ...body, enabled: form.enabled })
      drawerOpen.value = false
      notify('Key 已更新')
      await load()
    } else {
      const created = await gatewayApi.createKey(body)
      createdKey.value = created
      drawerOpen.value = false
      await load()
    }
  } catch (reason) {
    formError.value = errorMessage(reason, '保存 Key 失败')
  } finally {
    saving.value = false
  }
}

async function requestClose() {
  if (!drawerOpen.value) return true
  if (saving.value) return false
  if (dirty.value) {
    const confirmed = await confirmDialog({
      title: '放弃未保存修改',
      message: '当前 Key 配置尚未保存，确认放弃并关闭？',
      confirmLabel: '放弃并关闭',
      cancelLabel: '继续编辑',
      danger: true,
    })
    if (!confirmed) return false
  }
  drawerOpen.value = false
  return true
}
onBeforeRouteLeave(() => requestClose())

function closePlaintext() {
  createdKey.value = null
}

async function copyCreatedKey() {
  const value = createdKey.value?.key
  if (!value) return
  try {
    await navigator.clipboard.writeText(value)
    notify('Key 已复制到剪贴板')
  } catch {
    notify('浏览器拒绝自动复制，请手动选中复制', 'error')
  }
}

async function revoke(key: GatewayKey) {
  const confirmed = await confirmDialog({
    title: '吊销 Gateway Key',
    message: `确认吊销「${key.name}」？吊销后该 Key 立即失效且不可恢复。`,
    confirmLabel: '吊销',
    danger: true,
  })
  if (!confirmed) return
  revokingId.value = key.id
  try {
    await gatewayApi.revokeKey(key.id)
    notify('Key 已吊销')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '吊销失败'), 'error')
  } finally {
    revokingId.value = ''
  }
}

async function removeKey(key: GatewayKey) {
  const confirmed = await confirmDialog({
    title: '删除 Gateway Key',
    message: `确认删除「${key.name}」？删除后该 Key 将立即失效并从列表移除，无法恢复；历史调用和审计记录仍保留。`,
    confirmLabel: '删除',
    danger: true,
  })
  if (!confirmed) return
  deletingId.value = key.id
  try {
    await gatewayApi.deleteKey(key.id)
    notify('Key 已删除')
    await load()
  } catch (reason) {
    notify(errorMessage(reason, '删除 Key 失败'), 'error')
  } finally {
    deletingId.value = ''
  }
}

onMounted(load)
</script>

<style scoped>
.gateway-endpoint-strip {
  display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px 16px;
  padding: 10px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}
.gateway-endpoint-strip__left { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; }
.gateway-endpoint-strip__icon { color: var(--astra-text-secondary); flex: none; }
.gateway-endpoint-strip__label { color: var(--astra-text-primary); font-size: 13px; font-weight: 600; }
.gateway-endpoint-strip__url {
  max-width: 100%;
  padding: 2px 8px;
  overflow: hidden;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  font-family: var(--astra-font-mono);
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.gateway-endpoint-strip__hint { color: var(--astra-text-muted); font-size: 12px; }

.key-cell { display: grid; gap: 3px; min-width: 0; }
.key-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.key-cell__meta { display: flex; align-items: center; gap: 6px; min-width: 0; }
.key-prefix-chip {
  flex: none;
  padding: 0 6px;
  border: 1px solid var(--astra-border);
  border-radius: 4px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  line-height: 18px;
}
.key-cell__models { overflow: hidden; color: var(--astra-text-muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.key-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--astra-text-secondary); white-space: nowrap; }
.key-muted { color: var(--astra-text-muted); }
.key-expired { color: var(--astra-danger); font-weight: 500; }
.key-danger-item { color: var(--astra-danger); }

.key-form :deep(.el-form-item) { margin-bottom: 16px; }
.key-form :deep(.el-date-editor) { width: 100%; }
.key-form-row { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
.key-form-row--three { grid-template-columns: repeat(3, minmax(0, 1fr)); }

.gateway-drawer__footer { display: flex; align-items: center; gap: 8px; }
.gateway-drawer__spacer { flex: 1; }
.gateway-key-plaintext__row { display: flex; align-items: center; gap: 8px; margin-top: 12px; }
.gateway-key-plaintext__value { flex: 1; }
.gateway-key-plaintext__value :deep(input) { font-family: var(--astra-font-mono); font-size: 13px; }
.gateway-key-plaintext__endpoint {
  display: flex; align-items: center; gap: 8px;
  margin-top: 12px;
  padding: 8px 10px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  font-size: 12px;
  color: var(--astra-text-muted);
}
.gateway-key-plaintext__endpoint code { color: var(--astra-text-primary); font-family: var(--astra-font-mono); }

@media (max-width: 640px) {
  .key-form-row, .key-form-row--three { grid-template-columns: 1fr; }
}
</style>
