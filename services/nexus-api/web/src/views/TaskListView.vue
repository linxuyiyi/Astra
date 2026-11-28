<template>
  <section class="astra-page task-list-page" data-testid="task-list">
    <AstraPageHeader title="任务大厅">
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load()">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="creating = true">创建任务</el-button>
      </template>
    </AstraPageHeader>

    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="load()">重试</el-button>
    </el-alert>

    <form class="admin-page__toolbar task-toolbar" aria-label="任务筛选" @submit.prevent="applyFilters">
      <div class="status-segments" role="radiogroup" aria-label="任务状态">
        <button
          v-for="item in statusFilters"
          :key="item.value || 'ALL'"
          type="button"
          role="radio"
          class="status-segments__item"
          :class="{ 'is-active': filter === item.value }"
          :aria-checked="filter === item.value"
          @click="selectStatus(item.value)"
        >
          <span v-if="item.dot" class="status-segments__dot" :data-tone="item.dot" aria-hidden="true" />
          <span>{{ item.label }}</span>
        </button>
      </div>

      <div class="admin-page__search task-toolbar__search">
        <el-input
          v-model="search"
          class="admin-page__keyword"
          clearable
          :prefix-icon="Search"
          aria-label="任务搜索"
          placeholder="搜索任务标题或编号"
          @clear="applyFilters"
        />
        <el-button type="primary" native-type="submit">搜索</el-button>
        <el-button :disabled="!hasActiveFilters" @click="resetFilters">重置</el-button>
      </div>
    </form>

    <div class="astra-table-panel astra-table-panel--clickable">
      <el-table
        ref="tableRef"
        v-loading="loading"
        :data="tasks"
        row-key="id"
        height="100%"
        aria-label="任务列表"
        @row-click="openTask"
      >
        <el-table-column label="任务" min-width="300">
          <template #default="{ row }">
            <div class="task-cell">
              <div class="task-cell__head">
                <span
                  class="priority-dot"
                  :data-priority="row.priority"
                  :title="`优先级：${priorityLabel(row.priority)}`"
                  aria-hidden="true"
                />
                <span class="task-cell__title" :title="row.title">{{ row.title }}</span>
              </div>
              <div class="task-cell__meta">
                <span class="meta-chip">{{ taskTypeLabel(row.task_type) }}</span>
                <span v-if="categoryName(row.category_id)" class="meta-chip" :title="categoryName(row.category_id)">
                  {{ categoryName(row.category_id) }}
                </span>
                <span v-if="row.priority === 'VERY_HIGH' || row.priority === 'HIGH'" class="meta-priority" :data-priority="row.priority">
                  {{ priorityLabel(row.priority) }}优先
                </span>
                <span v-if="row.source && row.source !== 'manual'" class="meta-source">
                  {{ sourceLabel(row.source) }}
                </span>
              </div>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="状态" width="172">
          <template #default="{ row }">
            <SemanticTag :tone="semanticTone(statusTone(row.status))" :label="statusLabel(row.status)" />
          </template>
        </el-table-column>

        <el-table-column label="Worker" width="152" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="worker-cell" :class="{ 'is-muted': !row.worker_name }">
              {{ row.worker_name || (row.agent_worker_id ? '不可用' : '未分配') }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="当前进展" min-width="200" show-overflow-tooltip>
          <template #default="{ row }">
            <span class="progress-cell" :class="{ 'is-empty': !row.current_progress }">
              {{ row.current_progress || '—' }}
            </span>
          </template>
        </el-table-column>

        <el-table-column label="更新时间" width="152">
          <template #default="{ row }">
            <div class="time-cell">
              <span class="time-cell__main">{{ formatTime(row.updated_at) }}</span>
              <span v-if="row.started_at" class="time-cell__sub">
                耗时 {{ formatDuration(row.started_at, row.finished_at) }}
              </span>
            </div>
          </template>
        </el-table-column>

        <el-table-column label="操作" width="112" align="right" fixed="right">
          <template #default="{ row }">
            <RowMenu
              primary="查看"
              :aria-label="row.title"
              :items="taskActions(row)"
              :disabled="acting"
              @primary="openTask(row)"
              @command="act(row, $event)"
            />
          </template>
        </el-table-column>

        <template #empty>
          <el-empty v-if="!loading" :image-size="72" description="暂无匹配任务" />
        </template>
      </el-table>
    </div>

    <div class="admin-page__footer">
      <span v-if="lastUpdated" class="last-updated">上次更新 {{ lastUpdated }}</span>
      <el-pagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="total"
        layout="total, sizes, prev, pager, next"
        aria-label="任务分页"
      />
    </div>

    <TaskCreateDrawer
      :open="creating || !!editing"
      :task="editing"
      @close="closeDrawer"
      @created="created"
      @saved="saved"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, type TableInstance } from 'element-plus'
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import RowMenu from '@/components/astra/RowMenu.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import { useVisibleRefresh } from '@/composables/useVisibleRefresh'
import { runTaskAction, taskActions } from '@/features/tasks/actions'
import { semanticTone } from '@/features/workers/semantic'
import { useAstraStore } from '@/stores/astra'
import {
  formatDuration,
  formatTime,
  priorityLabel,
  sourceLabel,
  statusLabel,
  statusTone,
  taskTypeLabel,
} from '@/utils/format'

const router = useRouter()
const route = useRoute()
const store = useAstraStore()

const tasks = ref<Task[]>([])
const loading = ref(false)
const acting = ref(false)
const error = ref('')
const lastUpdated = ref('')
const filter = ref(String(route.query.status || ''))
const search = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const tableRef = ref<TableInstance>()
const creating = ref(false)
const editing = ref<Task>()

const statusFilters: Array<{ value: string; label: string; dot?: string }> = [
  { value: '', label: '全部' },
  { value: 'RUNNING', label: '执行中', dot: 'progress' },
  { value: 'WAITING', label: '待介入', dot: 'attention' },
  { value: 'PENDING', label: '待执行', dot: 'neutral' },
  { value: 'SUCCEEDED', label: '已完成', dot: 'success' },
  { value: 'FAILED', label: '失败', dot: 'danger' },
  { value: 'CANCELED', label: '已取消', dot: 'neutral' },
]

const categoryMap = computed(() => new Map(store.categories.map((item) => [item.id, item.name])))
const hasActiveFilters = computed(() => Boolean(filter.value || search.value.trim() || keyword.value))

function categoryName(categoryId: string | null) {
  return categoryId ? categoryMap.value.get(categoryId) ?? '' : ''
}

let request: AbortController | undefined

async function load(background = false) {
  if (background && request) return
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = !background
  const query = new URLSearchParams({
    limit: String(pageSize.value),
    offset: String((page.value - 1) * pageSize.value),
    status: filter.value || '',
    q: keyword.value,
  })
  try {
    const result = await api.taskPage(query, controller.signal)
    if (request !== controller) return
    tasks.value = result.items
    total.value = result.total
    lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
    error.value = ''
    if (!result.items.length && page.value > 1 && result.total > 0) {
      page.value = Math.max(1, Math.ceil(result.total / pageSize.value))
    }
  } catch (reason) {
    if (!controller.signal.aborted) error.value = `刷新失败：${String(reason)}`
  } finally {
    if (request === controller) {
      request = undefined
      loading.value = false
    }
  }
}

function selectStatus(value: string) {
  if (filter.value === value) return
  filter.value = value
  applyFilters()
}

function applyFilters() {
  keyword.value = search.value.trim()
  if (page.value === 1) void load()
  else page.value = 1
}

function resetFilters() {
  search.value = ''
  filter.value = ''
  applyFilters()
}

function closeDrawer() {
  creating.value = false
  editing.value = undefined
}

function created() {
  closeDrawer()
  applyFilters()
}

function saved() {
  closeDrawer()
  void load()
}

function openTask(task: Task) {
  void router.push({ name: 'task-detail', params: { id: task.id } })
}

async function act(task: Task, command: string) {
  if (command === 'workspace') {
    void router.push({ name: 'task-detail', params: { id: task.id }, query: { tab: 'workspace' } })
    return
  }
  if (command === 'edit') {
    editing.value = task
    return
  }
  acting.value = true
  try {
    if (await runTaskAction(task, command)) await load()
  } catch (reason) {
    ElMessage.error(String(reason))
    await load()
  } finally {
    acting.value = false
  }
}

watch(() => route.query.status, (value) => {
  filter.value = String(value || '')
  applyFilters()
})
watch(pageSize, () => {
  if (page.value !== 1) page.value = 1
})
watch([page, pageSize], () => {
  tableRef.value?.setScrollTop(0)
  void load()
})
onMounted(() => load())
useVisibleRefresh(() => {
  if (!(creating.value || editing.value || acting.value)) void load(true)
}, 8000)
onUnmounted(() => request?.abort())
</script>

<style scoped>
.task-toolbar {
  justify-content: space-between;
  gap: 10px 12px;
}

.status-segments {
  display: inline-flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 2px;
  padding: 3px;
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  background: var(--astra-bg-sidebar);
}

.status-segments__item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid transparent;
  border-radius: 6px;
  background: transparent;
  color: var(--astra-text-secondary);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
  transition: background-color 0.14s ease, color 0.14s ease, border-color 0.14s ease, box-shadow 0.14s ease;
}

.status-segments__item:hover {
  color: var(--astra-text-primary);
  background: rgba(255, 255, 255, 0.65);
}

.status-segments__item.is-active {
  border-color: var(--astra-border);
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  font-weight: 600;
  box-shadow: 0 1px 2px rgba(9, 9, 11, 0.05);
}

.status-segments__item:focus-visible {
  outline: 2px solid var(--astra-primary);
  outline-offset: -1px;
}

.status-segments__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  flex: none;
  background: var(--astra-neutral);
}

.status-segments__dot[data-tone='progress'] { background: var(--astra-info); }
.status-segments__dot[data-tone='attention'] { background: var(--astra-warning); }
.status-segments__dot[data-tone='success'] { background: var(--astra-success); }
.status-segments__dot[data-tone='danger'] { background: var(--astra-danger); }
.status-segments__dot[data-tone='neutral'] { background: var(--astra-neutral); }

.task-cell {
  display: grid;
  gap: 4px;
  min-width: 0;
  padding: 2px 0;
}

.task-cell__head {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}

.priority-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  flex: none;
  background: #d4d4d8;
}

.priority-dot[data-priority='VERY_HIGH'] { background: var(--astra-danger); }
.priority-dot[data-priority='HIGH'] { background: var(--astra-warning); }
.priority-dot[data-priority='MEDIUM'] { background: #a1a1aa; }
.priority-dot[data-priority='LOW'] { background: #d4d4d8; }

.task-cell__title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-primary);
  font-weight: 500;
  line-height: 1.4;
}

.task-cell__meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  padding-left: 14px;
  min-width: 0;
}

.meta-chip {
  display: inline-block;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 6px;
  height: 19px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 11.5px;
  font-weight: 500;
  line-height: 17px;
  vertical-align: middle;
}

.meta-priority {
  font-size: 11.5px;
  font-weight: 500;
  line-height: 1;
}

.meta-priority[data-priority='VERY_HIGH'] { color: var(--astra-danger); }
.meta-priority[data-priority='HIGH'] { color: var(--astra-warning); }

.meta-source {
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  line-height: 1;
}

.worker-cell {
  color: var(--astra-text-primary);
  font-variant-numeric: tabular-nums;
}

.worker-cell.is-muted {
  color: var(--astra-text-muted);
}

.progress-cell {
  color: var(--astra-text-secondary);
}

.progress-cell.is-empty {
  color: var(--astra-text-muted);
}

.time-cell {
  display: grid;
  gap: 2px;
  font-variant-numeric: tabular-nums;
}

.time-cell__main {
  color: var(--astra-text-secondary);
  font-size: 13px;
  line-height: 1.35;
}

.time-cell__sub {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  line-height: 1.25;
}

.last-updated {
  margin-right: auto;
  color: var(--astra-text-muted);
  font-size: var(--astra-font-helper);
  font-variant-numeric: tabular-nums;
}

@media (min-width: 2240px) {
  .meta-chip {
    max-width: 240px;
  }
}
</style>
