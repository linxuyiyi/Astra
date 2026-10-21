<template>
  <section class="worker-maintenance" data-testid="worker-maintenance-panel">
    <div class="worker-maintenance__toolbar">
      <ElSpace wrap :size="8">
        <ElTooltip content="只读取现状，不写配置、不安装、不重启" placement="top">
          <ElButton
            data-testid="check-environment"
            :icon="Stethoscope"
            :loading="busyAction === 'check'"
            :disabled="actionDisabled('check')"
            @click="runAction('check')"
          >检查</ElButton>
        </ElTooltip>
        <ElTooltip v-if="mode === 'LINUX_REMOTE'" content="安装或校验运行依赖并写入配置，不主动更新已有仓库代码" placement="top">
          <ElButton
            data-testid="deploy-environment"
            type="primary"
            :loading="busyAction === 'deploy'"
            :disabled="actionDisabled('deploy')"
            @click="runAction('deploy')"
          >一键部署</ElButton>
        </ElTooltip>
        <ElTooltip v-if="mode === 'LINUX_REMOTE'" content="获取目标分支并更新仓库代码；工作区有未提交修改时会停止" placement="top">
          <ElButton
            data-testid="update-environment"
            :loading="busyAction === 'update'"
            :disabled="actionDisabled('update')"
            @click="runAction('update')"
          >更新</ElButton>
        </ElTooltip>
        <ElTooltip v-if="mode === 'LINUX_REMOTE'" content="重启 OpenCode 服务；需要任务与未结束会话均已清空" placement="top">
          <ElButton
            data-testid="restart-environment"
            :loading="busyAction === 'restart'"
            :disabled="actionDisabled('restart')"
            @click="runAction('restart')"
          >重启</ElButton>
        </ElTooltip>
        <ElTooltip v-if="workerDisabled || needsReconcile" content="先只读核对远端状态，无法确认时不释放维护锁" placement="top">
          <ElButton
            data-testid="restore-environment"
            :loading="busyAction === 'restore'"
            :disabled="actionDisabled('restore')"
            @click="runAction('restore')"
          >恢复</ElButton>
        </ElTooltip>
        <ElTooltip v-else-if="health?.enabled === true" content="只停止新调度，不停止进程、不释放当前任务" placement="top">
          <ElButton
            data-testid="disable-environment"
            type="danger"
            plain
            :loading="busyAction === 'disable'"
            :disabled="actionDisabled('disable')"
            @click="runAction('disable')"
          >禁用</ElButton>
        </ElTooltip>
      </ElSpace>
      <ElButton data-testid="refresh-worker-maintenance" :icon="RefreshCw" :loading="loading" @click="controller.load">刷新</ElButton>
    </div>

    <ElAlert
      v-if="actionError"
      class="worker-maintenance__alert"
      data-testid="worker-maintenance-error"
      type="error"
      show-icon
      @close="controller.clearActionError"
      :title="actionError"
    />

    <ElAlert
      v-if="blockedNote"
      class="worker-maintenance__alert"
      type="warning"
      show-icon
      :closable="false"
      :title="blockedNote"
    />

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

    <div v-if="summaryOperation" class="worker-maintenance__summary">
      <SemanticTag :tone="maintenanceStateTone(summaryOperation.state)" :label="maintenanceStateLabel(summaryOperation.state)" />
      <strong>{{ maintenanceActionLabel(summaryOperation.action) }}</strong>
      <ElText>{{ maintenanceStageLabel(summaryOperation.stage) }}</ElText>
      <ElButton link type="primary" @click="viewOperation(summaryOperation.id)">查看详情</ElButton>
    </div>

    <ElEmpty v-if="!loading && !operations.length && !healthError" :image-size="48" description="暂无维护记录" />


    <div v-if="showHistory && historyOperations.length" class="worker-maintenance__history">
      <div class="worker-maintenance__history-head"><h4>维护记录</h4></div>
      <ElTable v-loading="loading || detailLoading" :data="historyOperations" size="small" @row-click="onSelectOperation">
        <ElTableColumn label="操作" min-width="110"><template #default="{ row }">{{ maintenanceActionLabel(row.action) }}</template></ElTableColumn>
        <ElTableColumn label="结果" width="110"><template #default="{ row }"><SemanticTag :tone="maintenanceStateTone(row.state)" :label="maintenanceStateLabel(row.state)" /></template></ElTableColumn>
        <ElTableColumn label="发起人" min-width="150" show-overflow-tooltip><template #default="{ row }">{{ requesterLabel(row.requested_by) }}</template></ElTableColumn>
        <ElTableColumn label="申请时间" width="130"><template #default="{ row }">{{ formatTime(row.requested_at) }}</template></ElTableColumn>
        <ElTableColumn label="结果说明" min-width="170" show-overflow-tooltip><template #default="{ row }">{{ operationReason(row as MaintenanceOperation) || maintenanceStageLabel(row.stage) }}</template></ElTableColumn>
        <ElTableColumn label="操作" width="80" align="right"><template #default="{ row }"><ElButton link type="primary" @click.stop="viewOperation(row.id)">详情</ElButton></template></ElTableColumn>
      </ElTable>
    </div>

    <ElDrawer v-model="detailsOpen" title="维护记录详情" size="min(760px, 96vw)" append-to-body destroy-on-close>
    <div v-if="operation" class="worker-maintenance__operation">
      <div class="worker-maintenance__operation-head">
        <div class="worker-maintenance__operation-title">
          <SemanticTag :tone="maintenanceStateTone(operation.state)" :label="maintenanceStateLabel(operation.state)" />
          <strong>{{ maintenanceActionLabel(operation.action) }}</strong>
          <ElText size="small">第 {{ operation.attempt }} 次尝试</ElText>
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

      <p v-if="operationReason(operation)" class="worker-maintenance__reason" :data-state="operation.state">{{ operationReason(operation) }}</p>

      <ElDescriptions :column="3" border size="small">
        <ElDescriptionsItem label="请求人">{{ requesterLabel(operation.requested_by) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="当前阶段">{{ maintenanceStageLabel(operation.stage) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="申请时间">{{ formatTime(operation.requested_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="开始时间">{{ formatTime(operation.started_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="结束时间">{{ formatTime(operation.finished_at) }}</ElDescriptionsItem>
        <ElDescriptionsItem label="取消请求">{{ operation.cancel_requested ? '已请求，等待安全检查点' : '无' }}</ElDescriptionsItem>
        <ElDescriptionsItem v-if="changed.length" label="本次变更" :span="3">
          <ElSpace wrap :size="6">
            <SemanticTag v-for="item in changed" :key="item" tone="neutral" :label="item" />
          </ElSpace>
        </ElDescriptionsItem>
      </ElDescriptions>

      <ElCollapse v-model="expandedDetails" class="worker-maintenance__detail">
        <ElCollapseItem v-if="revisions.length" title="版本差异" name="revisions">
          <ElTable :data="revisions" size="small">
            <ElTableColumn label="项目" prop="label" min-width="130" />
            <ElTableColumn label="目标版本" min-width="170"><template #default="{ row }">{{ row.target || '未核验' }}</template></ElTableColumn>
            <ElTableColumn label="实际版本" min-width="170"><template #default="{ row }">{{ row.actual || '未核验' }}</template></ElTableColumn>
          </ElTable>
        </ElCollapseItem>
        <ElCollapseItem title="维护日志" name="logs">
          <div class="worker-maintenance__logs">
            <div class="worker-maintenance__log-head">
              <div class="worker-maintenance__log-title">
                <SemanticTag :tone="streamTone" :label="streamLabel" />
                <ElText size="small">{{ streamNote }}</ElText>
              </div>
              <ElSpace :size="8">
                <ElButton size="small" :loading="historyLoading" :disabled="!operation || busyAction !== null" @click="controller.loadHistoryPage(true)">刷新日志</ElButton>
                <ElButton v-if="historyHasMore" size="small" :loading="historyLoading" :disabled="busyAction !== null" @click="controller.loadHistoryPage(false)">加载更多</ElButton>
              </ElSpace>
            </div>

            <ElAlert v-if="historyError" type="error" show-icon :closable="false" :title="historyError" />
            <div v-if="events.length" class="worker-maintenance__log-list">
              <article v-for="event in events" :key="event.id" class="worker-maintenance__log-item">
                <time>{{ formatTime(event.created_at) }}</time>
                <SemanticTag :tone="eventTone(event)" :label="eventStateLabel(event)" />
                <div class="worker-maintenance__log-copy">
                  <strong v-if="event.name">{{ maintenanceCopy(event.name) }}</strong>
                  <span>{{ eventText(event) }}</span>
                  <small>
                    #{{ event.id }} · 第 {{ event.attempt }} 次 · {{ event.type }}<template v-if="event.stage"> · {{ maintenanceStageLabel(event.stage) }}</template>
                    <template v-if="eventActor(event)"> · 操作者 {{ eventActor(event) }}</template>
                  </small>
                  <details v-if="eventDetailText(event)">
                    <summary>事件详情</summary>
                    <pre>{{ eventDetailText(event) }}</pre>
                  </details>
                </div>
              </article>
            </div>
            <ElText v-else size="small">暂无日志。</ElText>
          </div>
        </ElCollapseItem>
      </ElCollapse>
    </div>

    </ElDrawer>
  </section>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ElAlert, ElButton, ElCollapse, ElCollapseItem, ElDescriptions, ElDescriptionsItem, ElDrawer, ElEmpty, ElSpace, ElTable, ElTableColumn, ElText, ElTooltip, vLoading } from 'element-plus'
import { RefreshCw, Stethoscope } from 'lucide-vue-next'
import type { MaintenanceAction, MaintenanceEvent, MaintenanceOperation } from '@/api/maintenance'
import { actionBlockedByPolicy, hasOccupancy, isActiveMaintenanceState, isInterruptedMaintenanceState } from '@/features/workers/actionPolicy'
import type { WorkerMaintenanceController } from '@/composables/useWorkerMaintenance'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import {
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
  requesterLabel,
  revisionRows,
} from '@/features/workers/presentation'
import type { SemanticTone } from '@/features/workers/semantic'
import { formatTime } from '@/utils/format'

const props = withDefaults(defineProps<{
  controller: WorkerMaintenanceController
  mode: 'LOCAL' | 'LINUX_REMOTE' | null
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

const detailsOpen = ref(false)
const detailLoading = ref(false)
const expandedDetails = ref<string[]>([])
watch(() => operation.value?.id, () => { expandedDetails.value = [] })
watch(() => health.value?.environment_id, () => { detailsOpen.value = false })
const activeOperation = computed(() => operations.value.find((item) => isActiveMaintenanceState(item.state)) ?? null)
const activeMaintenance = computed(() =>
  isActiveMaintenanceState(health.value?.maintenance?.state)
  || isActiveMaintenanceState(operation.value?.state)
  || Boolean(activeOperation.value),
)
const summaryOperation = computed(() => activeOperation.value ?? (!props.showHistory ? operation.value : null))
const historyOperations = computed(() => operations.value.filter(item => item.id !== activeOperation.value?.id))
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
const canRetry = computed(() => canRetryMaintenance(operation.value) && !activeMaintenance.value && busyAction.value === null)
const blockedNote = computed(() => {
  if (props.disabledReason) return props.disabledReason
  if (needsReconcile.value) return '维护执行已中断，需核对远端；可执行检查或恢复（恢复会先只读核对），无法确认时维护锁保持。'
  if (activeMaintenance.value) return '维护操作进行中，部署、更新、重启和禁用暂不可用。'
  if (occupancy.value) return '存在正在执行或等待确认的任务/会话，部署、更新和重启已暂停；禁用仍允许当前任务完成。'
  if (!health.value) return '尚未获取健康状态；部署、更新和重启仍可提交，是否安全由后端最终检查。'
  return ''
})
const streamLabel = computed(() => ({
  idle: '未订阅',
  live: '同步正常',
  reconnecting: '重连中',
  polling: '正在同步',
  ended: '已结束',
}[streamState.value]))
const streamTone = computed<SemanticTone>(() => {
  if (streamState.value === 'live') return 'success'
  if (streamState.value === 'polling' || streamState.value === 'reconnecting') return 'attention'
  return 'neutral'
})

let unsubscribe: (() => void) | null = null
onMounted(() => {
  unsubscribe = props.controller.onChange(() => emit('changed'))
})
onBeforeUnmount(() => { unsubscribe?.() })

function eventActor(event: MaintenanceEvent): string {
  const value = event.detail?.requested_by
  return typeof value === 'string' ? value : ''
}

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
  if (!canRetry.value) return
  const action = operation.value?.action as MaintenanceAction | undefined
  if (action && action !== 'check') {
    const confirmed = await confirmDialog({ title: `重试${maintenanceActionLabel(action)}？`, message: confirmMessage(action), confirmLabel: '重试', danger: action === 'disable' || action === 'restart' })
    if (!confirmed) return
  }
  const updated = await props.controller.retry()
  if (updated) notify(`已开始第 ${updated.attempt} 次尝试`)
  else if (actionError.value) notify(actionError.value, 'error')
}

function onSelectOperation(row: { id: string }) { void viewOperation(row.id) }

async function viewOperation(id: string) {
  detailLoading.value = true
  try {
    await props.controller.selectOperation(id, { force: true })
    if (operation.value?.id === id && !actionError.value) detailsOpen.value = true
  } finally { detailLoading.value = false }
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
.worker-maintenance__alert { margin: 0; }
.worker-maintenance__summary { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; padding: 12px; border: 1px solid var(--astra-border); border-radius: 8px; }
.worker-maintenance__reason { margin: 0; color: var(--astra-text-secondary); overflow-wrap: anywhere; line-height: 1.6; }
.worker-maintenance__reason[data-state='FAILED'] { color: var(--astra-danger); }
.worker-maintenance__reason[data-state='INTERRUPTED'] { color: var(--astra-warning); }
.worker-maintenance__operation { display: grid; gap: 12px; padding: 12px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-subtle, transparent); }
.worker-maintenance__operation-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-maintenance__operation-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; min-width: 0; }
.worker-maintenance__operation-title strong { font-size: var(--astra-font-label); }
.worker-maintenance__detail { --el-collapse-header-height: 36px; }
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
