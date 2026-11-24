<template>
  <section class="wmp" data-testid="worker-maintenance">
    <!-- 运行环境诊断卡 -->
    <section class="wmp-card" aria-label="运行环境">
      <header class="wmp-card__head">
        <div class="wmp-card__title">
          <h3>运行环境</h3>
          <SemanticTag
            :tone="readiness[health?.readiness ?? 'UNKNOWN'].tone"
            :label="readiness[health?.readiness ?? 'UNKNOWN'].label"
          />
          <span v-if="health?.connection.last_checked_at" class="wmp-meta-time">
            检查于 {{ formatTime(health.connection.last_checked_at) }}
          </span>
        </div>
        <div v-if="activeMaintenance" class="wmp-card__actions">
          <button
            type="button"
            class="wmp-live-pill"
            @click="openOperation(activeMaintenance.operation_id)"
          >
            <i class="astra-service-dot" data-state="live" aria-hidden="true" />
            <span>维护进行中 · 查看日志</span>
            <ArrowUpRight :size="13" aria-hidden="true" />
          </button>
        </div>
      </header>

      <div class="wmp-card__body">
        <div v-if="health" class="wmp-env-list">
          <div v-for="item in health.runtime_health.equipment" :key="item.key" class="wmp-env-row">
            <div class="wmp-env-row__head">
              <span class="wmp-env-row__label">{{ item.label }}</span>
              <SemanticTag :tone="equipmentState[item.state].tone" :label="equipmentState[item.state].label" />
            </div>

            <div class="wmp-env-row__detail">
              <div class="wmp-env-facts">
                <template v-if="item.key === 'opencode'">
                  <span v-if="item.actual_revision" class="wmp-chip wmp-chip--mono">v{{ item.actual_revision }}</span>
                  <span
                    v-if="item.desired_revision && item.actual_revision && item.desired_revision !== item.actual_revision"
                    class="wmp-chip wmp-chip--warn"
                  >
                    期望 v{{ item.desired_revision }}
                  </span>
                  <span v-else-if="!item.actual_revision && item.detail" class="wmp-fact-text">{{ item.detail }}</span>
                </template>

                <template v-else-if="item.key === 'git' && health.repository">
                  <span
                    class="wmp-chip"
                    :class="
                      health.repository.authentication_verified === true
                        ? 'wmp-chip--ok'
                        : health.repository.authentication_verified === false
                          ? 'wmp-chip--err'
                          : ''
                    "
                  >
                    远端{{
                      health.repository.authentication_verified === true
                        ? '已验证'
                        : health.repository.authentication_verified === false
                          ? '验证失败'
                          : '未检查'
                    }}
                  </span>
                  <span v-if="health.repository.branch" class="wmp-chip wmp-chip--mono">
                    <GitBranch :size="11" aria-hidden="true" />
                    {{ health.repository.branch }}
                  </span>
                  <span v-if="health.repository.commit" class="wmp-chip wmp-chip--mono">
                    <IdDisplay :value="health.repository.commit" :max-width="10" />
                  </span>
                  <span
                    v-if="health.repository.dirty !== null"
                    class="wmp-chip"
                    :class="health.repository.dirty ? 'wmp-chip--warn' : 'wmp-chip--ok'"
                  >
                    {{ health.repository.dirty ? '有未提交修改' : '工作树干净' }}
                  </span>
                  <span
                    v-if="health.repository.url"
                    class="wmp-repo-url"
                    :title="health.repository.url"
                  >
                    {{ health.repository.url }}
                  </span>
                </template>

                <template v-else-if="item.detail">
                  <span class="wmp-fact-text">{{ item.detail }}</span>
                </template>
              </div>

              <ul
                v-if="diagnostics(item).length"
                class="wmp-reasons"
                :class="{ 'wmp-reasons--error': item.state === 'degraded' || Boolean(item.error) }"
              >
                <li v-for="reason in diagnostics(item)" :key="reason">{{ reason }}</li>
              </ul>
            </div>
          </div>
        </div>
        <el-empty v-else :image-size="48" description="暂无运行环境诊断" />
      </div>
    </section>

    <!-- 运维操作与执行历史卡 -->
    <section class="wmp-card" aria-label="运维操作">
      <header class="wmp-card__head">
        <div class="wmp-card__title">
          <h3>运维操作</h3>
          <span class="wmp-count">共 {{ total }} 条</span>
        </div>

        <div class="wmp-ops-bar">
          <el-button
            :icon="Activity"
            :disabled="!canSubmit('check')"
            :title="disabledReason('check')"
            @click="submit('check')"
          >
            {{ maintenanceActions.check }}
          </el-button>
          <span class="wmp-ops-divider" aria-hidden="true" />
          <el-button
            :icon="GitBranch"
            :disabled="!canSubmit('update')"
            :title="disabledReason('update')"
            @click="submit('update')"
          >
            {{ maintenanceActions.update }}
          </el-button>
          <el-button
            :icon="RotateCw"
            :disabled="!canSubmit('restart')"
            :title="disabledReason('restart')"
            @click="submit('restart')"
          >
            {{ maintenanceActions.restart }}
          </el-button>
          <el-button
            type="primary"
            :icon="Rocket"
            :disabled="!canSubmit('deploy')"
            :title="disabledReason('deploy')"
            @click="submit('deploy')"
          >
            {{ maintenanceActions.deploy }}
          </el-button>
        </div>
      </header>

      <div v-if="blockingItems.length" class="wmp-blocking" role="status">
        <AlertTriangle class="wmp-blocking__icon" :size="15" aria-hidden="true" />
        <span class="wmp-blocking__title">暂不可变更</span>
        <div class="wmp-blocking__list">
          <template v-for="(item, index) in blockingItems" :key="`${item.code}-${index}`">
            <RouterLink
              v-if="item.task_id"
              class="wmp-blocking__chip wmp-blocking__chip--link"
              :to="{ name: 'task-detail', params: { id: item.task_id } }"
            >
              <span>{{ item.message }}</span>
              <ArrowUpRight :size="12" aria-hidden="true" />
            </RouterLink>
            <button
              v-else-if="item.code === 'maintenance_active' && health?.maintenance?.operation_id"
              type="button"
              class="wmp-blocking__chip wmp-blocking__chip--link"
              @click="openOperation(health.maintenance.operation_id)"
            >
              <span>{{ item.message }}</span>
              <ArrowUpRight :size="12" aria-hidden="true" />
            </button>
            <span v-else class="wmp-blocking__chip">{{ item.message }}</span>
          </template>
        </div>
      </div>

      <div class="wmp-card__body wmp-card__body--stack">
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
          <el-button @click="load()">重试</el-button>
        </el-alert>

        <div class="wmp-toolbar" role="search" aria-label="运维操作筛选">
          <div class="wmp-toolbar__filters">
            <el-select
              v-model="actionFilter"
              class="wmp-select"
              aria-label="运维操作类型"
              placeholder="类型：全部"
              clearable
              @change="applyFilters"
            >
              <el-option v-for="(label, value) in maintenanceActions" :key="value" :value="value" :label="label" />
            </el-select>
            <el-select
              v-model="stateFilter"
              class="wmp-select"
              aria-label="运维操作状态"
              placeholder="状态：全部"
              clearable
              @change="applyFilters"
            >
              <el-option v-for="(label, value) in maintenanceStates" :key="value" :value="value" :label="label" />
            </el-select>
            <el-button v-if="actionFilter || stateFilter" text @click="resetFilters">清空筛选</el-button>
          </div>
          <div class="wmp-toolbar__right">
            <el-button :icon="RefreshCw" :loading="loading" @click="load()">刷新</el-button>
          </div>
        </div>

        <el-table
          v-loading="loading"
          :data="operations"
          row-key="id"
          class="wmp-table"
          aria-label="运维操作列表"
          @row-click="onRowClick"
        >
          <el-table-column label="操作" width="126">
            <template #default="{ row }">
              <div class="wmp-op-cell">
                <span class="wmp-op-name">{{ maintenanceActions[(row as MaintenanceOperation).action] }}</span>
                <span v-if="(row as MaintenanceOperation).attempt > 1" class="wmp-attempt">#{{ (row as MaintenanceOperation).attempt }}</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="118">
            <template #default="{ row }">
              <SemanticTag
                :tone="row.stage === 'stop_unconfirmed' ? 'attention' : maintenanceSemanticTone(row.state)"
                :label="row.stage === 'stop_unconfirmed' ? '停止待确认' : maintenanceStates[(row as MaintenanceOperation).state]"
              />
            </template>
          </el-table-column>

          <el-table-column label="发起人" width="110" show-overflow-tooltip>
            <template #default="{ row }">
              <span class="wmp-muted">{{ (row as MaintenanceOperation).requested_by || '—' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="发起时间" width="136">
            <template #default="{ row }">
              <span class="wmp-mono-num">{{ formatTime((row as MaintenanceOperation).requested_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="耗时" width="94">
            <template #default="{ row }">
              <span class="wmp-mono-num">{{ formatOperationDuration(row as MaintenanceOperation) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="结果说明" min-width="180" show-overflow-tooltip>
            <template #default="{ row }">
              <span
                v-if="(row as MaintenanceOperation).error_message"
                class="wmp-msg-error"
              >
                {{ (row as MaintenanceOperation).error_message }}
              </span>
              <span v-else-if="(row as MaintenanceOperation).execution_reason" class="wmp-muted">
                {{ (row as MaintenanceOperation).execution_reason }}
              </span>
              <span v-else class="wmp-muted">—</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="76" align="right" fixed="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openOperation((row as MaintenanceOperation).id)">详情</el-button>
            </template>
          </el-table-column>

          <template #empty>
            <el-empty v-if="!loading" :image-size="64" description="暂无匹配运维操作" />
          </template>
        </el-table>

        <footer class="wmp-footer">
          <el-pagination
            v-model:current-page="page"
            v-model:page-size="pageSize"
            :page-sizes="[10, 20, 50]"
            :total="total"
            layout="total, sizes, prev, pager, next"
            aria-label="运维操作分页"
          />
        </footer>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { RouterLink, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  Activity,
  AlertTriangle,
  ArrowUpRight,
  GitBranch,
  RefreshCw,
  Rocket,
  RotateCw,
} from 'lucide-vue-next'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import type { Worker } from '@/api/types'
import {
  listMaintenanceOperations,
  submitMaintenance,
  type MaintenanceAction,
  type MaintenanceOperation,
  type WorkerEquipmentItem,
  type WorkerHealth,
} from '@/api/maintenance'
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

const confirmCopy: Record<Exclude<MaintenanceAction, 'check'>, string> = {
  deploy: '将同步远端运行环境与配置并重启 OpenCode 服务，确认继续？',
  update: '将拉取并切换目标仓库至配置分支，确认继续？',
  restart: '将重启远端 OpenCode 服务进程，确认继续？',
}

const blockingCodes = new Set(['maintenance_active', 'occupied_task', 'review_active'])

const blockingItems = computed(() =>
  (props.health?.blocking ?? []).filter((item) => blockingCodes.has(item.code)),
)

const blocking = computed(() =>
  blockingItems.value.map((item) => item.message).join('；'),
)

const activeMaintenance = computed(() => {
  const m = props.health?.maintenance
  if (!m || !maintenanceActive(m.state)) return null
  return m
})

function diagnostics(item: WorkerEquipmentItem) {
  const reasons = item.key === 'git' && item.state === 'ready' ? [] : [...item.diff]
  const itemError = item.error || (item.key === 'git' ? props.health?.repository?.error : null)
  if (itemError && !reasons.some((reason) => reason.includes(itemError))) reasons.push(itemError)
  return [...new Set(reasons)]
}

function parseIsoMs(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = new Date(/[zZ]|[+-]\d{2}:?\d{2}$/.test(value) ? value : `${value}Z`).getTime()
  return Number.isNaN(ms) ? null : ms
}

function formatOperationDuration(row: MaintenanceOperation): string {
  const start = parseIsoMs(row.started_at)
  if (start === null) return row.state === 'QUEUED' ? '排队中' : '—'
  const end = parseIsoMs(row.finished_at)
  if (end === null) return maintenanceActive(row.state) ? '进行中' : '—'
  const seconds = Math.max(0, Math.round((end - start) / 1000))
  if (seconds < 60) return `${seconds}s`
  const mins = Math.floor(seconds / 60)
  const rem = seconds % 60
  if (mins < 60) return rem ? `${mins}m ${rem}s` : `${mins}m`
  const hours = Math.floor(mins / 60)
  const remMins = mins % 60
  return remMins ? `${hours}h ${remMins}m` : `${hours}h`
}

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
    limit: String(pageSize.value),
    offset: String((page.value - 1) * pageSize.value),
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

function resetFilters() {
  actionFilter.value = ''
  stateFilter.value = ''
  applyFilters()
}

function openOperation(operationId: string) {
  return router.push({ name: 'settings-worker-maintenance', params: { id: props.worker.id, operationId } })
}

function onRowClick(row: MaintenanceOperation) {
  void openOperation(row.id)
}

function canSubmit(action: MaintenanceAction): boolean {
  const worker = props.worker
  if (submitting.value || !props.health || props.health.blocking.some((item) => item.code === 'maintenance_active')) {
    return false
  }
  const online = props.health.connection.state === 'online'
  if (action === 'check') return online
  if (worker.mode !== 'LINUX_REMOTE' || blocking.value || worker.occupied_tasks.length) return false
  if (action === 'deploy') return Boolean(worker.host && worker.username && worker.has_ssh_password)
  if (action === 'update') return online && Boolean(worker.repository_url)
  return online || worker.status === 'OFFLINE'
}

function disabledReason(action: MaintenanceAction): string | undefined {
  if (canSubmit(action)) return undefined
  if (submitting.value) return '正在提交操作'
  if (!props.health) return '等待环境诊断完成'
  if (props.health.blocking.some((item) => item.code === 'maintenance_active')) return '已有维护操作进行中'
  const online = props.health.connection.state === 'online'
  if (action === 'check') return online ? undefined : 'Worker 未在线'
  if (props.worker.mode !== 'LINUX_REMOTE') return '仅支持 Linux 远程 Worker'
  if (blocking.value || props.worker.occupied_tasks.length) return blocking.value || '存在未释放占用任务'
  if (action === 'deploy') return '请先完善 SSH 主机、用户名与密码'
  if (action === 'update') return !online ? 'Worker 未在线' : '未配置 Git 仓库地址'
  return '当前状态不可执行'
}

async function submit(action: MaintenanceAction) {
  submitting.value = true
  try {
    if (action !== 'check') {
      await ElMessageBox.confirm(confirmCopy[action], maintenanceActions[action], { type: 'warning' })
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
watch(
  () => props.health?.maintenance,
  (current, previous) => {
    if (current && (current.operation_id !== previous?.operation_id || current.state !== previous?.state)) {
      void load(true)
    }
  },
)

onMounted(() => {
  void load()
  timer = setInterval(() => {
    if (
      !document.hidden &&
      (++refreshTick % 3 === 0 || operations.value.some((operation) => maintenanceActive(operation.state)))
    ) {
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
.wmp {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.wmp-card {
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.wmp-card__head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px 16px;
  padding: 13px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.wmp-card__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  min-width: 0;
}

.wmp-card__title h3 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 14px;
  font-weight: 600;
  line-height: 20px;
}

.wmp-meta-time,
.wmp-count {
  color: var(--astra-text-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.wmp-card__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wmp-live-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px;
  border: 1px solid var(--astra-accent-border);
  border-radius: 999px;
  background: var(--astra-accent-soft);
  color: var(--astra-accent-text);
  font-size: 12px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.15s ease, border-color 0.15s ease;
}

.wmp-live-pill:hover {
  border-color: var(--astra-accent);
  color: var(--astra-accent-hover);
}

.wmp-card__body {
  padding: 14px 16px;
}

.wmp-card__body--stack {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

/* ── 运行环境矩阵 ────────────────────────────────────────────────── */
.wmp-env-list {
  display: flex;
  flex-direction: column;
}

.wmp-env-row {
  display: grid;
  grid-template-columns: 196px minmax(0, 1fr);
  align-items: start;
  gap: 12px 16px;
  padding: 11px 0;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.wmp-env-row:first-child {
  padding-top: 2px;
}

.wmp-env-row:last-child {
  padding-bottom: 2px;
  border-bottom: 0;
}

.wmp-env-row__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  min-height: 24px;
}

.wmp-env-row__label {
  color: var(--astra-text-primary);
  font-size: 13.5px;
  font-weight: 500;
}

.wmp-env-row__detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.wmp-env-facts {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  min-height: 24px;
}

.wmp-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 8px;
  border: 1px solid var(--astra-border);
  border-radius: 6px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  line-height: 20px;
}

.wmp-chip--mono {
  font-family: var(--astra-font-mono);
}

.wmp-chip--ok {
  border-color: color-mix(in srgb, var(--astra-success) 26%, transparent);
  background: var(--astra-success-soft);
  color: var(--astra-success);
}

.wmp-chip--warn {
  border-color: color-mix(in srgb, var(--astra-warning) 28%, transparent);
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.wmp-chip--err {
  border-color: color-mix(in srgb, var(--astra-danger) 28%, transparent);
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
}

.wmp-fact-text {
  color: var(--astra-text-secondary);
  font-size: 13px;
  line-height: 22px;
}

.wmp-repo-url {
  max-width: min(360px, 100%);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 12px;
}

.wmp-reasons {
  display: flex;
  flex-direction: column;
  gap: 3px;
  margin: 0;
  padding: 6px 10px;
  list-style: none;
  border-left: 2px solid var(--astra-warning);
  border-radius: 0 6px 6px 0;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  font-size: 12.5px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.wmp-reasons--error {
  border-left-color: var(--astra-danger);
  background: var(--astra-danger-soft);
  color: var(--astra-danger);
}

/* ── 运维操作按钮分组 ─────────────────────────────────────────────── */
.wmp-ops-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.wmp-ops-bar .el-button + .el-button {
  margin-left: 0;
}

.wmp-ops-divider {
  width: 1px;
  height: 18px;
  margin: 0 2px;
  background: var(--astra-border);
}

/* ── 阻塞条件条 ──────────────────────────────────────────────────── */
.wmp-blocking {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
  padding: 10px 16px;
  border-bottom: 1px solid color-mix(in srgb, var(--astra-warning) 22%, transparent);
  background: var(--astra-warning-soft);
}

.wmp-blocking__icon {
  flex: none;
  color: var(--astra-warning);
}

.wmp-blocking__title {
  color: var(--astra-warning);
  font-size: 12.5px;
  font-weight: 600;
}

.wmp-blocking__list {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.wmp-blocking__chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, var(--astra-warning) 30%, transparent);
  border-radius: 6px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  font-size: 12px;
  line-height: 18px;
  text-decoration: none;
}

.wmp-blocking__chip--link {
  cursor: pointer;
  color: var(--astra-accent-text);
  transition: border-color 0.15s ease, color 0.15s ease;
}

.wmp-blocking__chip--link:hover {
  border-color: var(--astra-accent);
  color: var(--astra-accent-hover);
}

/* ── 筛选与表格 ──────────────────────────────────────────────────── */
.wmp-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.wmp-toolbar__filters {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.wmp-toolbar__right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.wmp-select {
  width: 148px;
}

.wmp-table :deep(.el-table__row) {
  cursor: pointer;
}

.wmp-op-cell {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.wmp-op-name {
  color: var(--astra-text-primary);
  font-weight: 500;
}

.wmp-attempt {
  padding: 0 5px;
  border: 1px solid var(--astra-border);
  border-radius: 4px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  line-height: 16px;
}

.wmp-mono-num {
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.wmp-muted {
  color: var(--astra-text-muted);
  font-size: 13px;
}

.wmp-msg-error {
  color: var(--astra-danger);
  font-size: 13px;
}

.wmp-footer {
  display: flex;
  justify-content: flex-end;
  padding-top: 4px;
}

@media (max-width: 900px) {
  .wmp-env-row {
    grid-template-columns: 1fr;
    gap: 6px;
  }
  .wmp-ops-divider {
    display: none;
  }
}
</style>
