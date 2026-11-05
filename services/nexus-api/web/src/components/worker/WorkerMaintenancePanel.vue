<template>
  <section class="admin-page" data-testid="worker-maintenance">
    <AstraPageHeader title="运维操作">
      <template #default>
        <span v-if="health">运行环境 · {{ readinessLabels[health.readiness] }}</span>
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
import SemanticTag from '@/components/astra/SemanticTag.vue'
import type { Worker } from '@/api/types'
import { getWorkerHealth, listMaintenanceOperations, submitMaintenance, type MaintenanceOperation, type MaintenanceAction, type WorkerHealth } from '@/api/maintenance'
import { maintenanceActions, maintenanceActive, maintenanceStates } from '@/features/workers/maintenance'
import { maintenanceSemanticTone } from '@/features/workers/semantic'
import { formatTime } from '@/utils/format'

const props = defineProps<{ worker: Worker }>()
const router = useRouter()
const operations = ref<MaintenanceOperation[]>([])
const health = ref<WorkerHealth>()
const error = ref('')
const loading = ref(false)
const submitting = ref(false)
const actionFilter = ref('')
const stateFilter = ref('')
const page = ref(1)
const pageSize = ref(10)
const total = ref(0)
const readinessLabels = { READY: '就绪', DRIFTED: '待同步', DEGRADED: '不可用', UNKNOWN: '未检查' }
const blocking = computed(() =>
  health.value?.blocking.filter(item => ['maintenance_active', 'occupied_task'].includes(item.code)).map(item => item.message).join('；'),
)
let request: AbortController | undefined
let timer: ReturnType<typeof setInterval> | undefined

async function load(background = false) {
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
    const [result, status] = await Promise.all([
      listMaintenanceOperations(props.worker.id, query, controller.signal),
      getWorkerHealth(props.worker.id, controller.signal),
    ])
    if (request !== controller) return
    operations.value = result.items
    total.value = result.total
    health.value = status
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
  if (submitting.value || !health.value || health.value.blocking.some(item => item.code === 'maintenance_active')) return false
  if (action === 'check') return true
  if (worker.mode !== 'LINUX_REMOTE' || blocking.value) return false
  if (action === 'deploy') return Boolean(worker.host && worker.username && worker.has_ssh_password)
  if (action === 'update') return Boolean(worker.repository_url)
  return true
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
onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (health.value?.maintenance && maintenanceActive(health.value.maintenance.state) && !request) void load(true)
  }, 3000)
})
onUnmounted(() => {
  clearInterval(timer)
  request?.abort()
})
</script>
