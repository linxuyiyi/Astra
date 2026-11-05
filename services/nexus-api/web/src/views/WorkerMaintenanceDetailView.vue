<template>
  <section class="astra-page maintenance-detail" data-testid="worker-maintenance-detail">
    <AstraPageHeader :title="operation ? maintenanceActions[operation.action] : '运维操作'" :description="worker?.name">
      <SemanticTag v-if="operation" :tone="operation.stage === 'stop_unconfirmed' ? 'attention' : maintenanceSemanticTone(operation.state)"
        :label="operation.stage === 'stop_unconfirmed' ? '停止待确认' : maintenanceStates[operation.state]" />
      <template #actions>
        <el-button @click="router.push({ name: 'settings-worker-detail', params: { id: workerId } })">返回 Worker</el-button>
        <template v-if="operation">
          <el-button v-if="maintenanceActive(operation.state) && !operation.cancel_requested" :loading="acting" @click="act('cancel')">取消操作</el-button>
          <el-button v-if="operation.stage === 'stop_unconfirmed'" type="danger" plain :loading="acting" @click="act('confirm')">确认远端已停止</el-button>
          <el-button v-else-if="operation.state === 'FAILED' || operation.state === 'CANCELED'" type="primary" :loading="acting" @click="act('retry')">重试操作</el-button>
        </template>
      </template>
    </AstraPageHeader>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="observe">重试读取</el-button>
    </el-alert>
    <el-descriptions v-if="operation" :column="3" border>
      <el-descriptions-item label="发起时间">{{ formatTime(operation.requested_at) }}</el-descriptions-item>
      <el-descriptions-item label="发起人">{{ operation.requested_by || '—' }}</el-descriptions-item>
      <el-descriptions-item label="执行次数">{{ operation.attempt }}</el-descriptions-item>
      <el-descriptions-item label="开始时间">{{ formatTime(operation.started_at) }}</el-descriptions-item>
      <el-descriptions-item label="结束时间">{{ formatTime(operation.finished_at) }}</el-descriptions-item>
      <el-descriptions-item label="执行方式">{{ operation.execution_reason || '—' }}</el-descriptions-item>
    </el-descriptions>
    <el-alert v-if="operation?.error_message" :title="operation.error_message" :type="operation.stage === 'stop_unconfirmed' ? 'warning' : 'error'" show-icon :closable="false" />
    <section class="astra-table-panel log-panel" v-loading="loading">
      <header class="log-heading">
        <h3>执行日志</h3>
        <span v-if="streaming && operation && maintenanceActive(operation.state)">实时更新</span>
      </header>
      <div ref="log" class="log-body" role="log" aria-label="运维执行日志">
        <el-empty v-if="!loading && !events.length" :image-size="72" description="暂无执行日志" />
        <div v-for="event in events" :key="event.id" class="log-line">
          <time>{{ formatTime(event.created_at) }}</time>
          <SemanticTag :tone="maintenanceSemanticTone(event.state, event.type)" :label="logState(event)" />
          <span v-if="event.name" class="log-name">{{ event.name }}</span>
          <span class="log-message">{{ redactMaintenanceText(event.message) }}</span>
        </div>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import { api } from '@/api/client'
import { cancelMaintenance, confirmMaintenanceStopped, getMaintenanceOperation, redactMaintenanceText, retryMaintenance, subscribeMaintenanceEvents, type MaintenanceEvent, type MaintenanceOperation, type MaintenanceState } from '@/api/maintenance'
import type { Worker } from '@/api/types'
import { maintenanceActions, maintenanceActive, maintenanceStates } from '@/features/workers/maintenance'
import { maintenanceSemanticTone } from '@/features/workers/semantic'
import { formatTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const workerId = computed(() => String(route.params.id))
const operationId = computed(() => String(route.params.operationId))
const worker = ref<Worker>()
const operation = ref<MaintenanceOperation>()
const events = ref<MaintenanceEvent[]>([])
const log = ref<HTMLElement>()
const loading = ref(false)
const streaming = ref(false)
const acting = ref(false)
const error = ref('')
let stream: AbortController | undefined

function logState(event: MaintenanceEvent) {
  const state = event.state?.toUpperCase()
  if (state === 'PASSED') return '通过'
  if (event.type === 'cancel_requested') return '请求取消'
  return maintenanceStates[state as MaintenanceState] || '记录'
}
function append(event: MaintenanceEvent) {
  if (operation.value?.attempt === event.attempt) {
    if (event.type === 'started' && operation.value.state === 'QUEUED') {
      operation.value.state = 'RUNNING'
      operation.value.started_at = event.created_at
    }
    if (event.type === 'cancel_requested') operation.value.cancel_requested = true
  }
  const viewport = log.value
  const follow = !viewport || viewport.scrollHeight - viewport.scrollTop - viewport.clientHeight < 32
  events.value.push(event)
  if (follow) void nextTick(() => { if (log.value) log.value.scrollTop = log.value.scrollHeight })
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
      api.worker(parentId),
    ])
    if (controller.signal.aborted) return
    if (row.agent_worker_id !== parentId) throw new Error('此操作不属于当前 Worker')
    operation.value = row
    worker.value = selectedWorker
    loading.value = false
    streaming.value = true
    await subscribeMaintenanceEvents(id, {
      signal: controller.signal,
      afterId: events.value.at(-1)?.id ?? 0,
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
      await ElMessageBox.confirm('请先在目标机器确认部署和更新进程均已停止。确认后才释放维护占用。', '确认远端已停止', { type: 'warning' })
      operation.value = await confirmMaintenanceStopped(id)
    } else if (command === 'cancel') {
      await ElMessageBox.confirm('确认请求停止此操作？', '取消操作', { type: 'warning' })
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
watch([workerId, operationId], () => {
  operation.value = undefined
  worker.value = undefined
  events.value = []
  void observe()
}, { immediate: true })
onUnmounted(() => stream?.abort())
</script>

<style scoped>
.log-panel { display: flex; flex-direction: column; }
.log-heading { display: flex; align-items: center; justify-content: space-between; padding: 12px 16px; border-bottom: 1px solid var(--astra-border); }
.log-heading h3 { margin: 0; font-size: var(--astra-font-section); }
.log-heading span, .log-line time { color: var(--astra-text-muted); }
.log-body { flex: 1 1 auto; min-height: 0; overflow-y: auto; padding: 8px 16px; scrollbar-gutter: stable; }
.log-line { display: grid; grid-template-columns: 156px 76px minmax(125px, max-content) minmax(0, 1fr); align-items: start; gap: 12px; padding: 6px 0; font-size: var(--astra-font-helper); }
.log-line time { font-variant-numeric: tabular-nums; }
.log-line .el-tag { justify-self: start; }
.log-name { font-weight: 600; }
.log-message { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
