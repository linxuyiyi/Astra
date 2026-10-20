<template>
  <section class="admin-page worker-page" data-testid="employee-list-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="store.loading || healthLoading" data-testid="employee-refresh" @click="refreshAll">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="employee-create" @click="beginCreate">新建 Agent Worker</ElButton>
    </SettingsHeaderActions>

    <div class="worker-stats" role="group" aria-label="Agent Worker 概览">
      <button
        v-for="item in statCards"
        :key="item.key"
        type="button"
        class="worker-stat"
        :class="{ 'is-active': quickFilter === item.key }"
        :aria-pressed="quickFilter === item.key"
        @click="toggleQuickFilter(item.key)"
      >
        <span class="worker-stat__label">{{ item.label }}</span>
        <strong>{{ item.value }}</strong>
        <small>{{ item.hint }}</small>
      </button>
    </div>

    <div class="admin-page__toolbar" role="search" aria-label="Agent Worker 筛选" data-testid="employee-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="digitalEmployee" class="admin-page__select" data-testid="employee-filter-digital" aria-label="数字员工" placeholder="数字员工：全部" clearable filterable>
          <ElOption v-for="item in digitalEmployees" :key="item.id" :value="item.id" :label="`${item.name} · ${item.domain}`" />
          <ElOption v-if="unlinkedCount" value="__none__" label="未关联数字员工" />
        </ElSelect>
        <ElSelect v-model="connection" class="admin-page__select" data-testid="employee-filter-status" aria-label="连接状态" placeholder="连接：全部" clearable>
          <ElOption v-for="option in connectionOptions" :key="option.value" :value="option.value" :label="option.label" />
        </ElSelect>
        <ElSelect v-model="maintenance" class="admin-page__select" data-testid="employee-filter-maintenance" aria-label="维护状态" placeholder="维护：全部" clearable>
          <ElOption v-for="option in maintenanceOptions" :key="option.value" :value="option.value" :label="option.label" />
        </ElSelect>
        <ElSelect v-model="enabled" class="admin-page__select" data-testid="employee-filter-enabled" aria-label="Worker 启用状态" placeholder="调度：全部" clearable>
          <ElOption value="enabled" label="允许调度" />
          <ElOption value="disabled" label="已停止派单" />
          <ElOption value="unknown" label="状态未获取" />
        </ElSelect>
      </div>
      <div class="admin-page__search">
        <ElInput v-model="query" class="admin-page__keyword" data-testid="employee-filter-keyword" aria-label="搜索 Agent Worker" placeholder="搜索 Worker / 数字员工 / 服务器 / 仓库 / 任务" clearable @keydown.enter.prevent="applySearch">
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="employee-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <div class="astra-table-panel">
      <ElTable v-loading="store.loading || healthLoading || digitalLoading" :data="paged" row-key="id" aria-label="Agent Worker 列表" @row-click="openDetail">
        <ElTableColumn label="Agent Worker" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="worker-cell">
              <EmployeeAvatar :employee="row.employee" :emoji="row.digital?.avatar" size="small" />
              <span class="worker-cell__copy">
                <strong>{{ row.name }}</strong>
                <ElText type="info" class="worker-cell__meta">{{ row.employee.id }} · 环境 #{{ row.environment.id }}</ElText>
                <span class="worker-cell__tags">
                  <ElTag v-if="row.enabled === false" size="small" type="info" effect="plain">进程已禁用</ElTag>
                  <ElTag v-if="!row.employee.enabled" size="small" type="info" effect="plain">已停止派单</ElTag>
                  <ElTag v-if="row.environment.mode === 'LINUX_REMOTE'" size="small" effect="plain">Linux</ElTag>
                  <ElTag v-else size="small" effect="plain">本地</ElTag>
                </span>
              </span>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="数字员工" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.digital" class="digital-cell">
              <span class="digital-cell__avatar">{{ row.digital.avatar || '🤖' }}</span>
              <span class="worker-cell__copy">
                <strong>{{ row.digital.name }}</strong>
                <ElText type="info">{{ row.digital.domain }}</ElText>
              </span>
            </div>
            <ElText v-else-if="row.employee.digital_employee_id" type="info">目录未加载 · {{ row.employee.digital_employee_id }}</ElText>
            <ElText v-else type="info">未关联数字员工</ElText>
          </template>
        </ElTableColumn>

        <ElTableColumn label="运行环境" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="environment-cell">
              <strong>{{ row.server }}</strong>
              <ElText type="info" truncated>{{ row.environment.workspace_root || '未设置工作空间' }}</ElText>
              <ElText v-if="row.repository.configured" type="info" truncated>{{ repositorySummary(row.repository) }}</ElText>
              <ElText v-if="row.repository.error" type="danger" truncated>{{ row.repository.error }}</ElText>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="状态" min-width="190">
          <template #default="{ row }">
            <div class="worker-health-cell">
              <ElTag size="small" :type="connectionTone(row.connection)">连接 {{ connectionLabel(row.connection) }}</ElTag>
              <ElTag size="small" :type="runtimeHealthTone(row.health)">OpenCode {{ runtimeHealthLabel(row.health) }}</ElTag>
              <ElTag v-if="row.maintenanceState" size="small" :type="maintenanceStateTone(row.maintenanceState)">{{ maintenanceStateLabel(row.maintenanceState) }}</ElTag>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="当前任务" min-width="220" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.currentTask" class="environment-cell">
              <RouterLink class="task-link" :to="`/tasks/${row.currentTask.id}`" :title="row.currentTask.title" @click.stop>
                <strong>{{ row.currentTask.title }}</strong>
                <ElText type="info" truncated>{{ row.currentTask.runtime_status_label }}</ElText>
              </RouterLink>
              <ElTag v-if="row.waitingTask" size="small" type="warning">等待确认</ElTag>
            </div>
            <ElText v-else type="info">空闲</ElText>
          </template>
        </ElTableColumn>

        <ElTableColumn label="最近维护" min-width="170" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.maintenanceState" class="environment-cell">
              <ElTag size="small" :type="maintenanceStateTone(row.maintenanceState)">{{ maintenanceStateLabel(row.maintenanceState) }}</ElTag>
              <ElText type="info" truncated>{{ row.health?.maintenance?.stage ? maintenanceStageLabel(row.health.maintenance.stage) : '—' }}</ElText>
            </div>
            <ElText v-else type="info">无记录</ElText>
          </template>
        </ElTableColumn>

        <ElTableColumn label="操作" width="190" align="right">
          <template #default="{ row }">
            <div class="admin-actions">
              <ElButton link type="primary" @click.stop="openDetail(row as WorkerRowView)">查看</ElButton>
              <ElButton link type="primary" :disabled="isBusy(row as WorkerRowView)" @click.stop="beginEdit((row as WorkerRowView).employee)">编辑</ElButton>
              <ElDropdown trigger="click" :disabled="isBusy(row as WorkerRowView)" @command="(command: string) => runMaintenance(row as WorkerRowView, command as MaintenanceAction)">
                <ElButton link type="primary" :loading="busyId === String(row.employee.id)" @click.stop>维护</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="check">检查</ElDropdownItem>
                    <ElDropdownItem command="deploy">一键部署</ElDropdownItem>
                    <ElDropdownItem command="restart">重启服务</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </template>
        </ElTableColumn>

        <template #empty>
          <ElEmpty v-if="store.error && !store.employees.length" :image-size="72" description="Agent Worker 数据加载失败">
            <ElText type="info">{{ store.error }}</ElText>
            <ElButton :icon="RefreshCw" @click="refreshAll">重新加载</ElButton>
          </ElEmpty>
          <ElEmpty v-else-if="!store.loading && !healthLoading" :image-size="72" :description="store.employees.length ? '没有匹配的 Agent Worker' : '暂无 Agent Worker'">
            <ElButton v-if="!store.employees.length" type="primary" :icon="Plus" @click="beginCreate">新建 Agent Worker</ElButton>
          </ElEmpty>
        </template>
      </ElTable>
    </div>

    <div class="admin-page__footer" data-testid="employee-total">
      <ElPagination
        v-model:current-page="page"
        v-model:page-size="pageSize"
        :page-sizes="[10, 20, 50]"
        :total="filtered.length"
        layout="total, sizes, prev, pager, next"
        aria-label="Agent Worker 列表分页"
        data-testid="employee-pagination"
      />
    </div>

    <EmployeeConfigModal
      :open="modalOpen"
      :employee="modalEmployee"
      :creating="modalCreating"
      :categories="store.categories"
      :digital-employees="digitalEmployees"
      :digital-loading="digitalLoading"
      @close="closeModal"
      @saved="onModalSaved"
    />
  </section>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import { getWorkerHealth, submitMaintenance, type MaintenanceAction, type WorkerHealth } from '@/api/maintenance'
import type { Employee } from '@/api/types'
import { listEmployees } from '@/api'
import type { Employee as DigitalEmployee } from '@/types'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import {
  connectionLabel,
  connectionTone,
  isMaintenanceActive,
  maintenanceStageLabel,
  maintenanceStateLabel,
  maintenanceStateTone,
  repositorySummary,
  runtimeHealthLabel,
  runtimeHealthTone,
  toWorkerRow,
  workerMatches,
  type WorkerRow,
} from '@/features/workers/presentation'
import { useAstraStore } from '@/stores/astra'
import SettingsHeaderActions from '@/views/settings/SettingsHeaderActions.vue'

type WorkerRowView = WorkerRow & { digital: DigitalEmployee | null }
type QuickFilter = 'all' | 'online' | 'attention' | 'maintenance'

const store = useAstraStore()
const route = useRoute()
const router = useRouter()
const query = ref('')
const connection = ref(typeof route.query.status === 'string' ? route.query.status : '')
const maintenance = ref('')
const enabled = ref('')
const digitalEmployee = ref('')
const quickFilter = ref<QuickFilter>('all')
const page = ref(1)
const pageSize = ref(20)
const modalOpen = ref(false)
const modalCreating = ref(false)
const modalEmployee = ref<Employee | null>(null)
const busyId = ref('')
const healthByEnvironment = ref<Record<number, WorkerHealth>>({})
const healthLoading = ref(false)
const digitalEmployees = ref<DigitalEmployee[]>([])
const digitalLoading = ref(false)
let healthGeneration = 0
let healthController: AbortController | null = null

const connectionOptions = [
  { value: 'online', label: '连接在线' },
  { value: 'offline', label: '连接离线' },
  { value: 'local', label: '本机资源' },
  { value: 'unknown', label: '连接未确认' },
]
const maintenanceOptions = [
  { value: 'active', label: '维护进行中' },
  { value: 'succeeded', label: '维护成功' },
  { value: 'failed', label: '维护失败或取消' },
  { value: 'interrupted', label: '维护待核对' },
  { value: 'none', label: '无维护记录' },
]

const digitalById = computed(() => new Map(digitalEmployees.value.map((item) => [item.id, item])))
const environmentEmployees = computed(() => store.employees.filter((employee) => employee.execution_environment))
const rows = computed<WorkerRowView[]>(() => environmentEmployees.value.map((employee) => {
  const environment = employee.execution_environment
  if (!environment) throw new Error('unreachable')
  const row = toWorkerRow(employee, environment, store.tasks, healthByEnvironment.value[environment.id] ?? null)
  return { ...row, digital: digitalById.value.get(employee.digital_employee_id ?? '') ?? null }
}))
const unlinkedCount = computed(() => rows.value.filter((row) => !row.employee.digital_employee_id).length)
const filtered = computed(() => rows.value.filter((row) => {
  const needle = query.value.trim().toLocaleLowerCase('zh-CN')
  const matchesQuery = workerMatches(row, query.value)
    || `${row.digital?.name ?? ''} ${row.digital?.domain ?? ''} ${row.employee.digital_employee_id ?? ''}`.toLocaleLowerCase('zh-CN').includes(needle)
  if (!matchesQuery) return false
  if (quickFilter.value === 'online' && row.connection !== 'online') return false
  if (quickFilter.value === 'maintenance' && !isMaintenanceActive(row.maintenanceState)) return false
  if (quickFilter.value === 'attention' && !needsAttention(row)) return false
  if (digitalEmployee.value === '__none__' && row.employee.digital_employee_id) return false
  if (digitalEmployee.value && digitalEmployee.value !== '__none__' && row.employee.digital_employee_id !== digitalEmployee.value) return false
  return (!connection.value || row.connection === connection.value)
    && (!maintenance.value || maintenanceFilter(row) === maintenance.value)
    && (!enabled.value || enabledState(row) === enabled.value)
}))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const environmentKey = computed(() => rows.value.map((row) => row.environment.id).join(','))

const statCards = computed(() => [
  { key: 'all' as const, label: '全部 Worker', value: rows.value.length, hint: '含本地与远程' },
  { key: 'online' as const, label: '连接在线', value: rows.value.filter((row) => row.connection === 'online').length, hint: 'Worker 通道可用' },
  { key: 'maintenance' as const, label: '维护中', value: rows.value.filter((row) => isMaintenanceActive(row.maintenanceState)).length, hint: '部署 / 更新 / 重启' },
  { key: 'attention' as const, label: '需要关注', value: rows.value.filter(needsAttention).length, hint: '离线、异常或维护失败' },
])

function needsAttention(row: WorkerRowView): boolean {
  if (row.connection === 'offline' || row.connection === 'unknown') return true
  if (row.maintenanceState === 'FAILED' || row.maintenanceState === 'INTERRUPTED') return true
  return row.health ? !row.health.schedulable : false
}

function enabledState(row: WorkerRow): string {
  if (row.enabled === true) return 'enabled'
  if (row.enabled === false) return 'disabled'
  return 'unknown'
}

function maintenanceFilter(row: WorkerRow): string {
  if (!row.maintenanceState) return 'none'
  if (isMaintenanceActive(row.maintenanceState)) return 'active'
  if (row.maintenanceState === 'INTERRUPTED') return 'interrupted'
  if (row.maintenanceState === 'SUCCEEDED') return 'succeeded'
  return 'failed'
}

function toggleQuickFilter(filter: QuickFilter) {
  quickFilter.value = quickFilter.value === filter ? 'all' : filter
}

async function loadHealth() {
  const ids = rows.value.map((row) => row.environment.id)
  const key = ids.join(',')
  const generation = ++healthGeneration
  healthController?.abort()
  if (!ids.length) {
    healthController = null
    healthByEnvironment.value = {}
    healthLoading.value = false
    return
  }
  const controller = new AbortController()
  healthController = controller
  healthLoading.value = true
  try {
    const results = await Promise.allSettled(ids.map((id) => getWorkerHealth(id, controller.signal)))
    if (generation !== healthGeneration || controller.signal.aborted) return
    if (rows.value.map((row) => row.environment.id).join(',') !== key) return
    const next: Record<number, WorkerHealth> = {}
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') next[ids[index]] = result.value
    })
    healthByEnvironment.value = next
  } finally {
    if (generation === healthGeneration) healthLoading.value = false
  }
}

async function loadDigitalEmployees() {
  digitalLoading.value = true
  try {
    digitalEmployees.value = await listEmployees()
  } catch {
    digitalEmployees.value = []
  } finally {
    digitalLoading.value = false
  }
}

async function refreshAll() {
  await Promise.all([store.refresh(), loadDigitalEmployees()])
  await loadHealth()
}

function openDetail(row: WorkerRowView) {
  void router.push(`/settings/workers/${row.employee.id}`)
}

async function runMaintenance(row: WorkerRowView, action: MaintenanceAction) {
  if (action === 'deploy' || action === 'restart') {
    const confirmed = await confirmDialog({
      title: action === 'deploy' ? '一键部署这个 Worker？' : '重启这个 Worker 的远程服务？',
      message: action === 'deploy'
        ? `将部署 ${row.name} 的执行环境；有任务占用或维护进行中时会被拒绝。`
        : `将重启 ${row.name} 的 OpenCode 服务；正在执行或等待确认的任务会被保护。`,
      confirmLabel: action === 'deploy' ? '开始部署' : '确认重启',
      danger: action === 'restart',
    })
    if (!confirmed) return
  }
  busyId.value = String(row.employee.id)
  try {
    const created = await submitMaintenance(row.environment.id, action)
    notify(`${maintenanceActionCopy(action)}已提交：${created.id}`)
    await loadHealth()
  } catch (error) {
    notify(error instanceof Error ? error.message : `${maintenanceActionCopy(action)}提交失败`, 'error')
  } finally {
    busyId.value = ''
  }
}

function maintenanceActionCopy(action: MaintenanceAction): string {
  if (action === 'check') return '检查'
  if (action === 'deploy') return '一键部署'
  if (action === 'restart') return '重启'
  return '维护操作'
}

function isBusy(row: WorkerRowView) {
  return busyId.value === String(row.employee.id) || isMaintenanceActive(row.maintenanceState)
}

function applySearch() {
  page.value = 1
  if (!store.loading) void refreshAll()
}

function beginCreate() {
  modalEmployee.value = null
  modalCreating.value = true
  modalOpen.value = true
}
function beginEdit(employee: Employee | null) {
  if (!employee) return
  modalEmployee.value = employee
  modalCreating.value = false
  modalOpen.value = true
}
function closeModal() {
  modalOpen.value = false
  modalEmployee.value = null
}
async function onModalSaved(id: string, deployRequested: boolean) {
  await store.refresh()
  const saved = store.employees.find((item) => item.id === id) ?? null
  if (deployRequested) {
    const environmentId = saved?.execution_environment?.id
    if (!environmentId) {
      notify('执行环境尚未创建，无法提交部署', 'error')
    } else {
      try {
        const created = await submitMaintenance(environmentId, 'deploy')
        notify(`一键部署已提交：${created.id}`)
      } catch (error) {
        notify(error instanceof Error ? error.message : '一键部署提交失败', 'error')
      }
    }
  }
  await loadHealth()
  if (modalOpen.value) {
    modalCreating.value = false
    modalEmployee.value = saved
  }
}

watch(pageSize, () => { page.value = 1 })
watch(totalPages, (value) => { page.value = Math.min(page.value, value) })
watch([query, connection, maintenance, enabled, digitalEmployee, quickFilter], () => { page.value = 1 })
watch(environmentKey, () => { void loadHealth() }, { immediate: true })
watch(() => store.employees, (employees) => {
  if (modalEmployee.value) modalEmployee.value = employees.find((item) => item.id === modalEmployee.value?.id) ?? null
})
onBeforeUnmount(() => {
  healthGeneration += 1
  healthController?.abort()
  healthController = null
})
onMounted(async () => {
  const editId = typeof route.query.edit === 'string' ? route.query.edit : ''
  const create = route.query.create === '1'
  await loadDigitalEmployees()
  if (!store.employees.length && (editId || create || !store.loading)) await store.refresh()
  if (editId) beginEdit(store.employees.find((item) => item.id === editId) ?? null)
  else if (create) beginCreate()
})
</script>

<style scoped>
.worker-page { gap: 12px; }
.worker-stats { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; }
.worker-stat {
  display: grid;
  gap: 2px;
  padding: 10px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 11px;
  background: var(--astra-bg-panel);
  color: inherit;
  text-align: left;
  cursor: pointer;
  transition: border-color .15s ease, background .15s ease;
}
.worker-stat:hover { border-color: var(--astra-primary-border); }
.worker-stat.is-active { border-color: var(--astra-primary); background: var(--astra-primary-soft); }
.worker-stat__label { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-stat strong { color: var(--astra-text-primary); font-size: 22px; line-height: 1.2; font-variant-numeric: tabular-nums; }
.worker-stat small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-cell { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.worker-cell__copy, .environment-cell, .worker-health-cell, .digital-cell { display: grid; gap: 2px; min-width: 0; }
.worker-cell__copy strong, .digital-cell strong, .task-link strong, .environment-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.worker-cell__meta { font-size: var(--astra-font-helper); }
.worker-cell__tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 2px; }
.worker-health-cell { justify-items: start; gap: 4px; }
.digital-cell { grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 8px; }
.digital-cell__avatar { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; border-radius: 9px; background: var(--astra-bg-subtle); font-size: 17px; }
.task-link, .task-link:hover { color: inherit; text-decoration: none; }
.task-link:hover strong { color: var(--astra-primary); }
.astra-table-panel :deep(.el-table__row) { cursor: pointer; }
@media (max-width: 1100px) { .worker-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
</style>
