<template>
  <section class="admin-page" data-testid="worker-maintenance">
    <section aria-label="运行环境">
      <AstraPageHeader title="运行环境">
        <SemanticTag :tone="readiness[health?.readiness ?? 'UNKNOWN'].tone" :label="readiness[health?.readiness ?? 'UNKNOWN'].label" />
      </AstraPageHeader>
      <el-descriptions v-if="health" :column="1" border class="equipment">
        <el-descriptions-item v-for="item in health.runtime_health.equipment" :key="item.key" :label="item.label">
          <div class="equipment-row">
            <SemanticTag :tone="equipmentState[item.state].tone" :label="equipmentState[item.state].label" />
            <template v-if="item.key === 'worker_agent'">
              <span>{{ worker.mode === 'LOCAL' ? '本机运行' : (health.connection.state === 'online' ? '已连接' : '未连接') }}</span>
              <span v-if="worker.mode !== 'LOCAL' && (item.actual_revision || worker.agent_version)">版本 {{ item.actual_revision || worker.agent_version }}</span>
            </template>
            <span v-else-if="item.key === 'opencode' && item.actual_revision">版本 {{ item.actual_revision }}</span>
            <span v-else-if="item.key !== 'git' && item.detail">{{ item.detail }}</span>
            <template v-if="item.key === 'git' && health.repository">
              <span>远端 {{ health.repository.authentication_verified === true ? '已验证' : health.repository.authentication_verified === false ? '验证失败' : '未检查' }}</span>
              <span v-if="health.repository.url" class="repository-url" :title="health.repository.url">{{ health.repository.url }}</span>
              <span v-if="health.repository.branch">分支 {{ health.repository.branch }}</span>
              <span v-if="health.repository.commit">Commit <IdDisplay :value="health.repository.commit" :max-width="13" /></span>
              <span v-if="health.repository.dirty !== null">{{ health.repository.dirty ? '有未提交修改' : '工作树干净' }}</span>
            </template>
          </div>
          <ul v-if="diagnostics(item).length" class="equipment-reasons" :class="{ 'equipment-reasons--error': item.state === 'degraded' || item.error }">
            <li v-for="reason in diagnostics(item)" :key="reason">{{ reason }}</li>
          </ul>
        </el-descriptions-item>
      </el-descriptions>
      <el-empty v-else :image-size="56" description="暂无运行环境诊断" />
    </section>
    <AstraPageHeader title="运维操作">
      <template #default>
        <span v-if="blocking">{{ blocking }}</span>
      </template>
      <template #actions>
        <el-button v-for="(label, action) in maintenanceActions" :key="action" :type="action === 'deploy' ? 'primary' : undefined" :disabled="!canSubmit(action)" @click="submit(action)">
          {{ label }}
        </el-button>
      </template>
    </AstraPageHeader>
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="load">重试</el-button>
    </el-alert>
    <div class="admin-page__toolbar" role="search" aria-label="运维操作筛选">
      <div class="admin-page__filters">
        <el-select v-model="actionFilter" class="admin-page__select" aria-label="运维操作类型" placeholder="类型：全部" clearable @change="applyFilters">
          <el-option v-for="(label, value) in maintenanceActions" :key="value" :value="value" :label="label" />
        </el-select>
        <el-select v-model="stateFilter" class="admin-page__select" aria-label="运维操作状态" placeholder="状态：全部" clearable @change="applyFilters">
          <el-option v-for="(label, value) in maintenanceStates" :key="value" :value="value" :label="label" />
        </el-select>
      </div>
      <div class="admin-page__search">
        <el-button :icon="RefreshCw" :loading="loading" @click="load">刷新</el-button>
      </div>
    </div>
    <el-table v-loading="loading" :data="operations" row-key="id" aria-label="运维操作列表">
      <el-table-column label="操作类型" width="132">
        <template #default="{ row }">{{ maintenanceActions[(row as MaintenanceOperation).action] }}</template>
      </el-table-column>
      <el-table-column label="状态" width="130">
        <template #default="{ row }">
          <SemanticTag :tone="row.stage === 'stop_unconfirmed' ? 'attention' : maintenanceSemanticTone(row.state)"
            :label="row.stage === 'stop_unconfirmed' ? '停止待确认' : maintenanceStates[(row as MaintenanceOperation).state]" />
        </template>
      </el-table-column>
      <el-table-column label="发起时间" width="176">
        <template #default="{ row }">{{ formatTime(row.requested_at) }}</template>
      </el-table-column>
      <el-table-column prop="error_message" label="说明" min-width="180" show-overflow-tooltip />
      <el-table-column label="操作" width="96" align="right" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="openOperation(row.id)">详情</el-button>
        </template>
      </el-table-column>
      <template #empty><el-empty v-if="!loading" :image-size="72" description="暂无匹配运维操作" /></template>
    </el-table>
    <div class="admin-page__footer">
      <el-pagination v-model:current-page="page" v-model:page-size="pageSize" :page-sizes="[10, 20, 50]" :total="total" layout="total, sizes, prev, pager, next" aria-label="运维操作分页" />
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { RefreshCw } from 'lucide-vue-next'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import type { Worker } from '@/api/types'
import { listMaintenanceOperations, submitMaintenance, type MaintenanceOperation, type MaintenanceAction, type WorkerHealth, type WorkerEquipmentItem } from '@/api/maintenance'
import { maintenanceActions, maintenanceActive, maintenanceStates } from '@/features/workers/maintenance'
import { maintenanceSemanticTone, type SemanticTone } from '@/features/workers/semantic'
import { formatTime } from '@/utils/format'

const props = defineProps<{ worker: Worker; health?: WorkerHealth }>()
const router = useRouter()
const operations = ref<MaintenanceOperation[]>([])
const error = ref('')
const loading = ref(false)
const submitting = ref(false)
const actionFilter = ref('')
const stateFilter = ref('')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const readiness: Record<WorkerHealth['readiness'], { label: string; tone: SemanticTone }> = {
  READY: { label: '就绪', tone: 'success' },
  DRIFTED: { label: '待同步', tone: 'attention' },
  DEGRADED: { label: '异常', tone: 'danger' },
  UNKNOWN: { label: '未检查', tone: 'neutral' },
}
const equipmentState: Record<WorkerEquipmentItem['state'], { label: string; tone: SemanticTone }> = {
  ready: { label: '正常', tone: 'success' },
  drifted: { label: '待同步', tone: 'attention' },
  degraded: { label: '异常', tone: 'danger' },
  unknown: { label: '未检查', tone: 'neutral' },
}
function diagnostics(item: WorkerEquipmentItem) {
  const reasons = item.key === 'git' && item.state === 'ready' ? [] : [...item.diff]
  const error = item.error || (item.key === 'git' ? props.health?.repository?.error : null)
  if (error && !reasons.some(reason => reason.includes(error))) reasons.push(error)
  return [...new Set(reasons)]
}
const blocking = computed(() =>
  props.health?.blocking.filter(item => ['maintenance_active', 'occupied_task'].includes(item.code)).map(item => item.message).join('；'),
)
let request: AbortController | undefined
let timer: ReturnType<typeof setInterval> | undefined
let refreshTick = 0

async function load(background = false) {
  if (background && (request || document.hidden)) return
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = !background
  error.value = ''
  const query = new URLSearchParams({
    limit: String(pageSize.value), offset: String((page.value - 1) * pageSize.value),
  })
  if (actionFilter.value) query.set('action', actionFilter.value)
  if (stateFilter.value) query.set('state', stateFilter.value)
  try {
    const result = await listMaintenanceOperations(props.worker.id, query, controller.signal)
    if (request !== controller) return
    operations.value = result.items
    total.value = result.total
  } catch (reason) {
    if (!controller.signal.aborted) error.value = String(reason)
  } finally {
    if (request === controller) {
      request = undefined
      loading.value = false
    }
  }
}
function applyFilters() {
  if (page.value === 1) void load()
  else page.value = 1
}
function openOperation(operationId: string) {
  return router.push({ name: 'settings-worker-maintenance', params: { id: props.worker.id, operationId } })
}
function canSubmit(action: MaintenanceAction) {
  const worker = props.worker
  if (submitting.value || !props.health || props.health.blocking.some(item => item.code === 'maintenance_active')) return false
  const online = props.health.connection.state === 'online'
  if (action === 'check') return online
  if (worker.mode !== 'LINUX_REMOTE' || blocking.value || worker.occupied_tasks.length) return false
  if (action === 'deploy') return Boolean(worker.host && worker.username && worker.has_ssh_password)
  if (action === 'update') return online && Boolean(worker.repository_url)
  return online || worker.status === 'OFFLINE'
}
async function submit(action: MaintenanceAction) {
  submitting.value = true
  try {
    if (action !== 'check') {
      await ElMessageBox.confirm('维护将操作 Worker 的原生运行环境。有未释放任务时禁止执行。', maintenanceActions[action], { type: 'warning' })
    }
    const operation = await submitMaintenance(props.worker.id, action)
    await openOperation(operation.id)
  } catch (reason) {
    if (reason !== 'cancel' && reason !== 'close') error.value = String(reason)
  } finally {
    submitting.value = false
  }
}
watch([page, pageSize], () => load())
watch(() => props.health?.maintenance, (current, previous) => {
  if (current && (current.operation_id !== previous?.operation_id || current.state !== previous?.state)) {
    void load(true)
  }
})
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (!document.hidden && (++refreshTick % 3 === 0 || operations.value.some(operation => maintenanceActive(operation.state)))) {
      void load(true)
    }
  }, 3000)
})
onUnmounted(() => {
  clearInterval(timer)
  request?.abort()
})
</script>

<style scoped>
.equipment { margin-top: 8px; }
.equipment-row { display: flex; flex-wrap: wrap; align-items: center; gap: 6px 12px; }
.repository-url { max-width: min(380px, 100%); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.equipment-reasons { margin: 4px 0 0; padding: 0; list-style: none; color: var(--astra-text-muted); overflow-wrap: anywhere; }
.equipment-reasons--error { color: var(--astra-danger); }
</style>
