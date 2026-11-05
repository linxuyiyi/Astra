<template>
  <section>
    <h3>运行维护</h3>
    <div class="toolbar">
      <el-button
        v-for="action in actions"
        :key="action.key"
        :disabled="busy"
        @click="submit(action.key)"
        >{{ action.label }}</el-button
      >
      <el-button @click="load">刷新</el-button>
    </div>
    <el-alert v-if="error" :title="error" type="error" />
    <p v-if="health">
      运行就绪：{{ readinessLabels[health.readiness] }} ·
      {{ health.blocking.map((x) => x.message).join('；') }}
    </p>
    <el-table :data="operations" @row-click="select">
      <el-table-column label="操作" width="140">
        <template #default="{ row }">{{
          actions.find((action) => action.key === row.action)?.label
        }}</template>
      </el-table-column>
      <el-table-column label="结果" width="160">
        <template #default="{ row }">
          <el-tag
            :type="semanticTag(maintenanceSemanticTone(row.state)).type"
            :class="semanticTag(maintenanceSemanticTone(row.state)).className"
            >{{ operationLabels[row.state] }}</el-tag
          >
        </template>
      </el-table-column>
      <el-table-column prop="error_message" label="说明" min-width="280" />
      <el-table-column label="操作" width="220">
        <template #default="{ row }">
          <el-button
            v-if="['QUEUED', 'RUNNING'].includes(row.state)"
            @click.stop="cancel(row.id)"
            >取消</el-button
          >
          <el-button
            v-if="row.stage === 'stop_unconfirmed'"
            type="danger"
            @click.stop="confirmStopped(row.id)"
            >确认远端已停止</el-button
          >
          <el-button
            v-else-if="['FAILED', 'CANCELED'].includes(row.state)"
            @click.stop="retry(row.id)"
            >重试</el-button
          >
        </template>
      </el-table-column>
      <template #empty><el-empty description="暂无维护操作" /></template>
    </el-table>
    <el-table v-if="events.length" :data="events">
      <el-table-column prop="name" label="步骤" width="220" />
      <el-table-column prop="message" label="详情" />
    </el-table>
    <el-button v-if="nextCursor !== null" @click="nextLogPage"
      >下一页日志</el-button
    >
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessageBox } from 'element-plus'
import type { Worker } from '@/api/types'
import {
  getWorkerHealth,
  listMaintenanceOperations,
  submitMaintenance,
  getMaintenanceEventsPage,
  cancelMaintenance,
  retryMaintenance,
  confirmMaintenanceStopped,
  subscribeMaintenanceEvents,
  type MaintenanceOperation,
  type MaintenanceEvent,
  type MaintenanceAction,
  type WorkerHealth,
} from '@/api/maintenance'
import {
  semanticTag,
  maintenanceSemanticTone,
} from '@/features/workers/semantic'

const props = defineProps<{ worker: Worker }>()
const emit = defineEmits<{ changed: [] }>()
const operationLabels: Record<string, string> = {
  QUEUED: '排队中',
  RUNNING: '执行中',
  SUCCEEDED: '成功',
  FAILED: '失败',
  CANCELED: '已取消',
}
const readinessLabels: Record<string, string> = {
  READY: '就绪',
  DRIFTED: '待同步',
  DEGRADED: '不可用',
  UNKNOWN: '未检查',
}
const operations = ref<MaintenanceOperation[]>([])
const events = ref<MaintenanceEvent[]>([])
const health = ref<WorkerHealth>()
const error = ref('')
const selected = ref('')
const nextCursor = ref<number | null>(null)
const actions: Array<{ key: MaintenanceAction; label: string }> = [
  { key: 'check', label: '检查' },
  { key: 'deploy', label: '一键部署' },
  { key: 'restart', label: '重启' },
  { key: 'update', label: '更新仓库' },
]
const busy = computed(() =>
  operations.value.some(
    (x) =>
      ['RUNNING', 'QUEUED'].includes(x.state) || x.stage === 'stop_unconfirmed',
  ),
)
let timer: ReturnType<typeof setInterval> | undefined
let stream: AbortController | undefined
let updating = false
let cursor = 0

function appendEvent(event: MaintenanceEvent) {
  if (event.id <= cursor) return
  cursor = event.id
  events.value = [...events.value.slice(-199), event]
}
async function observeLogs(operation: MaintenanceOperation) {
  const id = operation.id
  const page = await getMaintenanceEventsPage(id, cursor)
  if (selected.value !== id) return
  page.items.forEach(appendEvent)
  nextCursor.value = page.next_cursor
  if (!['QUEUED', 'RUNNING'].includes(operation.state) || stream) return
  const controller = new AbortController()
  stream = controller
  void subscribeMaintenanceEvents(id, {
    signal: controller.signal,
    afterId: cursor,
    onEvent: appendEvent,
    onEnd: () => {
      void load()
    },
  })
    .catch((e) => {
      if (!controller.signal.aborted) error.value = String(e)
    })
    .finally(() => {
      if (stream === controller) stream = undefined
    })
}
async function nextLogPage() {
  const operation = operations.value.find((x) => x.id === selected.value)
  if (operation) {
    try {
      events.value = []
      await observeLogs(operation)
    } catch (e) {
      error.value = String(e)
    }
  }
}
async function load() {
  if (updating) return
  updating = true
  try {
    const [list, status] = await Promise.all([
      listMaintenanceOperations(props.worker.id),
      getWorkerHealth(props.worker.id),
    ])
    operations.value = list.items
    health.value = status
    error.value = ''
    const operation = list.items.find((x) => x.id === selected.value)
    if (operation && !stream) await observeLogs(operation)
  } catch (e) {
    error.value = String(e)
  } finally {
    updating = false
  }
}
async function select(operation: MaintenanceOperation) {
  stream?.abort()
  stream = undefined
  selected.value = operation.id
  cursor = 0
  events.value = []
  nextCursor.value = null
  try {
    await observeLogs(operation)
  } catch (e) {
    error.value = String(e)
  }
}
async function command(action: () => Promise<MaintenanceOperation>) {
  try {
    const operation = await action()
    await load()
    await select(operation)
    emit('changed')
  } catch (e) {
    error.value = String(e)
  }
}
async function submit(action: MaintenanceAction) {
  if (action !== 'check') {
    try {
      await ElMessageBox.confirm(
        '维护将操作 Worker 的原生运行环境；有未释放任务时禁止执行。',
        'Worker 维护',
      )
    } catch {
      return
    }
  }
  await command(() => submitMaintenance(props.worker.id, action))
}
async function cancel(id: string) {
  await command(() => cancelMaintenance(id))
}
async function retry(id: string) {
  await command(() => retryMaintenance(id))
}
async function confirmStopped(id: string) {
  try {
    await ElMessageBox.confirm(
      '请先在目标机器确认部署和更新进程均已停止。确认后才释放维护互斥占用。',
      '确认远端进程停止',
      { type: 'warning' },
    )
  } catch {
    return
  }
  await command(() => confirmMaintenanceStopped(id))
}
onMounted(() => {
  void load()
  timer = setInterval(load, 3000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
  stream?.abort()
})
</script>
<style scoped>
section {
  margin-top: 28px;
}
.toolbar {
  display: flex;
  gap: 10px;
  margin-bottom: 16px;
}
</style>
