<template>
  <section class="admin-page worker-page" data-testid="employee-list-page">
    <SettingsHeaderActions>
      <ElButton :icon="RefreshCw" :loading="store.loading || healthLoading" data-testid="employee-refresh" @click="refreshAll">刷新</ElButton>
      <ElButton type="primary" :icon="Plus" data-testid="employee-create" @click="beginCreate">新建 Agent Worker</ElButton>
    </SettingsHeaderActions>

    <div class="admin-page__toolbar" role="search" aria-label="Agent Worker 筛选" data-testid="employee-filter">
      <div class="admin-page__filters">
        <ElSelect v-model="digitalEmployee" class="admin-page__select" data-testid="employee-filter-digital" aria-label="数字员工" placeholder="数字员工：全部" clearable filterable>
          <ElOption v-for="item in digitalEmployees" :key="item.id" :value="item.id" :label="`${item.name} · ${item.domain}`" />
          <ElOption v-if="unlinkedCount" value="__none__" label="未关联数字员工" />
        </ElSelect>
        <ElSelect v-model="connection" class="admin-page__select" data-testid="employee-filter-status" aria-label="连接状态" placeholder="连接：全部" clearable>
          <ElOption v-for="option in connectionOptions" :key="option.value" :value="option.value" :label="option.label" />
        </ElSelect>
        <ElButton
          :icon="SlidersHorizontal"
          data-testid="employee-filter-more"
          :aria-expanded="moreFiltersOpen"
          @click="moreFiltersOpen = !moreFiltersOpen"
        >更多筛选{{ activeMoreFilterCount ? `（${activeMoreFilterCount}）` : '' }}</ElButton>
      </div>
      <div class="admin-page__search">
        <ElInput v-model="query" class="admin-page__keyword" data-testid="employee-filter-keyword" aria-label="搜索 Agent Worker" placeholder="搜索 Worker / 数字员工 / 服务器 / 任务" clearable @keydown.enter.prevent="applySearch">
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="employee-search" @click="applySearch">搜索</ElButton>
      </div>
    </div>

    <div v-if="moreFiltersOpen" class="admin-page__filters worker-more-filters" data-testid="employee-filter-more-panel">
      <ElSelect v-model="maintenance" class="admin-page__select" data-testid="employee-filter-maintenance" aria-label="维护状态" placeholder="维护：全部" clearable>
        <ElOption v-for="option in maintenanceOptions" :key="option.value" :value="option.value" :label="option.label" />
      </ElSelect>
      <ElSelect v-model="enabled" class="admin-page__select" data-testid="employee-filter-enabled" aria-label="Worker 调度状态" placeholder="调度：全部" clearable>
        <ElOption value="enabled" label="允许调度" />
        <ElOption value="disabled" label="已停止派单" />
        <ElOption value="unknown" label="状态未获取" />
      </ElSelect>
    </div>

    <ElAlert v-if="store.error || healthError" type="error" show-icon :closable="false" :title="store.error || healthError">
      <ElButton size="small" @click="refreshAll">重新加载</ElButton>
    </ElAlert>

    <div class="astra-table-panel">
      <ElTable v-loading="store.loading || healthLoading || digitalLoading" :data="paged" row-key="id" aria-label="Agent Worker 列表" @row-click="openDetail">
        <ElTableColumn label="Agent Worker" min-width="230" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="worker-cell">
              <EmployeeAvatar :employee="row.employee" :emoji="row.digital?.avatar" size="small" />
              <span class="worker-cell__copy">
                <strong>{{ row.name }}</strong>
                <ElText v-if="row.digital">{{ row.digital.name }} · {{ row.digital.domain }}</ElText>
                <ElText v-else-if="row.employee.digital_employee_id">数字员工目录未加载</ElText>
                <ElText v-else>未关联数字员工</ElText>
                <ElText class="worker-cell__meta">{{ row.employee.id }} · 环境 #{{ row.environment.id }}</ElText>
              </span>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="部署位置" min-width="150" show-overflow-tooltip>
          <template #default="{ row }">
            <div class="environment-cell">
              <strong>{{ row.server }}</strong>
              <ElText>{{ row.environment.mode === 'LINUX_REMOTE' ? 'Linux 远程' : '本地' }}</ElText>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="可接任务" width="130">
          <template #default="{ row }">
            <div class="worker-health-cell" @click.stop>
              <SemanticTag :tone="schedulableTone(row.health)" :label="schedulableLabel(row.health)" />
              <ElPopover v-if="blockingReasons(row.health).length" trigger="click" :width="320" placement="bottom-start">
                <template #reference><ElButton link type="primary" size="small">查看原因</ElButton></template>
                <ul class="worker-reasons"><li v-for="reason in blockingReasons(row.health)" :key="reason">{{ reason }}</li></ul>
              </ElPopover>
            </div>
          </template>
        </ElTableColumn>

        <ElTableColumn label="当前任务" min-width="190" show-overflow-tooltip>
          <template #default="{ row }">
            <div v-if="row.currentTask" class="environment-cell">
              <RouterLink class="task-link" :to="`/tasks/${row.currentTask.id}`" :title="row.currentTask.title" @click.stop>
                <strong>{{ row.currentTask.title }}</strong>
                <ElText truncated>{{ row.currentTask.runtime_status_label }}</ElText>
              </RouterLink>
              <SemanticTag v-if="row.waitingTask" tone="attention" label="等待确认" />
            </div>
            <RouterLink v-else-if="row.currentTaskId" :to="`/tasks/${row.currentTaskId}`" @click.stop>{{ row.currentTaskId }}</RouterLink>
            <ElText v-else>{{ row.health ? '无占用任务' : '尚未获取' }}</ElText>
          </template>
        </ElTableColumn>

        <ElTableColumn label="操作" width="120" align="right">
          <template #default="{ row }">
            <div class="admin-actions">
              <ElButton link type="primary" @click.stop="openDetail(row as WorkerRowView)">查看</ElButton>
              <ElDropdown trigger="click" @command="(command: string) => runMore(row as WorkerRowView, command)">
                <ElButton link type="primary" @click.stop>更多</ElButton>
                <template #dropdown>
                  <ElDropdownMenu>
                    <ElDropdownItem command="edit" :disabled="isBusy(row as WorkerRowView)">编辑</ElDropdownItem>
                    <ElDropdownItem command="maintenance">部署与维护</ElDropdownItem>
                  </ElDropdownMenu>
                </template>
              </ElDropdown>
            </div>
          </template>
        </ElTableColumn>

        <template #empty>
          <ElEmpty v-if="store.error && !store.employees.length" :image-size="72" description="Agent Worker 数据加载失败">
            <ElText>{{ store.error }}</ElText>
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
import { Plus, RefreshCw, Search, SlidersHorizontal } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElButton, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElText, ElPopover, vLoading } from 'element-plus'
import { getWorkerHealth, submitMaintenance, type WorkerHealth } from '@/api/maintenance'
import type { Employee } from '@/api/types'
import { listEmployees } from '@/api'
import type { Employee as DigitalEmployee } from '@/types'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import { notify } from '@/composables/useToast'
import {
  blockingReasons,
  isMaintenanceActive,
  schedulableLabel,
  schedulableTone,
  toWorkerRow,
  workerMatches,
  type WorkerRow,
} from '@/features/workers/presentation'
import { useAstraStore } from '@/stores/astra'
import SettingsHeaderActions from '@/views/settings/SettingsHeaderActions.vue'

type WorkerRowView = WorkerRow & { digital: DigitalEmployee | null }

const store = useAstraStore()
const route = useRoute()
const router = useRouter()
const query = ref('')
const connection = ref(typeof route.query.status === 'string' ? route.query.status : '')
const maintenance = ref('')
const enabled = ref('')
const digitalEmployee = ref('')
const moreFiltersOpen = ref(false)
const page = ref(1)
const pageSize = ref(20)
const modalOpen = ref(false)
const modalCreating = ref(false)
const modalEmployee = ref<Employee | null>(null)
const healthError = ref('')
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
const activeMoreFilterCount = computed(() => (maintenance.value ? 1 : 0) + (enabled.value ? 1 : 0))
const filtered = computed(() => rows.value.filter((row) => {
  const needle = query.value.trim().toLocaleLowerCase('zh-CN')
  const matchesQuery = workerMatches(row, query.value)
    || `${row.digital?.name ?? ''} ${row.digital?.domain ?? ''} ${row.employee.digital_employee_id ?? ''}`.toLocaleLowerCase('zh-CN').includes(needle)
  if (!matchesQuery) return false
  if (digitalEmployee.value === '__none__' && row.employee.digital_employee_id) return false
  if (digitalEmployee.value && digitalEmployee.value !== '__none__' && row.employee.digital_employee_id !== digitalEmployee.value) return false
  return (!connection.value || row.connection === connection.value)
    && (!maintenance.value || maintenanceFilter(row) === maintenance.value)
    && (!enabled.value || enabledState(row) === enabled.value)
}))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize.value)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const environmentKey = computed(() => rows.value.map((row) => row.environment.id).join(','))

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
  healthError.value = ''
  try {
    const results = await Promise.allSettled(ids.map((id) => getWorkerHealth(id, controller.signal)))
    if (generation !== healthGeneration || controller.signal.aborted) return
    if (rows.value.map((row) => row.environment.id).join(',') !== key) return
    const next: Record<number, WorkerHealth> = {}
    results.forEach((result, index) => {
      if (result.status === 'fulfilled') next[ids[index]] = result.value
    })
    healthByEnvironment.value = next
    const failed = results.filter(result => result.status === 'rejected').length
    healthError.value = failed ? `${failed} 个 Worker 的健康状态读取失败，请重新加载。` : ''
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

function runMore(row: WorkerRowView, command: string) {
  if (command === 'edit') beginEdit(row.employee)
  else if (command === 'maintenance') void router.push(`/settings/workers/${row.employee.id}#maintenance`)
}

function isBusy(row: WorkerRowView) {
  return isMaintenanceActive(row.maintenanceState)
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
watch([query, connection, maintenance, enabled, digitalEmployee], () => { page.value = 1 })
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
.worker-more-filters { padding: 8px 10px; border: 1px solid var(--astra-border); border-radius: 10px; background: var(--astra-bg-panel); }
.worker-cell { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.worker-cell__copy, .environment-cell, .worker-health-cell { display: grid; gap: 2px; min-width: 0; }
.worker-cell__copy strong, .task-link strong, .environment-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.worker-cell__meta { font-size: var(--astra-font-helper); }
.worker-health-cell { justify-items: start; gap: 4px; }
.worker-reasons { margin: 0; padding-left: 18px; line-height: 1.7; overflow-wrap: anywhere; }
.task-link, .task-link:hover { color: inherit; text-decoration: none; }
.task-link:hover strong { color: var(--astra-primary); }
.astra-table-panel :deep(.el-table__row) { cursor: pointer; }
</style>
