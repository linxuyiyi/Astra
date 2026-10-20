<template>
  <div class="task-rail">
    <p v-if="!tasks.length" class="muted">{{ emptyText ?? "还没有任务" }}</p>
    <section v-if="active.length" class="task-rail__group">
      <h3>进行中</h3>
      <RouterLink v-for="task in active" :key="task.id" class="task-rail__card" :to="`/tasks/${task.id}`">
        <span class="task-rail__status" :data-tone="tone(task)">{{ statusText(task) }}</span>
        <span class="task-rail__title">{{ task.title }}</span>
        <span v-if="line(task)" class="task-rail__line">{{ line(task) }}</span>
        <span v-if="who(task)" class="task-rail__who">{{ who(task) }}</span>
      </RouterLink>
    </section>
    <el-collapse v-if="history.length" class="task-rail__history">
      <el-collapse-item :title="`历史 ${history.length}`" name="history">
        <RouterLink v-for="task in history" :key="task.id" class="task-rail__card task-rail__card--past" :to="`/tasks/${task.id}`">
          <span class="task-rail__status" :data-tone="tone(task)">{{ statusText(task) }}</span>
          <span class="task-rail__title">{{ task.title }}</span>
          <span v-if="line(task)" class="task-rail__line">{{ line(task) }}</span>
        </RouterLink>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { PlatformTask } from '../platform'

const props = defineProps<{
  tasks: PlatformTask[]
  names?: Record<string, string>
  hideResponsible?: boolean
  emptyText?: string
}>()

const STATUS_LABEL: Record<string, string> = {
  PENDING: '待启动',
  RUNNING: '执行中',
  WAITING: '等待处理',
  SUCCEEDED: '已完成',
  FAILED: '执行失败',
  CANCELED: '已取消',
}

const active = computed(() => props.tasks.filter((task) => !isHistory(task)).slice().sort(byRank))
const history = computed(() => props.tasks.filter((task) => isHistory(task)).slice().sort((a, b) => a.title.localeCompare(b.title, 'zh')))

function isHistory(task: PlatformTask) {
  return task.status === 'SUCCEEDED' || task.status === 'CANCELED' || (task.status === 'FAILED' && !task.human_intervention)
}

function byRank(a: PlatformTask, b: PlatformTask) {
  return rank(a) - rank(b)
}

function rank(task: PlatformTask) {
  if (task.status === 'RUNNING') return 0
  if (task.human_intervention || task.status === 'WAITING') return 1
  if (task.status === 'PENDING') return 2
  if (task.status === 'FAILED') return 3
  return 4
}

function statusText(task: PlatformTask) {
  return task.runtime_status_label || STATUS_LABEL[task.status] || task.status
}

function tone(task: PlatformTask) {
  if (task.status === 'RUNNING') return 'go'
  if (task.status === 'FAILED' || task.human_intervention) return 'stop'
  if (task.status === 'PENDING' || task.status === 'WAITING') return 'wait'
  return 'done'
}

function line(task: PlatformTask) {
  const pending = task.human_intervention?.reason?.trim()
  if (pending) return pending
  const progress = task.current_progress?.trim()
  if (!progress || progress === statusText(task)) return ''
  return progress
}

function lookup(id: string | null) {
  if (!id) return ''
  return props.names?.[id] || ''
}

function who(task: PlatformTask) {
  const worker = lookup(task.worker_id || task.employee_id)
  const owner = props.hideResponsible ? '' : lookup(task.responsible_employee_id)
  if (owner && worker && owner !== worker) return `${worker} 执行 · 问 ${owner}`
  if (worker) return `${worker} 执行`
  if (owner) return `问 ${owner}`
  return ''
}
</script>

<style scoped>
.task-rail { display: grid; gap: 12px; }
.task-rail__group h3 { margin: 0 0 8px; font-size: 13px; color: #537264; }
.task-rail__card {
  display: grid;
  gap: 4px;
  margin-bottom: 8px;
  padding: 10px 10px 11px;
  border: 1px solid #e1ece5;
  border-radius: 12px;
  background: #fbfefc;
  color: inherit;
  text-decoration: none;
}
.task-rail__card:hover { border-color: #b7dcc8; background: #f3faf6; }
.task-rail__status {
  justify-self: start;
  padding: 2px 8px;
  border-radius: 999px;
  background: #eef8f2;
  color: #147d59;
  line-height: 1.5;
}
.task-rail__status[data-tone='wait'] { background: #fff6e8; color: #9a6416; }
.task-rail__status[data-tone='stop'] { background: #fff1f1; color: #b42318; }
.task-rail__status[data-tone='done'] { background: #f1f5f2; color: #60786c; }
.task-rail__title {
  display: -webkit-box;
  overflow: hidden;
  font-weight: 700;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.task-rail__line, .task-rail__who { color: #60786c; line-height: 1.5; }
.task-rail__card--past { background: transparent; }
.task-rail__history :deep(.el-collapse-item__header) { font-size: 13px; color: #537264; }
</style>
