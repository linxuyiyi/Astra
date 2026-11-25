<template>
  <section class="admin-page admin-page--fill" data-testid="category-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="loading" data-testid="category-refresh" @click="load">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="category-create" @click="beginCreate">新增分类</ElButton>
    </SettingsHeaderActions>

    <ElAlert v-if="error" type="error" show-icon :closable="false" :title="error" data-testid="category-error">
      <ElButton type="primary" data-testid="category-retry" @click="load">重试</ElButton>
    </ElAlert>

    <div class="admin-page__toolbar" role="search" aria-label="任务分类筛选" data-testid="category-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="status" class="admin-page__select" data-testid="category-filter-status" aria-label="启用状态" placeholder="状态：全部" clearable>
          <ElOption value="enabled" label="已启用" />
          <ElOption value="disabled" label="已停用" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput
          v-model="keywordDraft"
          class="admin-page__keyword"
          data-testid="category-filter-keyword"
          aria-label="搜索任务分类"
          placeholder="搜索分类名称或描述"
          clearable
          @keydown.enter.prevent="applyKeyword"
          @clear="applyKeyword"
        >
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="category-search" @click="applyKeyword">搜索</ElButton>
      </div>
    </div>

    <div class="admin-page__table">
      <ElTable
        ref="tableRef"
        v-loading="loading"
        :data="paged"
        row-key="id"
        height="100%"
        aria-label="任务分类列表"
        element-loading-text="正在加载任务分类"
        data-testid="category-table"
      >
        <ElTableColumn label="分类名称" min-width="200">
          <template #default="{ row }">
            <div class="cat-cell">
              <strong :title="row.name">{{ row.name }}</strong>
              <small v-if="row.description" :title="row.description">{{ row.description }}</small>
            </div>
          </template>
        </ElTableColumn>
      <ElTableColumn label="Prompt 配置" width="180">
        <template #default="{ row }">
          <div class="prompt-pills">
            <span class="prompt-pill" :class="{ 'is-ready': Boolean(row.task_prompt?.trim()) }">
              任务 {{ row.task_prompt?.trim() ? '✓' : '—' }}
            </span>
            <span class="prompt-pill" :class="{ 'is-ready': Boolean(row.question_prompt?.trim()) }">
              问题 {{ row.question_prompt?.trim() ? '✓' : '—' }}
            </span>
          </div>
        </template>
      </ElTableColumn>
      <ElTableColumn label="排序" width="84" align="center">
        <template #default="{ row }"><span class="category-num">{{ row.sort_order }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="启用" width="84" align="center">
        <template #default="{ row }">
          <ElSwitch
            size="small"
            :model-value="row.enabled"
            :disabled="busyId === row.id"
            :aria-label="`${row.name} 启用状态`"
            @change="toggleEnabled(row as TaskCategory)"
          />
        </template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="96" align="center" fixed="right">
        <template #default="{ row }">
          <ElButton link type="primary" @click="beginEdit(row as TaskCategory)">编辑</ElButton>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="error && !categories.length" :image-size="64" description="任务分类加载失败">
          <ElButton :icon="RefreshCw" data-testid="category-empty-retry" @click="load">重新加载</ElButton>
        </ElEmpty>
        <ElEmpty v-else-if="!loading" :image-size="64" :description="categories.length ? '没有匹配的任务分类' : '暂无任务分类'" />
      </template>
    </ElTable>
    </div>

    <div class="admin-page__footer" data-testid="category-total">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filtered.length"
        layout="total, sizes, prev, pager, next"
        aria-label="任务分类分页"
        data-testid="category-pagination"
      />
    </div>

    <CategoryEditorDrawer
      ref="drawerRef"
      :open="drawerOpen"
      :category="editing"
      :default-sort-order="defaultSortOrder"
      :saving="saving"
      :error="saveError"
      @close="closeDrawer"
      @save="submitCategory"
    />
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElEmpty,
  ElInput,
  ElOption,
  ElPagination,
  ElSelect,
  ElSwitch,
  ElTable,
  ElTableColumn,
  vLoading,
} from 'element-plus'
import type { TableInstance } from 'element-plus'
import { api } from '@/api/client'
import type { TaskCategory, TaskCategoryWrite } from '@/api/types'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import CategoryEditorDrawer from '@/views/settings/CategoryEditorDrawer.vue'
import { useAstraStore } from '@/stores/astra'
import SettingsHeaderActions from './SettingsHeaderActions.vue'

const store = useAstraStore()
const pageSize = ref(20)

const categories = ref<TaskCategory[]>([])
const loading = ref(false)
const error = ref('')
const busyId = ref('')
const status = ref('')
const keywordDraft = ref('')
const keyword = ref('')
const page = ref(1)
const tableRef = ref<TableInstance>()
const drawerOpen = ref(false)
const editing = ref<TaskCategory | null>(null)
const saving = ref(false)
const saveError = ref('')
const drawerRef = ref<InstanceType<typeof CategoryEditorDrawer> | null>(null)

const filtered = computed(() => categories.value.filter((item) => {
  if (status.value && item.enabled !== (status.value === 'enabled')) return false
  const text = keyword.value.trim().toLowerCase()
  if (!text) return true
  return item.name.toLowerCase().includes(text) || (item.description ?? '').toLowerCase().includes(text)
}))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const defaultSortOrder = computed(() => Math.max(0, ...categories.value.map((item) => item.sort_order ?? 0)) + 10)

async function load() {
  loading.value = true
  error.value = ''
  try {
    categories.value = await api.categories(true)
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : '任务分类加载失败'
  } finally {
    loading.value = false
  }
}

function applyKeyword() {
  keyword.value = keywordDraft.value
  page.value = 1
}

function beginCreate() {
  editing.value = null
  saveError.value = ''
  drawerOpen.value = true
}

function beginEdit(row: TaskCategory) {
  editing.value = row
  saveError.value = ''
  drawerOpen.value = true
}

function closeDrawer() {
  drawerOpen.value = false
  editing.value = null
}

async function submitCategory(payload: TaskCategoryWrite) {
  if (saving.value) return
  const isEdit = Boolean(editing.value)
  const target = editing.value
  saving.value = true
  saveError.value = ''
  try {
    if (target) await api.updateCategory(target.id, payload)
    else await api.createCategory(payload)
    await load()
    await store.refresh()
    notify(isEdit ? '任务分类已更新' : '任务分类已创建')
    closeDrawer()
  } catch (reason) {
    saveError.value = reason instanceof Error ? reason.message : '任务分类保存失败'
  } finally {
    saving.value = false
  }
}

onBeforeRouteLeave(async () => {
  if (!drawerOpen.value) return true
  if (saving.value) return false
  if (!drawerRef.value?.getDirty()) return true
  return confirmDialog({
    title: '放弃未保存的分类修改？',
    message: '离开配置中心后未保存的分类修改将丢失。',
    confirmLabel: '放弃并离开',
    cancelLabel: '继续编辑',
    danger: true,
  })
})

async function toggleEnabled(row: TaskCategory) {
  if (busyId.value) return
  busyId.value = row.id
  try {
    await api.updateCategory(row.id, {
      name: row.name,
      description: row.description ?? '',
      enabled: !row.enabled,
      sort_order: row.sort_order,
      task_prompt: row.task_prompt,
      question_prompt: row.question_prompt,
    })
    await load()
    await store.refresh()
    notify(row.enabled ? '分类已停用' : '分类已启用')
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : '操作失败', 'error')
  } finally {
    busyId.value = ''
  }
}

watch(pageSize, () => { page.value = 1 })
watch(totalPages, (value) => { page.value = Math.min(page.value, value) })
watch([status, keyword], () => { page.value = 1 })
watch([page, pageSize], () => { tableRef.value?.setScrollTop(0) })

onMounted(load)
</script>

<style scoped>
.cat-cell { display: grid; gap: 2px; min-width: 0; }
.cat-cell strong {
  overflow: hidden;
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.cat-cell small { color: var(--astra-text-muted); font-size: 12px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.prompt-pills { display: flex; align-items: center; gap: 6px; }
.prompt-pill {
  padding: 1px 7px; border-radius: 5px;
  border: 1px solid var(--astra-border); background: var(--astra-bg-subtle);
  color: var(--astra-text-muted); font-size: 12px; line-height: 18px;
}
.prompt-pill.is-ready {
  border-color: color-mix(in srgb, var(--astra-success) 30%, transparent);
  background: var(--astra-success-soft);
  color: var(--astra-success);
  font-weight: 500;
}
.category-num { font-size: 13px; font-variant-numeric: tabular-nums; color: var(--astra-text-secondary); }
</style>
