<template>
  <section class="astra-page" v-loading="refreshing && !worker" data-testid="worker-detail">
    <AstraPageHeader :title="worker?.name || 'Worker 详情'" :description="worker?.description">
      <template v-if="worker">
        <SemanticTag :tone="semanticTone(statusTone(worker.status))" :label="statusLabel(worker.status)" />
        <span>连接 {{ worker.online === null ? '未知' : worker.online ? '在线' : '离线' }}</span>
        <span>任务调度 {{ worker.enabled ? '已启用' : '已停用' }}</span>
        <span v-if="lastUpdated">更新于 {{ lastUpdated }}</span>
      </template>
      <template #actions>
        <el-button @click="router.push({ name: 'settings-workers' })">返回列表</el-button>
        <el-button :icon="RefreshCw" :loading="refreshing" :disabled="acting" @click="refresh">刷新</el-button>
        <template v-if="worker">
          <el-button :disabled="acting" @click="editing = true">编辑配置</el-button>
          <el-button :disabled="acting" @click="toggle">{{ worker.status === 'PAUSED' ? '恢复 Worker' : '暂停 Worker' }}</el-button>
          <el-button type="danger" plain :disabled="acting || executionLocked || !health" @click="remove">删除</el-button>
        </template>
      </template>
    </AstraPageHeader>
    <el-alert v-if="refreshError" :title="refreshError" type="error" show-icon :closable="false" />
    <el-alert v-if="actionError" :title="actionError" type="error" show-icon :closable="false" />
    <div v-if="worker" class="worker-detail__content">
      <el-descriptions :column="2" border>
        <el-descriptions-item label="主机">{{ worker.host || '—' }}</el-descriptions-item>
        <el-descriptions-item label="工作区">{{ worker.workspace_root }}</el-descriptions-item>
        <el-descriptions-item label="OpenCode 端口">{{ worker.opencode_port || '待部署' }}</el-descriptions-item>
        <el-descriptions-item label="最后检查">{{ formatTime(worker.last_checked_at) }}</el-descriptions-item>
        <el-descriptions-item label="占用任务" :span="2"><WorkerOccupiedTasks :tasks="worker.occupied_tasks" /></el-descriptions-item>
      </el-descriptions>
      <WorkerMaintenancePanel :key="worker.id" :worker="worker" :health="health" />
    </div>
    <el-dialog v-if="worker" v-model="editing" title="编辑 Worker" width="760px" destroy-on-close>
      <WorkerConfig :worker="worker" :execution-locked="executionLocked" @saved="configurationSaved" />
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { RefreshCw } from 'lucide-vue-next'
import { api } from '@/api/client'
import { getWorkerHealth, type WorkerHealth } from '@/api/maintenance'
import type { Worker } from '@/api/types'
import { statusLabel, statusTone, formatTime } from '@/utils/format'
import { semanticTone } from '@/features/workers/semantic'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import WorkerConfig from './settings/WorkerConfig.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import WorkerOccupiedTasks from '@/components/worker/WorkerOccupiedTasks.vue'

const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id))
const worker = ref<Worker>()
const health = ref<WorkerHealth>()
const refreshing = ref(false)
const editing = ref(false)
const acting = ref(false)
const refreshError = ref('')
const actionError = ref('')
const lastUpdated = ref('')
const executionLocked = computed(() => Boolean(worker.value?.occupied_tasks.length ||
  health.value?.blocking.some(item => ['maintenance_active', 'occupied_task', 'review_active'].includes(item.code))))
let request: AbortController | undefined
let timer: ReturnType<typeof setInterval> | undefined

async function refresh() {
  if (request || acting.value) return
  const controller = new AbortController()
  const workerId = id.value
  request = controller
  refreshing.value = true
  const [record, diagnostics] = await Promise.allSettled([
    api.worker(workerId, controller.signal),
    getWorkerHealth(workerId, controller.signal),
  ])
  if (controller.signal.aborted) return
  if (record.status === 'fulfilled') worker.value = record.value
  if (diagnostics.status === 'fulfilled') health.value = diagnostics.value
  const failures = [
    record.status === 'rejected' ? `Worker 状态：${record.reason}` : '',
    diagnostics.status === 'rejected' ? `运行环境：${diagnostics.reason}` : '',
  ].filter(Boolean)
  refreshError.value = failures.length ? `刷新失败。 ${failures.join('；')}` : ''
  if (!failures.length) lastUpdated.value = new Date().toLocaleTimeString('zh-CN', { hour12: false })
  request = undefined
  refreshing.value = false
}
function stopRefresh() {
  request?.abort()
  request = undefined
  refreshing.value = false
}
function configurationSaved(updated: Worker) {
  editing.value = false
  if (id.value !== updated.id) return
  stopRefresh()
  worker.value = updated
  void refresh()
}
async function toggle() {
  if (!worker.value || acting.value) return
  const workerId = id.value
  const action = worker.value.status === 'PAUSED' ? 'resume' : 'pause'
  stopRefresh()
  acting.value = true
  actionError.value = ''
  try {
    const updated = await api.workerAction(workerId, action)
    if (id.value === workerId) worker.value = updated
  } catch (reason) {
    if (id.value === workerId) actionError.value = String(reason)
  } finally {
    if (id.value === workerId) {
      acting.value = false
      void refresh()
    }
  }
}
async function remove() {
  if (acting.value || executionLocked.value) return
  const workerId = id.value
  acting.value = true
  actionError.value = ''
  try {
    await ElMessageBox.confirm('删除此 Worker 配置？有未释放任务、维护操作或未完成复盘时不能删除。', '删除 Worker', { type: 'warning' })
    if (id.value !== workerId) return
    await api.deleteWorker(workerId)
    if (id.value === workerId) await router.push({ name: 'settings-workers' })
  } catch (reason) {
    if (id.value === workerId && reason !== 'cancel' && reason !== 'close') actionError.value = String(reason)
  } finally {
    if (id.value === workerId) acting.value = false
  }
}
function refreshWhenVisible() {
  if (!document.hidden) void refresh()
}
watch(id, () => {
  stopRefresh()
  worker.value = health.value = undefined
  refreshError.value = actionError.value = lastUpdated.value = ''
  editing.value = acting.value = false
  void refresh()
}, { immediate: true })
onMounted(() => {
  timer = setInterval(refreshWhenVisible, 10_000)
  document.addEventListener('visibilitychange', refreshWhenVisible)
})
onUnmounted(() => {
  clearInterval(timer)
  document.removeEventListener('visibilitychange', refreshWhenVisible)
  stopRefresh()
})
</script>

<style scoped>
.worker-detail__content { display: flex; flex: 1 1 auto; flex-direction: column; gap: 16px; min-height: 0; overflow-y: auto; scrollbar-gutter: stable; }
.worker-detail__content > * { flex: none; }
</style>
