<template>
  <section class="astra-page" data-testid="task-list">
    <AstraPageHeader title="任务大厅" description="查看任务进展、处理执行并确认验收">
      <template #actions>
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="creating = true">创建任务</el-button>
      </template>
    </AstraPageHeader>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="load">重试</el-button>
    </el-alert>
    <form class="admin-page__toolbar" aria-label="任务筛选" @submit.prevent="applyFilters">
      <el-select v-model="filter" class="admin-page__select" aria-label="任务状态" clearable placeholder="状态：全部" @change="applyFilters">
        <el-option v-for="state in statuses" :key="state" :value="state" :label="statusLabel(state)" />
      </el-select>
      <div class="admin-page__search">
        <el-input v-model="search" class="admin-page__keyword" clearable :prefix-icon="Search" aria-label="任务搜索" placeholder="搜索任务编号或标题" @clear="applyFilters" />
        <el-button type="primary" native-type="submit">搜索</el-button>
        <el-button @click="resetFilters">重置</el-button>
      </div>
    </form>
    <div class="astra-table-panel astra-table-panel--clickable">
      <el-table v-loading="loading" :data="tasks" row-key="id" height="100%" aria-label="任务列表" @row-click="openTask">
        <el-table-column prop="title" label="任务" min-width="220" show-overflow-tooltip />
        <el-table-column label="状态" width="108">
          <template #default="{ row }"><el-tag :type="statusTagType(row.status)">{{ statusLabel(row.status) }}</el-tag></template>
        </el-table-column>
        <el-table-column label="Worker" min-width="145" show-overflow-tooltip>
          <template #default="{ row }">{{ row.worker_name || (row.agent_worker_id ? '不可用' : '未分配') }}</template>
        </el-table-column>
        <el-table-column prop="current_progress" label="进展" min-width="210" show-overflow-tooltip />
        <el-table-column label="更新时间" width="168">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="128" align="right" fixed="right">
          <template #default="{ row }">
            <RowMenu primary="查看" :aria-label="row.title" :items="taskActions(row)" :disabled="acting" @primary="openTask(row)" @command="act(row, $event)" />
          </template>
        </el-table-column>
        <template #empty><el-empty v-if="!loading" :image-size="72" description="暂无匹配任务" /></template>
      </el-table>
    </div>
    <div class="admin-page__footer">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10, 20, 50]" :total="total" layout="total, sizes, prev, pager, next" aria-label="任务分页" />
    </div>
    <TaskCreateDrawer :open="creating || !!editing" :task="editing" @close="closeDrawer" @created="created" @saved="saved" />
  </section>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import RowMenu from '@/components/astra/RowMenu.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { api } from '@/api/client'
import type { Task } from '@/api/types'
import { runTaskAction, taskActions } from '@/features/tasks/actions'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'

const router = useRouter()
const route = useRoute()
const tasks = ref<Task[]>([])
const loading = ref(false)
const acting = ref(false)
const error = ref('')
const filter = ref(String(route.query.status || ''))
const search = ref('')
const keyword = ref('')
const page = ref(1)
const pageSize = ref(20)
const total = ref(0)
const creating = ref(false)
const editing = ref<Task>()
const statuses = ['PENDING', 'RUNNING', 'WAITING', 'SUCCEEDED', 'CANCELED']
let request: AbortController | undefined

async function load() {
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = true
  error.value = ''
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
  } catch (reason) {
    if (!controller.signal.aborted) error.value = String(reason)
  } finally {
    if (request === controller) loading.value = false
  }
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
watch([page, pageSize], load)
onMounted(load)
onUnmounted(() => request?.abort())
</script>
