<template>
  <div class="task-rail">
    <div v-if="!tasks.length" class="task-rail__empty">
      <ListTodo :size="18" :stroke-width="1.6" />
      <span>{{ emptyText ?? '暂无任务' }}</span>
    </div>

    <section v-if="active.length" class="task-rail__group">
      <div class="task-rail__group-head">
        <h3>进行中</h3>
        <span class="task-rail__count">{{ active.length }}</span>
      </div>
      <div class="task-rail__list">
        <RouterLink
          v-for="task in active"
          :key="task.id"
          class="task-rail__card"
          :data-tone="tone(task)"
          :to="`/tasks/${task.id}`"
        >
          <div class="task-rail__card-top">
            <span class="task-rail__status" :data-tone="tone(task)">
              <i class="task-rail__dot" aria-hidden="true" />
              {{ statusText(task) }}
            </span>
            <ArrowUpRight class="task-rail__arrow" :size="13" aria-hidden="true" />
          </div>
          <span class="task-rail__title">{{ task.title }}</span>
          <span
            v-if="line(task)"
            class="task-rail__line"
            :class="{ 'is-warning': task.status === 'WAITING' }"
          >{{ line(task) }}</span>
          <span v-if="who(task)" class="task-rail__who">{{ who(task) }}</span>
        </RouterLink>
      </div>
    </section>

    <el-collapse v-if="history.length" class="task-rail__history">
      <el-collapse-item name="history">
        <template #title>
          <span class="task-rail__history-title">
            历史记录
            <span class="task-rail__count">{{ history.length }}</span>
          </span>
        </template>
        <div class="task-rail__list task-rail__list--past">
          <RouterLink
            v-for="task in history"
            :key="task.id"
            class="task-rail__card task-rail__card--past"
            :to="`/tasks/${task.id}`"
          >
            <div class="task-rail__card-top">
              <span class="task-rail__status" :data-tone="tone(task)">
                <i class="task-rail__dot" aria-hidden="true" />
                {{ statusText(task) }}
              </span>
              <ArrowUpRight class="task-rail__arrow" :size="13" aria-hidden="true" />
            </div>
            <span class="task-rail__title">{{ task.title }}</span>
            <span v-if="line(task)" class="task-rail__line">{{ line(task) }}</span>
          </RouterLink>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, ListTodo } from 'lucide-vue-next'
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { PlatformTask } from '../platform'
import { runtimeStatusLabel, runtimeStatusTone } from '../features/runtime/status'

const props = defineProps<{
  tasks: PlatformTask[]
  names?: Record<string, string>
  hideResponsible?: boolean
  emptyText?: string
}>()

const active = computed(() =>
  props.tasks.filter((task) => !isHistory(task)).slice().sort(byRank),
)

const history = computed(() =>
  props.tasks
    .filter((task) => isHistory(task))
    .slice()
    .sort((a, b) => {
      const timeA = a.finished_at || a.updated_at || ''
      const timeB = b.finished_at || b.updated_at || ''
      if (timeA && timeB && timeA !== timeB) return timeB.localeCompare(timeA)
      return a.title.localeCompare(b.title, 'zh')
    }),
)

function isHistory(task: PlatformTask) {
  return ['SUCCEEDED', 'CANCELED', 'FAILED'].includes(task.status)
}

function byRank(a: PlatformTask, b: PlatformTask) {
  return rank(a) - rank(b)
}

function rank(task: PlatformTask) {
  if (task.status === 'RUNNING') return 0
  if (task.status === 'WAITING') return 1
  if (task.status === 'PENDING') return 2
  return 4
}

function statusText(task: PlatformTask) {
  return runtimeStatusLabel(task.status)
}

function tone(task: PlatformTask) {
  return runtimeStatusTone(task.status)
}

function line(task: PlatformTask) {
  const detail =
    task.status === 'WAITING'
      ? task.waiting_reason?.trim() || task.current_progress?.trim()
      : task.current_progress?.trim()
  if (!detail || detail === statusText(task)) return ''
  return detail
}

function lookup(id: string | null) {
  if (!id) return ''
  return props.names?.[id] || ''
}

function who(task: PlatformTask) {
  const worker = lookup(task.agent_worker_id) || task.worker_name || ''
  const owner = props.hideResponsible ? '' : lookup(task.origin_employee_id)
  if (owner && worker && owner !== worker) return `${worker} · 负责 ${owner}`
  if (worker) return worker
  if (owner) return `负责 ${owner}`
  return ''
}
</script>

<style scoped>
.task-rail {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.task-rail__empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 28px 16px;
  border: 1px dashed var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.task-rail__group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.task-rail__group-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 2px;
}

.task-rail__group-head h3 {
  margin: 0;
  color: var(--astra-text-muted);
  font-size: 11.5px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.task-rail__count {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-weight: 500;
}

.task-rail__list {
  display: flex;
  flex-direction: column;
  gap: 7px;
}

.task-rail__card {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 11px;
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  background: var(--astra-bg-panel);
  color: inherit;
  text-decoration: none;
  transition: border-color 0.15s ease, background-color 0.15s ease, box-shadow 0.15s ease;
}

.task-rail__card:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-subtle);
  box-shadow: 0 1px 3px rgba(9, 9, 11, 0.04);
}

.task-rail__card[data-tone='warning'] {
  border-color: color-mix(in srgb, var(--astra-warning) 28%, var(--astra-border));
}

.task-rail__card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.task-rail__status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
  font-size: 11px;
  font-weight: 500;
  line-height: 1.45;
  white-space: nowrap;
}

.task-rail__dot {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
  flex: none;
}

.task-rail__status[data-tone='info'] {
  background: var(--astra-info-soft);
  color: var(--astra-info);
}

.task-rail__status[data-tone='warning'] {
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.task-rail__status[data-tone='success'] {
  background: var(--astra-success-soft);
  color: var(--astra-success);
}

.task-rail__status[data-tone='danger'] {
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
}

.task-rail__status[data-tone='neutral'] {
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
}

.task-rail__arrow {
  color: var(--astra-text-muted);
  opacity: 0;
  transition: opacity 0.15s ease, transform 0.15s ease, color 0.15s ease;
}

.task-rail__card:hover .task-rail__arrow {
  opacity: 1;
  color: var(--astra-text-primary);
  transform: translate(1px, -1px);
}

.task-rail__title {
  display: -webkit-box;
  overflow: hidden;
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 600;
  line-height: 1.45;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.task-rail__line {
  display: -webkit-box;
  overflow: hidden;
  color: var(--astra-text-secondary);
  font-size: 12px;
  line-height: 1.5;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}

.task-rail__line.is-warning {
  color: var(--astra-warning);
}

.task-rail__who {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-muted);
  font-size: 11.5px;
  line-height: 1.4;
}

.task-rail__card--past {
  padding: 8px 10px;
  border-color: var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.task-rail__card--past .task-rail__title {
  font-weight: 500;
  color: var(--astra-text-secondary);
}

.task-rail__card--past:hover {
  background: var(--astra-bg-panel);
  border-color: var(--astra-border);
}

.task-rail__card--past:hover .task-rail__title {
  color: var(--astra-text-primary);
}

.task-rail__history {
  border-top: 1px solid var(--astra-border-subtle);
  border-bottom: 0;
  --el-collapse-border-color: transparent;
  --el-collapse-header-bg-color: transparent;
  --el-collapse-content-bg-color: transparent;
  --el-collapse-header-height: 36px;
}

.task-rail__history-title {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.task-rail__history :deep(.el-collapse-item__header) {
  padding: 0 2px;
  border-bottom: 0;
}

.task-rail__history :deep(.el-collapse-item__wrap) {
  border-bottom: 0;
}

.task-rail__history :deep(.el-collapse-item__content) {
  padding: 4px 0 0;
}
</style>
