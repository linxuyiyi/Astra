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
              <i class="task-rail__dot" :class="{ 'is-pulse': task.status === 'RUNNING' || task.status === 'WAITING' }" aria-hidden="true" />
              {{ statusText(task) }}
            </span>
            <span v-if="timeLabel(task)" class="task-rail__time">{{ timeLabel(task) }}</span>
            <span class="task-rail__actions">
              <button
                type="button"
                class="task-rail__ext"
                title="新窗口打开任务详情"
                aria-label="新窗口打开任务详情"
                @click.stop.prevent="openExternal(task.id)"
              >
                <ExternalLink :size="12" aria-hidden="true" />
              </button>
              <ArrowUpRight class="task-rail__arrow" :size="13" aria-hidden="true" />
            </span>
          </div>

          <span class="task-rail__title">
            <i
              v-if="task.priority === 'HIGH' || task.priority === 'VERY_HIGH'"
              class="task-rail__prio"
              :class="task.priority === 'VERY_HIGH' ? 'is-urgent' : 'is-high'"
              :title="task.priority === 'VERY_HIGH' ? '很高优先级' : '高优先级'"
              aria-hidden="true"
            />
            {{ task.title }}
          </span>

          <div
            v-if="line(task)"
            class="task-rail__line"
            :class="{ 'is-warning': task.status === 'WAITING' }"
          >{{ line(task) }}</div>

          <div class="task-rail__foot">
            <span v-if="workerInfo(task).label" class="task-rail__worker">
              <i
                class="task-rail__worker-dot"
                :class="workerInfo(task).online === true ? 'is-on' : workerInfo(task).online === false ? 'is-off' : ''"
                aria-hidden="true"
              />
              <span class="task-rail__worker-name">{{ workerInfo(task).label }}</span>
              <span v-if="workerInfo(task).online === false" class="task-rail__worker-off">离线</span>
            </span>
            <span v-else class="task-rail__worker task-rail__worker--unassigned">待分配 Worker</span>
            <code class="task-rail__id" :title="task.id">{{ shortTaskId(task.id) }}</code>
          </div>
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
              <span v-if="timeLabel(task)" class="task-rail__time">{{ timeLabel(task) }}</span>
              <ArrowUpRight class="task-rail__arrow" :size="13" aria-hidden="true" />
            </div>
            <span class="task-rail__title">{{ task.title }}</span>
            <span v-if="line(task)" class="task-rail__line">{{ line(task) }}</span>
            <div v-if="workerInfo(task).label" class="task-rail__foot">
              <span class="task-rail__worker">
                <span class="task-rail__worker-name">{{ workerInfo(task).label }}</span>
              </span>
              <code class="task-rail__id" :title="task.id">{{ shortTaskId(task.id) }}</code>
            </div>
          </RouterLink>
        </div>
      </el-collapse-item>
    </el-collapse>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, ExternalLink, ListTodo } from 'lucide-vue-next'
import { computed } from 'vue'
import { RouterLink } from 'vue-router'
import type { PlatformTask } from '../platform'
import { runtimeStatusLabel, runtimeStatusTone } from '../features/runtime/status'

const props = defineProps<{
  tasks: PlatformTask[]
  names?: Record<string, string>
  workerOnlineById?: Record<string, boolean | null>
  hideResponsible?: boolean
  emptyText?: string
}>()

const WAITING_REASON_LABELS: Record<string, string> = {
  question: '执行端提出问题，等待答复',
  permission: '等待工具或命令执行授权',
  confirmation: '回合结束，等待确认验收',
  manual_intervene: '已转人工介入处理',
  paused: '任务已暂停',
  session_missing: '远端执行会话丢失，等待恢复',
  worker_offline: '关联 Worker 当前离线',
  delivery_uncertain: '指令投递状态待确认',
  reconcile_timeout: '执行状态对账超时',
}

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
  if (task.status === 'WAITING') return 0
  if (task.status === 'RUNNING') return 1
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
  if (task.status === 'WAITING') {
    const rawReason = task.waiting_reason?.trim()
    const mapped = rawReason ? WAITING_REASON_LABELS[rawReason] || rawReason : ''
    const progress = task.current_progress?.trim()
    if (mapped && progress && progress !== mapped && progress !== statusText(task)) {
      return `${mapped} · ${progress}`
    }
    return mapped || (progress && progress !== statusText(task) ? progress : '')
  }
  const detail = task.current_progress?.trim()
  if (!detail || detail === statusText(task)) return ''
  return detail
}

function lookup(id: string | null) {
  if (!id) return ''
  return props.names?.[id] || ''
}

function workerInfo(task: PlatformTask): { label: string; online: boolean | null } {
  const workerName = lookup(task.agent_worker_id) || task.worker_name || ''
  const owner = props.hideResponsible ? '' : lookup(task.origin_employee_id)
  const online =
    task.worker_online !== undefined && task.worker_online !== null
      ? task.worker_online
      : task.agent_worker_id
        ? (props.workerOnlineById?.[task.agent_worker_id] ?? null)
        : null
  if (owner && workerName && owner !== workerName) return { label: `${workerName} · 负责 ${owner}`, online }
  if (workerName) return { label: workerName, online }
  if (owner) return { label: `负责 ${owner}`, online: null }
  return { label: '', online: null }
}

function shortTaskId(id: string): string {
  const parts = id.split('-')
  return parts.length >= 3 ? `#${parts[parts.length - 1]}` : id.slice(-8)
}

function timeLabel(task: PlatformTask): string {
  const now = Date.now()
  if (task.status === 'RUNNING' && task.started_at) {
    const started = Date.parse(task.started_at)
    if (Number.isFinite(started)) {
      const sec = Math.max(0, Math.floor((now - started) / 1000))
      if (sec < 60) return `已运行 ${sec}s`
      const min = Math.floor(sec / 60)
      if (min < 60) return `已运行 ${min}m`
      return `已运行 ${Math.floor(min / 60)}h${min % 60}m`
    }
  }
  const raw = task.finished_at || task.last_activity_at || task.updated_at || task.created_at
  if (!raw) return ''
  const ts = Date.parse(raw)
  if (!Number.isFinite(ts)) return ''
  const diffSec = Math.max(0, Math.floor((now - ts) / 1000))
  if (diffSec < 60) return '刚刚'
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)} 分钟前`
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)} 小时前`
  return `${Math.floor(diffSec / 86400)} 天前`
}

function openExternal(id: string) {
  window.open(`/tasks/${encodeURIComponent(id)}`, '_blank', 'noopener')
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
  border-color: color-mix(in srgb, var(--astra-warning) 35%, var(--astra-border));
  background: color-mix(in srgb, #fffbeb 42%, var(--astra-bg-panel));
}

.task-rail__card-top {
  display: flex;
  align-items: center;
  gap: 6px;
}

.task-rail__time {
  margin-left: auto;
  color: var(--astra-text-muted);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.task-rail__actions {
  display: inline-flex;
  align-items: center;
  gap: 2px;
}

.task-rail__ext {
  display: inline-grid;
  place-items: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: transparent;
  color: var(--astra-text-muted);
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.14s ease, background-color 0.14s ease, color 0.14s ease;
}

.task-rail__card:hover .task-rail__ext,
.task-rail__card:focus-within .task-rail__ext {
  opacity: 1;
}

.task-rail__ext:hover {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
}

.task-rail__dot.is-pulse {
  animation: task-rail-pulse 1.8s ease-in-out infinite;
}

@keyframes task-rail-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.35; }
}

.task-rail__prio {
  display: inline-block;
  width: 6px;
  height: 6px;
  margin-right: 5px;
  border-radius: 50%;
  vertical-align: middle;
}

.task-rail__prio.is-urgent { background: var(--astra-danger); }
.task-rail__prio.is-high { background: var(--astra-warning); }

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
  padding: 5px 8px;
  border-radius: 6px;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  font-size: 11.5px;
}

.task-rail__foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding-top: 2px;
}

.task-rail__worker {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
  color: var(--astra-text-muted);
  font-size: 11.5px;
  line-height: 1.4;
}

.task-rail__worker--unassigned {
  font-style: normal;
  opacity: 0.78;
}

.task-rail__worker-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-rail__worker-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--astra-neutral);
  flex: none;
}

.task-rail__worker-dot.is-on { background: var(--astra-success); }
.task-rail__worker-dot.is-off { background: var(--astra-danger); }

.task-rail__worker-off {
  flex: none;
  padding: 0 4px;
  border-radius: 4px;
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
  font-size: 10.5px;
  line-height: 1.45;
}

.task-rail__id {
  flex: none;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 10.5px;
  font-variant-numeric: tabular-nums;
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
