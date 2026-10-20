<template>
  <section class="worker-maintenance" data-testid="worker-maintenance-panel">
    <div class="worker-maintenance__toolbar">
      <ElSpace wrap :size="8">
        <ElButton
          data-testid="check-environment"
          :icon="Stethoscope"
          :loading="busyAction === 'check'"
          :disabled="actionDisabled('check')"
          @click="runAction('check')"
        >检查</ElButton>
        <ElButton
          data-testid="deploy-environment"
          type="primary"
          :loading="busyAction === 'deploy'"
          :disabled="actionDisabled('deploy')"
          @click="runAction('deploy')"
        >一键部署</ElButton>
        <ElButton
          data-testid="update-environment"
          :loading="busyAction === 'update'"
          :disabled="actionDisabled('update')"
          @click="runAction('update')"
        >更新</ElButton>
        <ElButton
          data-testid="restart-environment"
          :loading="busyAction === 'restart'"
          :disabled="actionDisabled('restart')"
          @click="runAction('restart')"
        >重启</ElButton>
        <ElButton
          v-if="workerDisabled || needsReconcile"
          data-testid="restore-environment"
          :loading="busyAction === 'restore'"
          :disabled="actionDisabled('restore')"
          @click="runAction('restore')"
        >恢复</ElButton>
        <ElButton
          v-else-if="health?.enabled === true"
          data-testid="disable-environment"
          type="danger"
          plain
          :loading="busyAction === 'disable'"
          :disabled="actionDisabled('disable')"
          @click="runAction('disable')"
        >禁用</ElButton>
      </ElSpace>
      <ElButton data-testid="refresh-worker-maintenance" :icon="RefreshCw" :loading="loading" @click="controller.load">刷新</ElButton>
    </div>

    <p class="worker-maintenance__help">
      检查只读取现状；一键部署不会主动更新已有仓库代码；更新才会获取目标分支；重启会中断执行资源，任务或等待确认期间不可执行。禁用只停止新调度，不停止进程、不释放当前任务，当前任务可继续完成；禁用不会删除环境、仓库或维护记录。
    </p>

    <ElAlert
      v-if="blockedNote"
      class="worker-maintenance__alert"
      type="warning"
      show-icon
      :closable="false"
      :title="blockedNote"
    >
      <ul v-if="blockers.length" class="worker-maintenance__blockers">
        <li v-for="reason in blockers" :key="reason">{{ reason }}</li>
      </ul>
    </ElAlert>

    <ElAlert
      v-if="activeOperation && activeOperation.id !== operation?.id"
      class="worker-maintenance__alert"
      type="info"
      show-icon
      :closable="false"
      title="有进行中的维护操作"
    >
      <p>{{ maintenanceActionLabel(activeOperation.action) }} · {{ maintenanceStageLabel(activeOperation.stage) }} · {{ activeOperation.id }}</p>
      <ElButton size="small" @click="controller.selectOperation(activeOperation.id, { force: true })">查看进行中的操作</ElButton>
    </ElAlert>

    <ElAlert
      v-if="healthError"
      class="worker-maintenance__alert"
      type="error"
      show-icon
      :closable="false"
      title="无法读取 Worker 维护状态"
    >
      <p>{{ healthError }}</p>
      <ElButton size="small" @click="controller.load">重新加载</ElButton>
    </ElAlert>

    <ElAlert
      v-if="actionError"
      class="worker-maintenance__alert"
      data-testid="worker-maintenance-error"
      type="error"
      show-icon
      @close="controller.clearActionError"
      :title="actionError"
    />

    <div v-if="operation" class="worker-maintenance__operation">
      <div class="worker-maintenance__operation-head">
        <div class="worker-maintenance__operation-title">
          <ElTag :type="maintenanceStateTone(operation.state)" size="small">{{ maintenanceStateLabel(operation.state) }}</ElTag>
          <strong>{{ maintenanceActionLabel(operation.action) }}</strong>
          <ElText type="info" size="small">第 {{ operation.attempt }} 次尝试</ElText>
          <IdDisplay :value="operation.id" :max-width="220" />
        </div>
        <ElSpace :size="8">
          <ElButton
            v-if="canCancel"
            data-testid="cancel-maintenance-operation"
            size="small"
            :loading="busyAction === 'cancel'"
            :disabled="busyAction !== null"
            @click="cancelOperation"
          >取消操作</ElButton>
          <ElButton
            v-if="canRetry"
            data-testid="retry-maintenance-operation"
            size="small"
            type="primary"
            :loading="busyAction === 'retry'"
            :disabled="busyAction !== null"
            @click="retryOperation"
          >重试</ElButton>
        </ElSpace>
      </div>

      <ElDescriptions :column="3" border size="small">
        <ElDescriptionsItem label="当前阶段">{{ maintenanceStageLabel(operation.stage) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="请求人">{{ operation.requested_by || '—' }}</ElDescriptionsItem>
        <ElDescriptionsItem label="申请时间">{{ formatTime(operation.requested_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="开始时间">{{ formatTime(operation.started_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="结束时间">{{ formatTime(operation.finished_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="取消请求">{{ operation.cancel_requested ? '已请求，等待安全检查点' : '无' }}</ElDescriptionsItem>
        <ElDescriptionsItem v-if="operationReason(operation)" label="原因" :span="3">
          <span class="worker-maintenance__error">{{ operationReason(operation) }}</span>
        </ElDescriptionsItem>
        <ElDescriptionsItem v-if="changed.length" label="本次变更" :span="3">
          <ElSpace wrap :size="6">
            <ElTag v-for="item in changed" :key="item" size="small" effect="plain">{{ item }}</ElTag>
          </ElSpace>
        </ElDescriptionsItem>
      </ElDescriptions>

      <div v-if="revisions.length" class="worker-maintenance__revisions">
        <ElTable :data="revisions" size="small">
          <ElTableColumn label="项目" prop="label" min-width="130" />
          <ElTableColumn label="目标版本" min-width="170"><template #default="{ row }">{{ row.target || '未核验' }}</template></ElTableColumn>
          <ElTableColumn label="实际版本" min-width="170"><template #default="{ row }">{{ row.actual || '未核验' }}</template></ElTableColumn>
        </ElTable>
      </div>

      <div class="worker-maintenance__logs">
        <div class="worker-maintenance__log-head">
          <div class="worker-maintenance__log-title">
            <h4>维护日志</h4>
            <ElTag size="small" :type="streamTone">{{ streamLabel }}</ElTag>
            <ElText type="info" size="small">{{ streamNote }}</ElText>
          </div>
          <ElSpace :size="8">
            <ElText type="info" size="small">{{ events.length }} 条</ElText>
            <ElButton size="small" :loading="historyLoading" :disabled="!operation || busyAction !== null" @click="controller.loadHistoryPage(true)">分页回放</ElButton>
            <ElButton v-if="historyHasMore" size="small" :loading="historyLoading" :disabled="busyAction !== null" @click="controller.loadHistoryPage(false)">加载更多</ElButton>
          </ElSpace>
        </div>
        <ElText type="info" size="small">
          每页 {{ MAINTENANCE_EVENT_PAGE_SIZE }} 条分页回放；实时日志由 SSE 续传与重连。
        </ElText>
        <ElAlert v-if="historyError" type="error" show-icon :closable="false" :title="historyError" />
        <div v-if="events.length" class="worker-maintenance__log-list">
          <article v-for="event in events" :key="event.id" class="worker-maintenance__log-item">
            <time>{{ formatTime(event.created_at) }}</time>
            <ElTag size="small" :type="eventTone(event)">{{ eventStateLabel(event) }}</ElTag>
            <div class="worker-maintenance__log-copy">
              <strong v-if="event.name">{{ maintenanceCopy(event.name) }}</strong>
              <span>{{ eventText(event) }}</span>
              <small>#{{ event.id }} · 第 {{ event.attempt }} 次 · {{ event.type }}<template v-if="event.stage"> · {{ maintenanceStageLabel(event.stage) }}</template></small>
              <details v-if="eventDetailText(event)">
                <summary>事件详情</summary>
                <pre>{{ eventDetailText(event) }}</pre>
              </details>
            </div>
          </article>
        </div>
        <ElText v-else type="info" size="small">尚无日志。操作开始后会持续写入并可断线续传。</ElText>
      </div>
    </div>

    <ElEmpty v-else :image-size="54" :description="loading ? '正在加载维护状态…' : '该 Worker 暂无维护记录'" />

    <div v-if="showHistory && operations.length" class="worker-maintenance__history">
      <div class="worker-maintenance__history-head">
        <h4>维护记录</h4>
        <ElText type="info" size="small">点击记录查看阶段、版本与日志</ElText>
      </div>
      <ElTable :data="operations" size="small" @row-click="onSelectOperation">
        <ElTableColumn label="操作" min-width="150">
          <template #default="{ row }">
            <div class="worker-maintenance__history-id">
              <ElTag size="small" :type="maintenanceStateTone(row.state)">{{ maintenanceStateLabel(row.state) }}</ElTag>
              <strong>{{ maintenanceActionLabel(row.action) }}</strong>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="阶段" width="120"><template #default="{ row }">{{ maintenanceStageLabel(row.stage) }}</template></ElTableColumn>
        <ElTableColumn label="尝试" width="70" align="right" prop="attempt" />
        <ElTableColumn label="申请时间" width="130"><template #default="{ row }">{{ formatTime(row.requested_at) }}</template></ElTableColumn>
        <ElTableColumn label="结束时间" width="130"><template #default="{ row }">{{ formatTime(row.finished_at) }}</template></ElTableColumn>
        <ElTableColumn label="操作 ID" min-width="170"><template #default="{ row }"><IdDisplay :value="row.id" :max-width="170" /></template></ElTableColumn>
        <ElTableColumn label="" width="80" align="right">
          <template #default="{ row }">
            <ElButton link type="primary" @click.stop="controller.selectOperation(row.id, { force: true })">查看</ElButton>
          </template>
        </ElTableColumn>
      </ElTable>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted } from 'vue'
import { ElAlert, ElButton, ElDescriptions, ElDescriptionsItem, ElEmpty, ElSpace, ElTable, ElTableColumn, ElTag, ElText } from 'element-plus'
import { RefreshCw, Stethoscope } from 'lucide-vue-next'
import type { MaintenanceAction } from '@/api/maintenance'
import { MAINTENANCE_EVENT_PAGE_SIZE } from '@/api/maintenance'
import { actionBlockedByPolicy, hasOccupancy, isActiveMaintenanceState, isInterruptedMaintenanceState } from '@/features/workers/actionPolicy'
import type { WorkerMaintenanceController } from '@/composables/useWorkerMaintenance'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import {
  blockingReasons,
  canCancelMaintenance,
  changedLabels,
  canRetryMaintenance,
  eventDetailText,
  eventStateLabel,
  eventText,
  eventTone,
  maintenanceActionLabel,
  maintenanceCopy,
  maintenanceStageLabel,
  maintenanceStateLabel,
  maintenanceStateTone,
  operationReason,
  revisionRows,
} from '@/features/workers/presentation'
import { formatTime } from '@/utils/format'

const props = withDefaults(defineProps<{
  controller: WorkerMaintenanceController
  disabledReason?: string
  showHistory?: boolean
}>(), {
  disabledReason: '',
  showHistory: true,
})

const emit = defineEmits<{ changed: [] }>()
const {
  health,
  operations,
  operation,
  events,
  loading,
  healthError,
  actionError,
  busyAction,
  streamState,
  streamNote,
  historyHasMore,
  historyLoading,
  historyError,
} = props.controller

const blockers = computed(() => blockingReasons(health.value))
const activeOperation = computed(() => operations.value.find((item) => isActiveMaintenanceState(item.state)) ?? null)
const activeMaintenance = computed(() =>
  isActiveMaintenanceState(health.value?.maintenance?.state)
  || isActiveMaintenanceState(operation.value?.state)
  || Boolean(activeOperation.value),
)
const occupancy = computed(() => hasOccupancy(health.value))
const needsReconcile = computed(() => {
  const latestState = health.value?.maintenance?.state
  if (latestState) return isInterruptedMaintenanceState(latestState)
  return isInterruptedMaintenanceState(operation.value?.state)
})
const workerDisabled = computed(() => health.value?.enabled === false)
const revisions = computed(() => revisionRows(operation.value))
const changed = computed(() => changedLabels(operation.value))
const canCancel = computed(() => canCancelMaintenance(operation.value) && !operation.value?.cancel_requested && busyAction.value !== 'cancel')
const canRetry = computed(() => canRetryMaintenance(operation.value) && busyAction.value !== 'retry')
const blockedNote = computed(() => {
  if (props.disabledReason) return props.disabledReason
  if (needsReconcile.value) return '维护执行已中断，需核对远端；可执行检查或恢复（恢复会先只读核对），无法确认时维护锁保持。'
  if (activeMaintenance.value) return '维护操作进行中，部署、更新、重启和禁用暂不可用。'
  if (occupancy.value) return '存在正在执行或等待确认的任务/会话，部署、更新和重启已暂停；禁用仍允许当前任务完成。'
  if (blockers.value.length) return '当前存在调度健康阻塞，但不阻止提交部署、更新或重启；是否安全由后端最终检查。'
  if (!health.value) return '尚未获取健康状态；部署、更新和重启仍可提交，是否安全由后端最终检查。'
  return ''
})
const streamLabel = computed(() => ({
  idle: '未订阅',
  live: '实时连接',
  reconnecting: '重连中',
  polling: '轮询同步',
  ended: '已结束',
}[streamState.value]))
const streamTone = computed(() => streamState.value === 'live' ? 'success' : streamState.value === 'polling' || streamState.value === 'reconnecting' ? 'warning' : 'info')

let unsubscribe: (() => void) | null = null
onMounted(() => {
  unsubscribe = props.controller.onChange(() => emit('changed'))
})
onBeforeUnmount(() => { unsubscribe?.() })

function actionDisabled(action: MaintenanceAction): boolean {
  if (busyAction.value !== null) return true
  if (props.disabledReason) return action !== 'check' && action !== 'restore'
  return actionBlockedByPolicy(action, health.value, {
    activeMaintenance: activeMaintenance.value,
    reconcileRequired: needsReconcile.value,
  })
}

async function runAction(action: MaintenanceAction) {
  if (actionDisabled(action)) return
  if (action !== 'check') {
    const confirmed = await confirmDialog({
      title: `${maintenanceActionLabel(action)} Agent Worker`,
      message: confirmMessage(action),
      confirmLabel: maintenanceActionLabel(action),
      danger: action === 'disable',
    })
    if (!confirmed) return
  }
  const created = await props.controller.submit(action)
  if (created) notify(`${maintenanceActionLabel(action)}已提交：${created.id}`)
  else if (actionError.value) notify(actionError.value, 'error')
}

async function cancelOperation() {
  const confirmed = await confirmDialog({
    title: '取消维护操作',
    message: '取消只停止服务端维护任务，不会回滚已经写入远端的配置或代码。危险步骤已发出时会进入待核对状态。',
    confirmLabel: '请求取消',
    danger: true,
  })
  if (!confirmed) return
  const done = await props.controller.cancel()
  if (done) notify('已请求取消维护操作')
  else if (actionError.value) notify(actionError.value, 'error')
}

async function retryOperation() {
  const updated = await props.controller.retry()
  if (updated) notify(`已开始第 ${updated.attempt} 次尝试`)
  else if (actionError.value) notify(actionError.value, 'error')
}

function onSelectOperation(row: { id: string }) {
  void props.controller.selectOperation(row.id, { force: true })
}

function confirmMessage(action: MaintenanceAction): string {
  const messages: Record<MaintenanceAction, string> = {
    check: '',
    deploy: '将安装或校验运行依赖并写入配置，不会主动更新已有仓库代码；维护失败不会把真实健康的 Worker 标成不可用。',
    update: '将获取目标分支并更新仓库代码；工作区存在未提交修改或冲突时会停止并保留现场。',
    restart: '将重启 OpenCode Serve；需要任务、等待确认和未结束会话均已清空。',
    disable: '仅停止新调度，不停止进程、不释放当前任务；当前任务可继续完成。环境、仓库、装备与维护记录不会删除。',
    restore: '恢复会先只读核对远端状态；无法确认时不会由前端释放维护锁。',
  }
  return messages[action]
}
</script>

<style scoped>
.worker-maintenance { display: grid; gap: 12px; min-width: 0; }
.worker-maintenance__toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-maintenance__help { margin: 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); line-height: 1.7; }
.worker-maintenance__alert { margin: 0; }
.worker-maintenance__blockers { margin: 4px 0 0; padding-left: 18px; }
.worker-maintenance__operation { display: grid; gap: 12px; padding: 12px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle, transparent); }
.worker-maintenance__operation-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-maintenance__operation-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.worker-maintenance__operation-title strong { font-size: var(--astra-font-label); }
.worker-maintenance__error { color: var(--el-color-danger); overflow-wrap: anywhere; }
.worker-maintenance__revisions { min-width: 0; }
.worker-maintenance__logs { display: grid; gap: 8px; }
.worker-maintenance__log-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-maintenance__log-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.worker-maintenance__log-title h4, .worker-maintenance__history-head h4 { margin: 0; font-size: var(--astra-font-label); }
.worker-maintenance__log-list { display: grid; gap: 6px; max-height: 320px; overflow: auto; padding-right: 4px; }
.worker-maintenance__log-item { display: flex; align-items: flex-start; gap: 8px; padding: 6px 8px; border-radius: 7px; background: var(--astra-bg-muted, rgba(127, 127, 127, .06)); }
.worker-maintenance__log-item time { flex: none; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-maintenance__log-copy { display: grid; gap: 2px; min-width: 0; }
.worker-maintenance__log-copy strong { font-size: var(--astra-font-body); }
.worker-maintenance__log-copy span { overflow-wrap: anywhere; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.worker-maintenance__log-copy small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-maintenance__log-copy details { margin-top: 2px; }
.worker-maintenance__log-copy summary { cursor: pointer; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-maintenance__log-copy pre { margin: 4px 0 0; max-height: 220px; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); }
.worker-maintenance__history { display: grid; gap: 8px; }
.worker-maintenance__history-head { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
.worker-maintenance__history :deep(.el-table__row) { cursor: pointer; }
.worker-maintenance__history-id { display: flex; align-items: center; gap: 6px; min-width: 0; }
@media (max-width: 720px) {
  .worker-maintenance__operation-head, .worker-maintenance__log-head { align-items: flex-start; flex-direction: column; }
}
</style>
