<template>
  <ElCard class="admin-card" data-testid="task-list-page">
    <template #header>
      <div class="admin-card__header">
        <div class="task-header-lead">
          <h2 class="admin-card__title">任务大厅</h2>
          <ElRadioGroup v-model="view" aria-label="任务大厅视图">
            <ElRadioButton value="list">任务列表</ElRadioButton>
            <ElRadioButton value="interventions">待处理</ElRadioButton>
          </ElRadioGroup>
        </div>
        <div class="admin-card__actions">
          <ElButton :icon="RefreshCw" :loading="store.loading" data-testid="task-refresh" @click="store.refresh()">刷新</ElButton>
          <ElButton type="primary" :icon="Plus" data-testid="task-create" @click="createOpen = true">创建任务</ElButton>
        </div>
      </div>
    </template>

    <InterventionPanel v-if="view === 'interventions'" />

    <div v-else class="admin-page">
      <div class="admin-page__toolbar" role="search" aria-label="任务筛选">
        <div class="admin-page__filters">
          <ElSelect v-model="filters.type" class="admin-page__select" data-testid="task-filter-type" aria-label="任务类型" placeholder="类型：全部" clearable>
            <ElOption value="ISSUE" label="问题" />
            <ElOption value="REQUIREMENT" label="需求" />
          </ElSelect>
          <ElSelect v-model="filters.category" class="admin-page__select" data-testid="task-filter-category" aria-label="任务分类" placeholder="分类：全部" clearable>
            <ElOption v-for="item in store.categories" :key="item.id" :value="item.id" :label="item.name" />
          </ElSelect>
          <ElSelect v-model="filters.status" class="admin-page__select" data-testid="task-filter-status" aria-label="任务状态" placeholder="状态：全部" clearable>
            <ElOption value="ABNORMAL" label="执行失败" />
            <ElOption v-for="item in statuses" :key="item" :value="item" :label="runtimeStatusLabel(item)" />
          </ElSelect>
          <ElSelect v-model="filters.employee" class="admin-page__select" data-testid="task-filter-employee" aria-label="Worker" placeholder="Worker：全部" clearable>
            <ElOption v-for="item in store.employees" :key="item.id" :value="item.id" :label="employeeDisplayName(item.name)" />
          </ElSelect>
          <ElSelect v-model="filters.owner" class="admin-page__select" data-testid="task-filter-owner" aria-label="责任数字员工" placeholder="责任数字员工：全部" clearable>
            <ElOption v-for="item in owners" :key="item.id" :value="item.id" :label="item.name" />
          </ElSelect>
        </div>
        <div class="admin-page__search">
          <ElInput v-model="filters.query" class="admin-page__keyword" data-testid="task-filter-keyword" aria-label="搜索任务" placeholder="搜索任务编号 / 标题" clearable @keydown.enter.prevent="applySearch">
            <template #prefix><Search :size="15" /></template>
          </ElInput>
          <ElButton type="primary" :icon="Search" data-testid="task-search" @click="applySearch">搜索</ElButton>
          <ElButton :icon="RotateCcw" data-testid="task-filter-reset" @click="resetFilters">重置</ElButton>
        </div>
      </div>

      <ElTable v-loading="store.loading" :data="paged" row-key="id" data-testid="task-table" @row-click="(row: Task) => openTask(row.id)">
        <ElTableColumn label="任务" min-width="280">
          <template #default="{ row }">
            <div class="task-title-cell">
              <ElTag v-if="isHigh(row.priority)" :type="row.priority === 'VERY_HIGH' ? 'danger' : 'warning'" effect="light">{{ priorityLabel(row.priority) }}</ElTag>
              <RouterLink class="task-title-cell__link" :to="`/tasks/${row.id}`" :title="row.title" @click.stop>{{ row.title }}</RouterLink>
            </div>
            <div class="task-title-cell__meta">{{ row.id }} · {{ taskTypeLabel(row.task_type) }} · {{ categoryName(row.category_id) }}</div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="进展" min-width="220">
          <template #default="{ row }">
            <RuntimeStatusBadge :projection="taskObservation(row as Task)" :show-detail="false" />
            <ElText v-if="extraProgress(row as Task)" class="task-status-reason" type="info" :title="extraProgress(row as Task)">{{ extraProgress(row as Task) }}</ElText>
          </template>
        </ElTableColumn>
        <ElTableColumn label="执行 Worker" width="180">
          <template #default="{ row }">
            <div class="task-who">
              <span>{{ employeeName(row.worker_id || row.employee_id) }}</span>
              <ElText type="info">{{ ownerLine(row.responsible_employee_id) }}</ElText>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="132">
          <template #default="{ row }">
            <RowMenu :aria-label="(row as Task).title" :primary="primaryFor(row as Task).label" :items="moreFor(row as Task)" @primary="runPrimary(row as Task)" @command="(command) => runCommand(row as Task, command)" />
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="store.error && !store.tasks.length" :image-size="72" description="任务数据加载失败">
            <ElText type="info">{{ store.error }}</ElText>
            <ElButton :icon="RefreshCw" @click="store.refresh()">重新加载</ElButton>
          </ElEmpty>
          <ElEmpty v-else-if="!store.loading" :image-size="72" :description="store.tasks.length ? '暂无符合条件的任务' : '暂无任务'">
            <ElButton v-if="store.tasks.length" @click="resetFilters">清除筛选</ElButton>
            <ElButton v-else type="primary" :icon="Plus" @click="createOpen = true">创建任务</ElButton>
          </ElEmpty>
        </template>
      </ElTable>

      <div class="admin-page__footer">
        <ElPagination
          v-model:current-page="currentPage"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          aria-label="任务列表分页"
          data-testid="task-pagination"
        />
      </div>
    </div>

    <TaskCreateDrawer :open="createOpen || Boolean(editTask)" :task="editTask" @close="closeEditor" @created="onTaskCreated" @saved="onTaskSaved" />

    <ElDialog
      v-if="pending"
      data-testid="task-action-confirm"
      :model-value="true"
      :title="pending.label"
      width="min(440px,100%)"
      :close-on-click-modal="false"
      :close-on-press-escape="false"
      :before-close="closePendingAction"
    >
      <ElText type="info">{{ `确认对「${shortTitle(pending.task.title)}」执行该操作？` }}</ElText>
      <template #footer>
        <ElSpace :size="8">
          <ElButton :disabled="acting" @click="pending = null">取消</ElButton>
          <ElButton type="primary" :disabled="acting" :loading="acting" @click="runPendingAction">确认{{ pending.label }}</ElButton>
        </ElSpace>
      </template>
    </ElDialog>
  </ElCard>
</template>

<script setup lang="ts">
import { Ban, Play, Plus, RefreshCw, RotateCcw, RotateCw, Search, Trash2 } from 'lucide-vue-next'
import { computed, onMounted, reactive, ref, watch, type Component } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElButton, ElCard, ElDialog, ElEmpty, ElInput, ElOption, ElPagination, ElRadioButton, ElRadioGroup, ElSelect, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import { api } from '@/api/client'
import type { Task, TaskPriority, TaskStatus } from '@/api/types'
import { listEmployees } from '../api'
import type { Employee as NexusEmployee } from '../types'
import RowMenu, { type RowMenuItem } from '@/components/astra/RowMenu.vue'
import RuntimeStatusBadge from '@/components/runtime/RuntimeStatusBadge.vue'
import InterventionPanel from '@/components/task/InterventionPanel.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { notify } from '@/composables/useToast'
import {
  TASK_RUNTIME_FILTER_STATES,
  runtimeStatusLabel,
  runtimeStatusMatchesFilter,
  type RuntimeStatusFilter,
} from '@/features/runtime/status'
import { useAstraStore } from '@/stores/astra'
import { employeeDisplayName, platformCopy, priorityLabel, taskTypeLabel } from '@/utils/format'

const store = useAstraStore()
const route = useRoute()
const router = useRouter()
const createOpen = ref(route.query.create === '1')
const view = ref<'list' | 'interventions'>(route.query.view === 'interventions' ? 'interventions' : 'list')
const editTask = ref<Task | null>(null)
const owners = ref<NexusEmployee[]>([])
const pageSize = ref(20)
const currentPage = ref(1)
type ListAction = 'start' | 'cancel' | 'rerun' | 'delete'
interface ActionItem { action: ListAction; tip: string; label: string; icon: Component; danger?: boolean; confirm?: boolean }
const pending = ref<{ task: Task; name: ListAction; label: string } | null>(null)
const acting = ref(false)

const statuses = TASK_RUNTIME_FILTER_STATES
const terminalStatuses: TaskStatus[] = ['SUCCEEDED', 'FAILED', 'CANCELED']

const filters = reactive({
  query: typeof route.query.q === 'string' ? route.query.q : '',
  type: '',
  category: '',
  status: typeof route.query.status === 'string' ? route.query.status : '',
  employee: typeof route.query.employee === 'string' ? route.query.employee : '',
  owner: typeof route.query.owner === 'string' ? route.query.owner : '',
})
watch(() => filters, () => { currentPage.value = 1 }, { deep: true })
watch(view, (value) => {
  const query = { ...route.query }
  if (value === 'interventions') query.view = 'interventions'
  else delete query.view
  void router.replace({ path: '/tasks', query })
})
watch(() => route.query.view, (value) => {
  view.value = value === 'interventions' ? 'interventions' : 'list'
})

onMounted(async () => {
  await store.refresh()
  owners.value = await listEmployees().catch(() => [])
})

const filtered = computed(() => store.tasks.filter((task) => {
  const query = filters.query.toLowerCase().trim()
  return (!query || `${task.id}${task.title}`.toLowerCase().includes(query))
    && (!filters.type || task.task_type === filters.type)
    && (!filters.category || task.category_id === filters.category)
    && runtimeStatusMatchesFilter(task.runtime_state, filters.status as RuntimeStatusFilter)
    && (!filters.employee || task.employee_id === filters.employee || task.worker_id === filters.employee)
    && (!filters.owner || task.responsible_employee_id === filters.owner)
}))

const total = computed(() => filtered.value.length)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
const paged = computed(() => filtered.value.slice((currentPage.value - 1) * pageSize.value, currentPage.value * pageSize.value))
watch(totalPages, (value) => { currentPage.value = Math.min(currentPage.value, value) })
watch(pageSize, () => { currentPage.value = 1 })

const categoryName = (id: string | null) => store.categories.find((item) => item.id === id)?.name ?? '—'
const employeeName = (id: string | null) => {
  const item = store.employees.find((employee) => employee.id === id)
  return item?.execution_environment?.name || employeeDisplayName(item?.name ?? '自动分配')
}
const ownerName = (id: string | null) => owners.value.find((item) => item.id === id)?.name ?? ''
function ownerLine(id: string | null) {
  if (!id) return '还没指定要问的人'
  const name = ownerName(id)
  return name ? `问 ${name}` : '要问的人还没对上'
}
const isHigh = (priority: TaskPriority) => ['VERY_HIGH', 'HIGH'].includes(priority)
const isTerminal = (status: TaskStatus) => terminalStatuses.includes(status)
const progressCopy = (task: Task) => platformCopy(task.runtime_detail || task.current_progress) || '—'
function extraProgress(task: Task) {
  const progress = progressCopy(task)
  const label = taskObservation(task).runtime_status_label
  if (!progress || progress === '—' || progress === label) return ''
  return progress
}
function primaryFor(task: Task): ActionItem {
  if (task.status === 'PENDING') return actionsFor(task)[0]
  if (task.status === 'FAILED') return actionsFor(task)[0]
  return { action: 'start', tip: '查看', label: '查看', icon: Play }
}
function moreFor(task: Task): RowMenuItem[] {
  const items: RowMenuItem[] = []
  if (primaryFor(task).label !== '查看') items.push({ command: 'open', label: '查看详情' })
  if (!isTerminal(task.status)) items.push({ command: 'edit', label: '编辑' })
  for (const item of actionsFor(task)) {
    if (item.label === primaryFor(task).label && item.action === primaryFor(task).action) continue
    items.push({ command: item.action, label: item.label })
  }
  return items
}
function runPrimary(task: Task) {
  if (primaryFor(task).label === '查看') openTask(task.id)
  else onAction(task, primaryFor(task))
}
function runCommand(task: Task, command: string) {
  if (command === 'open') { openTask(task.id); return }
  if (command === 'edit') { editTask.value = task; return }
  const item = actionsFor(task).find((entry) => entry.action === command)
  if (item) onAction(task, item)
}
function taskObservation(task: Task): Task {
  if (task.runtime_connection === 'UNAVAILABLE' || task.session_status === 'UNKNOWN') {
    return { ...task, runtime_state: 'WAITING', runtime_status_label: '连接异常', runtime_detail: task.runtime_error || '状态待确认' }
  }
  if (task.status === 'WAITING' && task.session_status === 'SUCCEEDED') {
    return { ...task, runtime_status_label: '本轮已完成', runtime_detail: task.human_required_reason || '执行结束，待确认' }
  }
  if (task.status === 'WAITING' && (task.session_status === 'FAILED' || task.session_status === 'INTERRUPTED' || task.execution_error)) {
    return { ...task, runtime_status_label: '执行异常', runtime_detail: task.human_required_reason || '待恢复' }
  }
  if (task.status === 'WAITING') {
    return { ...task, runtime_status_label: '等待确认', runtime_detail: task.human_required_reason || '' }
  }
  return task
}
const shortTitle = (value: string) => value.length > 24 ? `${value.slice(0, 24)}…` : value

const resetFilters = () => Object.assign(filters, { query: '', type: '', category: '', status: '', employee: '', owner: '' })
const closeEditor = () => { createOpen.value = false; editTask.value = null }
const openTask = (id: string) => router.push(`/tasks/${id}`)
function applySearch() {
  currentPage.value = 1
  if (!store.loading) void store.refresh()
}

const ACTION_ENDPOINT: Record<Exclude<ListAction, 'delete'>, string> = { start: 'start', cancel: 'stop', rerun: 'restart' }

function actionsFor(task: Task): ActionItem[] {
  if (task.status === 'PENDING') {
    return [
      { action: 'start', tip: '启动', label: '启动', icon: Play },
      { action: 'cancel', tip: '取消', label: '取消任务', icon: Ban, danger: true, confirm: true },
      { action: 'delete', tip: '删除', label: '删除任务', icon: Trash2, danger: true, confirm: true },
    ]
  }
  if (task.status === 'RUNNING' || task.status === 'WAITING') {
    return [{ action: 'cancel', tip: '取消', label: '取消任务', icon: Ban, danger: true, confirm: true }]
  }
  if (task.status === 'FAILED') {
    return [
      { action: 'rerun', tip: '重试', label: '重试', icon: RotateCw },
      { action: 'delete', tip: '删除', label: '删除任务', icon: Trash2, danger: true, confirm: true },
    ]
  }
  if (isTerminal(task.status)) {
    return [
      { action: 'rerun', tip: '重新执行', label: '重新执行', icon: RotateCw },
      { action: 'delete', tip: '删除', label: '删除任务', icon: Trash2, danger: true, confirm: true },
    ]
  }
  return []
}
function onAction(task: Task, item: ActionItem) {
  if (item.confirm) {
    pending.value = { task, name: item.action, label: item.label }
    return
  }
  void runAction(task, item.action)
}
async function dispatchAction(task: Task, action: ListAction) {
  if (action === 'delete') await api.deleteTask(task.id)
  else await api.taskAction(task.id, ACTION_ENDPOINT[action], { reason: '从任务中心操作' })
}
async function runAction(task: Task, action: ListAction) {
  try {
    await dispatchAction(task, action)
    await store.refresh()
  } catch (error) {
    notify(error instanceof Error ? error.message : '操作失败', 'error')
  }
}
function closePendingAction() {
  if (!acting.value) pending.value = null
}
async function runPendingAction() {
  if (!pending.value) return
  acting.value = true
  const { task, name } = pending.value
  try {
    await dispatchAction(task, name)
    pending.value = null
    await store.refresh()
  } catch (error) {
    // Keep the dialog open on failure so the user can retry or cancel.
    notify(error instanceof Error ? error.message : '操作失败', 'error')
  } finally {
    acting.value = false
  }
}

async function onTaskCreated(id: string) {
  closeEditor()
  await store.refresh()
  openTask(id)
}
async function onTaskSaved() {
  closeEditor()
  await store.refresh()
}
</script>

<style scoped>
/* 页面骨架、标题栏、查询工具栏、表格与分页统一走 styles/page-layout.css，
   这里只保留标题栏左侧视图切换与表格单元格内部的排版。 */
.task-header-lead { display: flex; align-items: center; gap: 16px; }
.task-title-cell { display: flex; align-items: flex-start; gap: 7px; min-width: 0; }
.task-title-cell__link {
  display: -webkit-box; overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label);
  font-weight: 700; line-height: 1.45; text-decoration: none; white-space: normal; -webkit-box-orient: vertical; -webkit-line-clamp: 2;
}
.task-title-cell__meta, .task-status-reason { display: block; margin-top: 4px; color: var(--astra-text-muted); font-size: var(--astra-font-helper); line-height: 1.45; }
.task-who { display: grid; gap: 2px; }
.task-title-cell__link:hover, .task-id-link:hover { color: var(--astra-primary); }
</style>
