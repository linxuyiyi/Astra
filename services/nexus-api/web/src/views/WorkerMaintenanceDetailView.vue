<template>
  <section class="op-detail-page" data-testid="worker-maintenance-detail">
    <ElAlert v-if="error" :title="error" type="error" show-icon :closable="false">
      <ElButton size="small" @click="observe">重试读取</ElButton>
    </ElAlert>

    <!-- 顶部身份与操作头 -->
    <section class="op-identity">
      <div class="op-identity__main">
        <RouterLink class="op-back" :to="{ name: 'settings-worker-detail', params: { id: workerId } }">
          <ArrowLeft :size="14" aria-hidden="true" />
          <span>{{ worker?.name || '返回 Worker' }}</span>
        </RouterLink>

        <div class="op-identity__title">
          <h2>{{ operation ? maintenanceActions[operation.action] : '运维操作' }}</h2>
          <SemanticTag
            v-if="operation"
            :tone="operation.stage === 'stop_unconfirmed' ? 'attention' : maintenanceSemanticTone(operation.state)"
            :label="operation.stage === 'stop_unconfirmed' ? '停止待确认' : maintenanceStates[operation.state]"
          />
          <ElTag v-if="operation && maintenanceActive(operation.state) && operation.cancel_requested" size="small" type="warning">
            正在取消
          </ElTag>
          <ElTag v-if="operation" size="small" type="info" class="astra-tag--neutral">
            第 {{ operation.attempt }} 次
          </ElTag>
          <span v-if="operation" class="op-id-pill">
            <IdDisplay :value="operation.id" :max-width="18" />
          </span>
        </div>

        <div v-if="worker || operation?.execution_reason" class="op-identity__sub">
          <span v-if="worker?.host" class="op-mono">{{ worker.host }}</span>
          <span v-if="worker?.host && operation?.execution_reason" class="op-sub-sep" aria-hidden="true">·</span>
          <span v-if="operation?.execution_reason">{{ operation.execution_reason }}</span>
        </div>
      </div>

      <div v-if="operation" class="op-identity__actions">
        <ElButton
          v-if="maintenanceActive(operation.state) && !operation.cancel_requested"
          :icon="Square"
          :loading="acting"
          @click="act('cancel')"
        >
          取消操作
        </ElButton>
        <ElButton
          v-if="operation.stage === 'stop_unconfirmed'"
          type="danger"
          plain
          :icon="ShieldAlert"
          :loading="acting"
          @click="act('confirm')"
        >
          确认远端已停止
        </ElButton>
        <ElButton
          v-else-if="operation.state === 'FAILED' || operation.state === 'CANCELED'"
          type="primary"
          :icon="RotateCw"
          :loading="acting"
          @click="act('retry')"
        >
          重试操作
        </ElButton>
      </div>
    </section>

    <!-- 操作摘要指标条 -->
    <section v-if="operation" class="op-summary-grid" aria-label="操作摘要">
      <div class="op-metric">
        <span class="op-metric__label">执行耗时</span>
        <strong class="op-metric__value op-mono">{{ durationText }}</strong>
        <span class="op-metric__hint">阶段：{{ stageLabel(operation.stage) }}</span>
      </div>
      <div class="op-metric">
        <span class="op-metric__label">开始时间</span>
        <strong class="op-metric__value op-mono">{{ formatDateTimeSec(operation.started_at) }}</strong>
        <span class="op-metric__hint op-mono">发起 {{ formatDateTimeSec(operation.requested_at) }}</span>
      </div>
      <div class="op-metric">
        <span class="op-metric__label">结束时间</span>
        <strong class="op-metric__value op-mono">{{ formatDateTimeSec(operation.finished_at) }}</strong>
        <span class="op-metric__hint">
          {{ maintenanceActive(operation.state) ? '正在执行' : maintenanceStates[operation.state] }}
        </span>
      </div>
      <div class="op-metric">
        <span class="op-metric__label">目标与触发</span>
        <strong class="op-metric__value">{{ worker?.name || operation.agent_worker_id }}</strong>
        <span class="op-metric__hint">触发人 {{ operation.requested_by || '—' }}</span>
      </div>
    </section>

    <!-- 异常 / 待确认提示 -->
    <section
      v-if="operation?.error_message"
      class="op-callout"
      :class="operation.stage === 'stop_unconfirmed' ? 'op-callout--warning' : 'op-callout--danger'"
      role="alert"
    >
      <div class="op-callout__head">
        <AlertTriangle v-if="operation.stage === 'stop_unconfirmed'" :size="16" aria-hidden="true" />
        <XCircle v-else :size="16" aria-hidden="true" />
        <span>{{ operation.stage === 'stop_unconfirmed' ? '远端进程停止待确认' : '执行异常' }}</span>
      </div>
      <MarkdownView
        v-if="isRichText(operation.error_message)"
        :content="redactMaintenanceText(operation.error_message)"
        compact
      />
      <p v-else class="op-callout__text">{{ redactMaintenanceText(operation.error_message) }}</p>
    </section>

    <!-- 阶段进度与步骤时间线 -->
    <section v-if="operation" class="op-timeline-card" aria-label="执行阶段与步骤">
      <header class="op-card-head">
        <div class="op-card-head__title">
          <h3>阶段与步骤</h3>
          <span v-if="timelineSteps.length" class="op-card-head__meta">{{ timelineSteps.length }} 项</span>
        </div>
        <div v-if="snapshotChips.length" class="op-snapshot-chips">
          <span v-for="chip in snapshotChips" :key="chip.label" class="op-chip" :class="{ 'op-mono': chip.mono }">
            {{ chip.label }}
          </span>
        </div>
      </header>

      <ol class="op-stages">
        <li v-for="stage in lifecycleStages" :key="stage.key" class="op-stage" :data-state="stage.status">
          <span class="op-stage__dot" aria-hidden="true">
            <Check v-if="stage.status === 'done'" :size="11" />
            <X v-else-if="stage.status === 'failed'" :size="11" />
            <AlertTriangle v-else-if="stage.status === 'warning'" :size="11" />
            <Loader2 v-else-if="stage.status === 'active'" :size="11" class="op-spin" />
          </span>
          <div class="op-stage__info">
            <span class="op-stage__name">{{ stage.label }}</span>
            <time class="op-stage__time op-mono">{{ stage.time }}</time>
          </div>
        </li>
      </ol>

      <ul v-if="timelineSteps.length" class="op-steps">
        <li v-for="step in timelineSteps" :key="step.key" class="op-step" :data-tone="step.tone">
          <span class="op-step__icon" aria-hidden="true">
            <CheckCircle2 v-if="step.tone === 'success'" :size="15" />
            <XCircle v-else-if="step.tone === 'danger'" :size="15" />
            <AlertTriangle v-else-if="step.tone === 'attention'" :size="15" />
            <Loader2 v-else-if="step.tone === 'progress'" :size="15" class="op-spin" />
            <CircleDot v-else :size="15" />
          </span>
          <span class="op-step__name">{{ step.name }}</span>
          <span class="op-step__msg" :title="step.message">{{ step.message || '—' }}</span>
          <time class="op-step__time op-mono">{{ formatClock(step.createdAt) }}</time>
        </li>
      </ul>
    </section>

    <!-- Codex 深炭黑终端日志区 -->
    <section
      class="op-terminal"
      v-loading="loading"
      element-loading-background="rgba(24, 24, 27, 0.72)"
      element-loading-text="正在读取日志"
    >
      <header class="op-terminal__bar">
        <div class="op-terminal__title">
          <Terminal :size="14" aria-hidden="true" />
          <h3>执行日志</h3>
          <span class="op-terminal__count op-mono">{{ visibleEvents.length }} 行</span>
          <span
            v-if="!history && streaming && operation && maintenanceActive(operation.state)"
            class="op-terminal__live"
          >
            <i class="op-live-dot" aria-hidden="true" />
            实时更新
          </span>
          <span v-else-if="history" class="op-terminal__hist-badge">历史分页</span>
        </div>

        <div class="op-terminal__actions">
          <button
            type="button"
            class="term-btn"
            :disabled="loadingHistory || historyCursor === null || !visibleEvents.length"
            @click="older"
          >
            <History :size="13" aria-hidden="true" />
            <span>{{ loadingHistory ? '加载中…' : '更早日志' }}</span>
          </button>
          <button v-if="history" type="button" class="term-btn term-btn--accent" @click="latest">
            <span>返回最新</span>
          </button>
          <button
            type="button"
            class="term-btn"
            :class="{ 'term-btn--copied': copiedLogs }"
            :disabled="!visibleEvents.length"
            @click="copyLogs"
          >
            <Check v-if="copiedLogs" :size="13" aria-hidden="true" />
            <Copy v-else :size="13" aria-hidden="true" />
            <span>{{ copiedLogs ? '已复制' : '一键复制日志' }}</span>
          </button>
        </div>
      </header>

      <div ref="log" class="op-terminal__body" role="log" aria-label="运维执行日志">
        <div v-if="!loading && !visibleEvents.length" class="term-empty">
          <Terminal :size="20" aria-hidden="true" />
          <span>暂无执行日志</span>
        </div>

        <div
          v-for="(event, index) in visibleEvents"
          :key="event.id"
          class="term-line"
          :data-tone="maintenanceSemanticTone(event.state, event.type)"
        >
          <span class="term-lineno" aria-hidden="true">{{ index + 1 }}</span>
          <time class="term-time">{{ formatLogTime(event.created_at) }}</time>
          <span class="term-badge" :data-tone="maintenanceSemanticTone(event.state, event.type)">
            {{ logState(event) }}
          </span>
          <span class="term-name" :title="event.name || stageLabel(event.stage)">
            {{ event.name || stageLabel(event.stage) }}
          </span>
          <div class="term-content">
            <span class="term-message">{{ redactMaintenanceText(event.message) || '—' }}</span>
            <details v-if="hasDetail(event.detail)" class="term-detail">
              <summary>结构化输出</summary>
              <pre class="term-detail__pre">{{ formatEventDetail(event.detail) }}</pre>
            </details>
          </div>
        </div>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElAlert, ElButton, ElMessage, ElMessageBox, ElTag, vLoading } from 'element-plus'
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  CheckCircle2,
  CircleDot,
  Copy,
  History,
  Loader2,
  RotateCw,
  ShieldAlert,
  Square,
  Terminal,
  X,
  XCircle,
} from 'lucide-vue-next'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import { api } from '@/api/client'
import {
  cancelMaintenance,
  confirmMaintenanceStopped,
  getMaintenanceOperation,
  listOlderMaintenanceEvents,
  redactMaintenanceText,
  retryMaintenance,
  subscribeMaintenanceEvents,
  type MaintenanceEvent,
  type MaintenanceOperation,
  type MaintenanceState,
} from '@/api/maintenance'
import type { Worker } from '@/api/types'
import { maintenanceActions, maintenanceActive, maintenanceStates } from '@/features/workers/maintenance'
import { maintenanceSemanticTone, type SemanticTone } from '@/features/workers/semantic'

const route = useRoute()
const workerId = computed(() => String(route.params.id))
const operationId = computed(() => String(route.params.operationId))

const worker = ref<Worker>()
const operation = ref<MaintenanceOperation>()
const events = ref<MaintenanceEvent[]>([])
const logLimit = 500
const history = ref<MaintenanceEvent[]>()
const historyCursor = ref<number | null>()
const visibleEvents = computed(() => history.value ?? events.value)
const loadingHistory = ref(false)
let cursor: number | undefined
let historyRequest: AbortController | undefined
const log = ref<HTMLElement>()
const loading = ref(false)
const streaming = ref(false)
const acting = ref(false)
const error = ref('')
const copiedLogs = ref(false)
const nowTick = ref(Date.now())
let stream: AbortController | undefined
let copyTimer: number | undefined
let tickTimer: ReturnType<typeof setInterval> | undefined

const STAGE_LABELS: Record<string, string> = {
  queued: '排队中',
  running: '执行中',
  completed: '已完成',
  succeeded: '已完成',
  failed: '执行失败',
  canceled: '已取消',
  stop_unconfirmed: '停止待确认',
  stopped_confirmed: '已确认停止',
}

function stageLabel(stage: string | null | undefined): string {
  if (!stage) return '—'
  return STAGE_LABELS[stage.toLowerCase()] ?? stage
}

function parseIsoDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const d = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

function formatDateTimeSec(value: string | null | undefined): string {
  const d = parseIsoDate(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

function formatClock(value: string | null | undefined): string {
  const d = parseIsoDate(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

function formatLogTime(value: string | null | undefined): string {
  const d = parseIsoDate(value)
  if (!d) return '—'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d)
}

const durationText = computed(() => {
  const op = operation.value
  if (!op) return '—'
  const start = parseIsoDate(op.started_at)
  if (!start) return op.state === 'QUEUED' ? '等待开始' : '—'
  const end = parseIsoDate(op.finished_at)
  const endMs = end ? end.getTime() : maintenanceActive(op.state) ? nowTick.value : start.getTime()
  const seconds = Math.max(0, Math.round((endMs - start.getTime()) / 1000))
  if (seconds < 60) return `${seconds}s`
  const mins = Math.floor(seconds / 60)
  const rem = seconds % 60
  if (mins < 60) return `${mins}m ${String(rem).padStart(2, '0')}s`
  const hours = Math.floor(mins / 60)
  const remMins = mins % 60
  return `${hours}h ${remMins}m`
})

const lifecycleStages = computed(() => {
  const op = operation.value
  if (!op) return []
  const isUnconfirmed = op.stage === 'stop_unconfirmed'
  const execStatus =
    op.state === 'QUEUED'
      ? 'pending'
      : op.state === 'RUNNING'
        ? 'active'
        : op.state === 'SUCCEEDED'
          ? 'done'
          : isUnconfirmed
            ? 'warning'
            : op.state === 'CANCELED'
              ? 'canceled'
              : 'failed'

  const endStatus =
    op.state === 'SUCCEEDED'
      ? 'done'
      : isUnconfirmed
        ? 'warning'
        : op.state === 'FAILED'
          ? 'failed'
          : op.state === 'CANCELED'
            ? 'canceled'
            : 'pending'

  const endLabel = isUnconfirmed
    ? '停止待确认'
    : op.state === 'SUCCEEDED'
      ? '已完成'
      : op.state === 'FAILED'
        ? '执行失败'
        : op.state === 'CANCELED'
          ? '已取消'
          : '待完成'

  return [
    { key: 'queued', label: '已入队', status: 'done', time: formatClock(op.requested_at) },
    { key: 'running', label: '执行中', status: execStatus, time: formatClock(op.started_at) },
    { key: 'result', label: endLabel, status: endStatus, time: formatClock(op.finished_at) },
  ]
})

interface StepSummary {
  key: string
  name: string
  tone: SemanticTone
  message: string
  createdAt: string
}

const timelineSteps = computed<StepSummary[]>(() => {
  const source = events.value
  const map = new Map<string, StepSummary>()
  for (const event of source) {
    if (event.type !== 'step' && !event.name) continue
    const name = event.name || stageLabel(event.stage)
    const tone = maintenanceSemanticTone(event.state, event.type)
    map.set(name, {
      key: `${event.attempt}-${name}`,
      name,
      tone,
      message: redactMaintenanceText(event.message),
      createdAt: event.created_at,
    })
  }
  return Array.from(map.values())
})

const snapshotChips = computed<Array<{ label: string; mono?: boolean }>>(() => {
  const snap = operation.value?.after_snapshot
  if (!snap) return []
  const chips: Array<{ label: string; mono?: boolean }> = []
  if (snap.opencode_version) chips.push({ label: `OpenCode v${snap.opencode_version}`, mono: true })
  if (snap.model_name) chips.push({ label: snap.model_name, mono: true })
  if (snap.repository?.branch) chips.push({ label: `分支 ${snap.repository.branch}`, mono: true })
  if (snap.repository?.commit) chips.push({ label: snap.repository.commit.slice(0, 8), mono: true })
  if (snap.changed) {
    const changedLabels = [
      snap.changed.runtime ? '运行时' : '',
      snap.changed.configuration ? '配置' : '',
      snap.changed.skills ? '技能' : '',
      snap.changed.repository ? '仓库' : '',
    ].filter(Boolean)
    if (changedLabels.length) chips.push({ label: `已更新：${changedLabels.join(' / ')}` })
  }
  return chips
})

function isRichText(value: string): boolean {
  return value.includes('\n') || /```|^#{1,3}\s|^[-*]\s/m.test(value)
}

function hasDetail(detail: Record<string, unknown> | null | undefined): boolean {
  return Boolean(detail && Object.keys(detail).length > 0)
}

function formatEventDetail(detail: Record<string, unknown> | null | undefined): string {
  if (!detail) return ''
  try {
    return redactMaintenanceText(JSON.stringify(detail, null, 2))
  } catch {
    return redactMaintenanceText(String(detail))
  }
}

function logState(event: MaintenanceEvent) {
  const state = event.state?.toUpperCase()
  if (state === 'PASSED') return '通过'
  if (event.type === 'cancel_requested') return '请求取消'
  return maintenanceStates[state as MaintenanceState] || '记录'
}

async function copyLogs() {
  if (!visibleEvents.value.length) return
  const lines = visibleEvents.value.map((event) => {
    const head = `[${formatLogTime(event.created_at)}] [${logState(event)}] ${event.name || stageLabel(event.stage)}`
    const msg = redactMaintenanceText(event.message)
    const detail = hasDetail(event.detail) ? `\n${formatEventDetail(event.detail)}` : ''
    return `${head}: ${msg}${detail}`
  })
  const text = lines.join('\n')
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    document.body.removeChild(area)
  }
  copiedLogs.value = true
  window.clearTimeout(copyTimer)
  copyTimer = window.setTimeout(() => {
    copiedLogs.value = false
  }, 1600)
}

function append(event: MaintenanceEvent) {
  if (event.id <= (cursor ?? 0)) return
  cursor = event.id
  if (operation.value?.attempt === event.attempt) {
    if (event.type === 'started' && operation.value.state === 'QUEUED') {
      operation.value.state = 'RUNNING'
      operation.value.started_at = event.created_at
    }
    if (event.type === 'cancel_requested') operation.value.cancel_requested = true
  }
  const viewport = log.value
  const follow = !history.value && (!viewport || viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 32)
  events.value.push(event)
  if (events.value.length > logLimit) events.value.shift()
  if (follow) void nextTick(() => { if (log.value) log.value.scrollTop = log.value.scrollHeight })
}

async function older() {
  const before = historyCursor.value ?? visibleEvents.value[0]?.id
  if (before === undefined || historyCursor.value === null || historyRequest) return
  const controller = new AbortController()
  historyRequest = controller
  loadingHistory.value = true
  try {
    const result = await listOlderMaintenanceEvents(operationId.value, before, controller.signal)
    if (controller.signal.aborted) return
    if (result.items.length) {
      history.value = result.items
      if (log.value) log.value.scrollTop = 0
    }
    historyCursor.value = result.next_cursor
  } catch (reason) {
    if (!controller.signal.aborted) ElMessage.error(String(reason))
  } finally {
    if (historyRequest === controller) {
      historyRequest = undefined
      loadingHistory.value = false
    }
  }
}

function latest() {
  historyRequest?.abort()
  historyRequest = undefined
  loadingHistory.value = false
  history.value = undefined
  historyCursor.value = undefined
  void nextTick(() => { if (log.value) log.value.scrollTop = log.value.scrollHeight })
}

async function observe() {
  stream?.abort()
  const controller = new AbortController()
  stream = controller
  const id = operationId.value
  const parentId = workerId.value
  loading.value = !operation.value
  streaming.value = false
  error.value = ''
  try {
    const [row, selectedWorker] = await Promise.all([
      getMaintenanceOperation(id, controller.signal),
      api.worker(parentId, controller.signal),
    ])
    if (controller.signal.aborted) return
    if (row.agent_worker_id !== parentId) throw new Error('此操作不属于当前 Worker')
    operation.value = row
    worker.value = selectedWorker
    if (cursor === undefined) {
      events.value = row.events ?? []
      cursor = events.value.at(-1)?.id ?? 0
      if (!history.value) void nextTick(() => { if (log.value) log.value.scrollTop = log.value.scrollHeight })
    }
    loading.value = false
    streaming.value = true
    await subscribeMaintenanceEvents(id, {
      signal: controller.signal,
      afterId: cursor,
      onEvent: append,
    })
    const final = await getMaintenanceOperation(id, controller.signal)
    if (!controller.signal.aborted) operation.value = final
  } catch (reason) {
    if (!controller.signal.aborted) error.value = String(reason)
  } finally {
    if (stream === controller) {
      loading.value = false
      streaming.value = false
    }
  }
}

async function act(command: 'cancel' | 'retry' | 'confirm') {
  if (!operation.value || acting.value) return
  const id = operation.value.id
  acting.value = true
  try {
    if (command === 'confirm') {
      await ElMessageBox.confirm('请先确认目标主机上的部署或更新进程已停止，确认后将释放维护锁定。', '确认远端已停止', {
        type: 'warning',
      })
      operation.value = await confirmMaintenanceStopped(id)
    } else if (command === 'cancel') {
      await ElMessageBox.confirm('确认请求取消当前运维操作？', '取消操作', { type: 'warning' })
      operation.value = await cancelMaintenance(id)
    } else {
      operation.value = await retryMaintenance(id)
    }
    void observe()
  } catch (reason) {
    if (reason !== 'cancel' && reason !== 'close') ElMessage.error(String(reason))
  } finally {
    acting.value = false
  }
}

watch(
  [workerId, operationId],
  () => {
    operation.value = undefined
    worker.value = undefined
    events.value = []
    cursor = undefined
    latest()
    void observe()
  },
  { immediate: true },
)

onMounted(() => {
  tickTimer = setInterval(() => {
    if (operation.value && maintenanceActive(operation.value.state)) {
      nowTick.value = Date.now()
    }
  }, 1000)
})

onUnmounted(() => {
  clearInterval(tickTimer)
  window.clearTimeout(copyTimer)
  stream?.abort()
  historyRequest?.abort()
})
</script>

<style scoped>
.op-detail-page {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.op-mono {
  font-family: var(--astra-font-mono);
  font-variant-numeric: tabular-nums;
}

/* ── 顶部身份操作卡 ──────────────────────────────────────────────── */
.op-identity {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding: 16px;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}

.op-identity__main {
  display: grid;
  gap: 6px;
  min-width: 0;
}

.op-back {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  width: fit-content;
  color: var(--astra-text-muted);
  font-size: var(--astra-font-helper);
  text-decoration: none;
  transition: color 0.15s ease;
}

.op-back:hover {
  color: var(--astra-text-primary);
}

.op-identity__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
}

.op-identity__title h2 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 18px;
  font-weight: 600;
  line-height: 26px;
}

.op-id-pill {
  display: inline-flex;
  align-items: center;
  padding: 1px 6px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
}

.op-id-pill :deep(.id-display) {
  font-family: var(--astra-font-mono);
  font-size: 12px;
}

.op-id-pill :deep(.id-display__value) {
  color: var(--astra-text-muted);
}

.op-identity__sub {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.op-sub-sep {
  color: var(--astra-border-hover);
}

.op-identity__actions {
  display: flex;
  flex: none;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.op-identity__actions .el-button + .el-button {
  margin-left: 0;
}

/* ── 操作摘要指标条 ──────────────────────────────────────────────── */
.op-summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 12px;
}

.op-metric {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
  padding: 12px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
}

.op-metric__label {
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 500;
}

.op-metric__value {
  color: var(--astra-text-primary);
  font-size: 15px;
  font-weight: 600;
  line-height: 22px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.op-metric__hint {
  color: var(--astra-text-muted);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ── 异常 / 待确认提示卡 ─────────────────────────────────────────── */
.op-callout {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--astra-border);
  border-left-width: 3px;
  border-radius: 10px;
}

.op-callout--danger {
  border-color: color-mix(in srgb, var(--astra-danger) 28%, transparent);
  border-left-color: var(--astra-danger);
  background: var(--astra-danger-soft);
}

.op-callout--warning {
  border-color: color-mix(in srgb, var(--astra-warning) 30%, transparent);
  border-left-color: var(--astra-warning);
  background: var(--astra-warning-soft);
}

.op-callout__head {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 13px;
  font-weight: 600;
}

.op-callout--danger .op-callout__head {
  color: var(--astra-danger);
}

.op-callout--warning .op-callout__head {
  color: var(--astra-warning);
}

.op-callout__text {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}

/* ── 阶段与步骤时间线卡 ──────────────────────────────────────────── */
.op-timeline-card {
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.op-card-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.op-card-head__title {
  display: flex;
  align-items: center;
  gap: 8px;
}

.op-card-head__title h3 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.op-card-head__meta {
  color: var(--astra-text-muted);
  font-size: 12px;
}

.op-snapshot-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.op-chip {
  padding: 1px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  line-height: 20px;
}

.op-stages {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
  margin: 0;
  padding: 12px 16px;
  list-style: none;
  background: var(--astra-bg-subtle);
}

.op-stage {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-panel);
}

.op-stage__dot {
  display: grid;
  place-items: center;
  width: 22px;
  height: 22px;
  flex: none;
  border-radius: 50%;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-muted);
}

.op-stage[data-state='done'] .op-stage__dot {
  background: var(--astra-success-soft);
  color: var(--astra-success);
}

.op-stage[data-state='active'] .op-stage__dot {
  background: var(--astra-info-soft);
  color: var(--astra-info);
}

.op-stage[data-state='failed'] .op-stage__dot {
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
}

.op-stage[data-state='warning'] .op-stage__dot {
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.op-stage__info {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.op-stage__name {
  color: var(--astra-text-primary);
  font-size: 13px;
  font-weight: 600;
  line-height: 18px;
}

.op-stage__time {
  color: var(--astra-text-muted);
  font-size: 11.5px;
  line-height: 16px;
}

.op-steps {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 4px 16px 8px;
  list-style: none;
  border-top: 1px solid var(--astra-border-subtle);
}

.op-step {
  display: grid;
  grid-template-columns: 18px minmax(110px, 180px) minmax(0, 1fr) 68px;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid var(--astra-border-subtle);
  font-size: 13px;
}

.op-step:last-child {
  border-bottom: 0;
}

.op-step__icon {
  display: inline-flex;
  color: var(--astra-neutral);
}

.op-step[data-tone='success'] .op-step__icon {
  color: var(--astra-success);
}

.op-step[data-tone='progress'] .op-step__icon {
  color: var(--astra-info);
}

.op-step[data-tone='attention'] .op-step__icon {
  color: var(--astra-warning);
}

.op-step[data-tone='danger'] .op-step__icon {
  color: var(--astra-danger);
}

.op-step__name {
  color: var(--astra-text-primary);
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.op-step__msg {
  color: var(--astra-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.op-step[data-tone='danger'] .op-step__msg {
  color: var(--astra-danger);
}

.op-step__time {
  color: var(--astra-text-muted);
  font-size: 12px;
  text-align: right;
}

/* ── Codex 深炭黑终端日志区 ──────────────────────────────────────── */
.op-terminal {
  display: flex;
  flex-direction: column;
  border: 1px solid #27272a;
  border-radius: 12px;
  background: #18181b;
  overflow: hidden;
  box-shadow: 0 2px 8px rgba(9, 9, 11, 0.12);
}

.op-terminal__bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 12px;
  padding: 10px 14px;
  border-bottom: 1px solid #27272a;
  background: #202024;
}

.op-terminal__title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: #f4f4f5;
}

.op-terminal__title h3 {
  margin: 0;
  color: #f4f4f5;
  font-size: 13px;
  font-weight: 600;
  line-height: 18px;
}

.op-terminal__count {
  padding: 1px 7px;
  border-radius: 999px;
  background: #27272a;
  color: #a1a1aa;
  font-size: 11px;
  line-height: 16px;
}

.op-terminal__live {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px;
  border: 1px solid rgba(34, 197, 94, 0.35);
  border-radius: 999px;
  background: rgba(34, 197, 94, 0.12);
  color: #86efac;
  font-size: 11.5px;
}

.op-live-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #22c55e;
  box-shadow: 0 0 0 3px rgba(34, 197, 94, 0.25);
}

.op-terminal__hist-badge {
  padding: 1px 8px;
  border: 1px solid rgba(245, 158, 11, 0.35);
  border-radius: 999px;
  background: rgba(245, 158, 11, 0.12);
  color: #fcd34d;
  font-size: 11.5px;
}

.op-terminal__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.term-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 10px;
  border: 1px solid #3f3f46;
  border-radius: 6px;
  background: #27272a;
  color: #e4e4e7;
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease, color 0.15s ease;
}

.term-btn:hover:not(:disabled) {
  border-color: #52525b;
  background: #323238;
  color: #ffffff;
}

.term-btn:disabled {
  opacity: 0.42;
  cursor: not-allowed;
}

.term-btn--accent {
  border-color: rgba(59, 130, 246, 0.45);
  background: rgba(37, 99, 235, 0.2);
  color: #93c5fd;
}

.term-btn--copied {
  border-color: rgba(34, 197, 94, 0.5);
  background: rgba(21, 128, 61, 0.22);
  color: #86efac;
}

.op-terminal__body {
  min-height: 360px;
  max-height: 62vh;
  overflow-y: auto;
  padding: 8px 0;
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  line-height: 1.65;
  scrollbar-gutter: stable;
}

.term-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  min-height: 220px;
  color: #71717a;
  font-family: var(--astra-font-family);
  font-size: 13px;
}

.term-line {
  display: grid;
  grid-template-columns: 38px 116px 66px minmax(96px, 148px) minmax(0, 1fr);
  align-items: start;
  gap: 10px;
  padding: 4px 14px;
  border-left: 2px solid transparent;
  transition: background-color 0.1s ease;
}

.term-line:hover {
  background: rgba(255, 255, 255, 0.035);
}

.term-line[data-tone='danger'] {
  border-left-color: #ef4444;
  background: rgba(239, 68, 68, 0.09);
}

.term-line[data-tone='attention'] {
  border-left-color: #f59e0b;
  background: rgba(245, 158, 11, 0.08);
}

.term-lineno {
  color: #52525b;
  font-size: 11px;
  text-align: right;
  user-select: none;
  font-variant-numeric: tabular-nums;
}

.term-time {
  color: #71717a;
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.term-badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  padding: 0 6px;
  border-radius: 4px;
  background: rgba(161, 161, 170, 0.14);
  color: #a1a1aa;
  font-family: var(--astra-font-family);
  font-size: 11px;
  font-weight: 500;
  line-height: 20px;
  white-space: nowrap;
}

.term-badge[data-tone='success'] {
  background: rgba(34, 197, 94, 0.16);
  color: #86efac;
}

.term-badge[data-tone='progress'] {
  background: rgba(59, 130, 246, 0.18);
  color: #93c5fd;
}

.term-badge[data-tone='attention'] {
  background: rgba(245, 158, 11, 0.18);
  color: #fcd34d;
}

.term-badge[data-tone='danger'] {
  background: rgba(239, 68, 68, 0.2);
  color: #fca5a5;
}

.term-name {
  color: #e4e4e7;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.term-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.term-message {
  color: #d4d4d8;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.term-line[data-tone='danger'] .term-message {
  color: #fecaca;
}

.term-detail {
  margin-top: 2px;
}

.term-detail summary {
  width: fit-content;
  color: #93c5fd;
  font-family: var(--astra-font-family);
  font-size: 11.5px;
  cursor: pointer;
  user-select: none;
}

.term-detail__pre {
  margin: 6px 0 2px;
  padding: 8px 10px;
  border: 1px solid #27272a;
  border-radius: 6px;
  background: #111114;
  color: #a1a1aa;
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
  line-height: 1.55;
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.op-spin {
  animation: op-spin 1s linear infinite;
}

@keyframes op-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

@media (min-width: 2240px) {
  .op-detail-page {
    gap: 18px;
  }
  .op-summary-grid {
    gap: 16px;
  }
  .op-step {
    grid-template-columns: 20px minmax(140px, 240px) minmax(0, 1fr) 80px;
    gap: 12px;
  }
  .op-terminal__body {
    min-height: 480px;
    max-height: 68vh;
    font-size: 13px;
  }
  .term-line {
    grid-template-columns: 44px 128px 72px minmax(120px, 220px) minmax(0, 1fr);
    gap: 12px;
    padding: 5px 18px;
  }
}

@media (max-width: 1120px) {
  .op-summary-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

@media (max-width: 900px) {
  .op-identity {
    flex-wrap: wrap;
  }
  .op-stages {
    grid-template-columns: 1fr;
  }
  .op-step {
    grid-template-columns: 18px minmax(0, 1fr) 64px;
  }
  .op-step__msg {
    grid-column: 2 / 4;
  }
  .term-line {
    grid-template-columns: 32px 108px 60px minmax(0, 1fr);
  }
  .term-name {
    grid-column: 2 / 5;
  }
  .term-content {
    grid-column: 2 / 5;
  }
}
</style>
