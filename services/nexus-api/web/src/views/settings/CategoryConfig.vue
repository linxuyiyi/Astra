<template>
  <section class="admin-page" data-testid="category-page">
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
          placeholder="搜索分类名称 / 描述"
          clearable
          @keydown.enter.prevent="applyKeyword"
          @clear="applyKeyword"
        >
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="category-search" @click="applyKeyword">搜索</ElButton>
      </div>
    </div>

    <ElTable
      v-loading="loading"
      :data="paged"
      row-key="id"
      aria-label="任务分类列表"
      element-loading-text="正在加载任务分类"
      data-testid="category-table"
    >
      <ElTableColumn label="分类名称" prop="name" min-width="150" show-overflow-tooltip />
      <ElTableColumn label="描述" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">{{ row.description || '—' }}</template>
      </ElTableColumn>
      <ElTableColumn label="排序" width="84">
        <template #default="{ row }"><span class="category-num">{{ row.sort_order }}</span></template>
      </ElTableColumn>
      <ElTableColumn label="启用" width="96">
        <template #default="{ row }">
          <ElTag :type="row.enabled ? 'success' : 'info'" :effect="row.enabled ? 'light' : 'plain'">{{ row.enabled ? '已启用' : '已停用' }}</ElTag>
        </template>
      </ElTableColumn>
      <ElTableColumn label="提示词" min-width="220" show-overflow-tooltip>
        <template #header>
          <span>提示词 <ElTooltip content="任务提示词用于需求，问题提示词用于问题。没配的时候 Worker 只知道分类名称，不知道该怎么做。"><button type="button" class="category-help">说明</button></ElTooltip></span>
        </template>
        <template #default="{ row }">{{ promptSummary(row as TaskCategory) }}</template>
      </ElTableColumn>
      <ElTableColumn label="操作" width="150" align="center">
        <template #default="{ row }">
          <div class="admin-actions">
            <ElButton link type="primary" @click="beginEdit(row as TaskCategory)">编辑</ElButton>
            <ElButton
              link
              :type="row.enabled ? 'warning' : 'primary'"
              :disabled="busyId === row.id"
              @click="toggleEnabled(row as TaskCategory)"
            >{{ row.enabled ? '停用' : '启用' }}</ElButton>
          </div>
        </template>
      </ElTableColumn>
      <template #empty>
        <ElEmpty v-if="error && !categories.length" :image-size="72" description="任务分类加载失败">
          <ElText type="info">{{ error }}</ElText>
          <ElButton :icon="RefreshCw" data-testid="category-empty-retry" @click="load">重新加载</ElButton>
        </ElEmpty>
        <ElEmpty v-else-if="!loading" :image-size="72" :description="categories.length ? '没有匹配的任务分类' : '暂无任务分类'" />
      </template>
    </ElTable>

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
  ElTable,
  ElTableColumn,
  ElTag,
  ElText,
  ElTooltip,
  vLoading,
} from 'element-plus'
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
// 新分类默认排在现有分类之后，与旧的行内新增保持同一套排序语义。
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

function promptSummary(row: TaskCategory) {
  const task = Boolean(row.task_prompt?.trim())
  const question = Boolean(row.question_prompt?.trim())
  if (task && question) return '任务和问题提示词都已配置'
  if (task) return '已有任务提示词，还缺问题提示词'
  if (question) return '已有问题提示词，还缺任务提示词'
  return '提示词还没配置'
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
    // 分类在任务、员工与自动分配里被引用，保存后同步刷新平台缓存。
    await store.refresh()
    notify(isEdit ? '任务分类已更新' : '任务分类已创建')
    closeDrawer()
  } catch (reason) {
    saveError.value = reason instanceof Error ? reason.message : '任务分类保存失败'
  } finally {
    saving.value = false
  }
}

// 抽屉内未保存的内容在离开配置中心时同样需要确认，避免整页导航绕过抽屉的关闭确认。
// 判定复用抽屉自身的 dirty，而不是「抽屉是否打开」：没改过就不打扰用户。
onBeforeRouteLeave(async () => {
  if (!drawerRef.value?.getDirty()) return true
  const confirmed = await confirmDialog({
    title: '放弃未保存的分类修改？',
    message: '离开配置中心后未保存的分类修改将丢失。',
    confirmLabel: '放弃并离开',
    danger: true,
  })
  return confirmed
})

// 启停是列表级即时操作：只改 enabled 并立即落库，其余字段按服务端返回值原样带回。
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
    // 分类在任务、员工与自动分配里被引用，启停后同步刷新平台缓存。
    await store.refresh()
    notify(row.enabled ? '分类已停用' : '分类已启用')
  } catch (reason) {
    notify(reason instanceof Error ? reason.message : '操作失败', 'error')
  } finally {
    busyId.value = ''
  }
}

// 分类已整表加载，翻页只切当前页；每页条数变化回到第 1 页。
watch(pageSize, () => { page.value = 1 })
watch(totalPages, (value) => { page.value = Math.min(page.value, value) })
watch([status, keyword], () => { page.value = 1 })

onMounted(load)
</script>

<style scoped>
/* 页面骨架、标题栏操作、查询工具栏、表格与分页统一走 styles/page-layout.css，
   这里只保留表格单元格内部的排版。 */
.category-num { font-size: var(--astra-font-helper); font-variant-numeric: tabular-nums; }
.category-help { padding: 0; border: 0; background: transparent; color: var(--astra-primary); cursor: pointer; }
</style>
