<template>
  <div class="astra-page" data-testid="employee-list-page">
    <AstraPageHeader title="Agent Worker" hide-title testid="employee-header">
      <template #actions>
        <ElButton :icon="RefreshCw" :loading="store.loading || healthLoading" data-testid="employee-refresh" @click="refreshAll">刷新</ElButton>
        <ElButton type="primary" :icon="Plus" data-testid="employee-create" @click="beginCreate">新建 Worker</ElButton>
      </template>
    </AstraPageHeader>

    <AstraFilterPanel label="Worker 筛选" testid="employee-filter">
      <ElSelect v-model="connection" class="astra-filter__select" data-testid="employee-filter-status" aria-label="连接状态" placeholder="连接：全部" clearable>
        <ElOption v-for="option in connectionOptions" :key="option.value" :value="option.value" :label="option.label" />
      </ElSelect>
      <ElSelect v-model="maintenance" class="astra-filter__select" data-testid="employee-filter-maintenance" aria-label="维护状态" placeholder="维护：全部" clearable>
        <ElOption v-for="option in maintenanceOptions" :key="option.value" :value="option.value" :label="option.label" />
      </ElSelect>
      <ElSelect v-model="enabled" class="astra-filter__select" data-testid="employee-filter-enabled" aria-label="Worker 启用状态" placeholder="Worker：全部" clearable>
        <ElOption value="enabled" label="已启用" />
        <ElOption value="disabled" label="已禁用" />
        <ElOption value="unknown" label="启用状态未获取" />
      </ElSelect>
      <template #search>
        <ElInput v-model="query" class="astra-filter__keyword" data-testid="employee-filter-keyword" aria-label="搜索 Worker" placeholder="搜索 Worker 名称 / 服务器 / 仓库 / 任务" clearable>
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="employee-search" @click="applySearch">搜索</ElButton>
      </template>
    </AstraFilterPanel>

    <section class="astra-table-panel astra-table-panel--clickable" aria-label="Worker 列表">
      <ElTable v-loading="store.loading || healthLoading" :data="paged" height="100%" row-key="id" @row-click="openDetail">
        <ElTableColumn label="Worker" min-width="240">
          <template #default="{ row }">
            <div class="employee-cell">
              <EmployeeAvatar :employee="row.employee" size="small" />
              <span class="employee-cell__meta">
                <strong>{{ row.name }}</strong>
                <ElText type="info" class="employee-cell__id">环境资源 #{{ row.environment.id }} · 关联员工 {{ row.employeeName }}</ElText>
                <ElTag v-if="row.enabled === false" size="small" type="info" effect="plain">Worker 已禁用</ElTag>
                <ElTag v-if="!row.employee.enabled" size="small" type="info" effect="plain">平台调度已停用</ElTag>
              </span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="服务器" min-width="180">
          <template #default="{ row }">
            <div class="environment-cell">
              <strong>{{ row.server }}</strong>
              <ElText type="info">{{ row.environment.mode === 'LINUX_REMOTE' ? `OpenCode ${row.environment.opencode_server_port}` : '本地执行资源' }}</ElText>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="目标仓库" min-width="210">
          <template #default="{ row }">
            <div class="environment-cell">
              <ElText :type="row.repository.configured ? undefined : 'info'" truncated>{{ repositorySummary(row.repository) }}</ElText>
              <ElText v-if="row.repository.targetBranch" type="info">目标分支 {{ row.repository.targetBranch }}</ElText>
              <ElText v-if="row.repository.error" type="danger" truncated>{{ row.repository.error }}</ElText>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="连接 / 健康" min-width="150">
          <template #default="{ row }">
            <div class="worker-health-cell">
              <ElTag size="small" :type="connectionTone(row.connection)">连接 {{ connectionLabel(row.connection) }}</ElTag>
              <ElTag size="small" :type="runtimeHealthTone(row.health)">OpenCode {{ runtimeHealthLabel(row.health) }}</ElTag>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="当前任务 / 等待确认" min-width="220">
          <template #default="{ row }">
            <div v-if="row.currentTask" class="environment-cell">
              <RouterLink class="task-link" :to="`/tasks/${row.currentTask.id}`" :title="row.currentTask.title" @click.stop>
                <strong>{{ row.currentTask.title }}</strong>
                <ElText type="info" truncated>{{ row.currentTask.runtime_status_label }}</ElText>
              </RouterLink>
              <ElTag v-if="row.waitingTask" size="small" type="warning">等待确认</ElTag>
            </div>
            <ElText v-else type="info">—</ElText>
          </template>
        </ElTableColumn>
        <ElTableColumn label="最近维护" min-width="190">
          <template #default="{ row }">
            <div v-if="row.maintenanceState" class="environment-cell">
              <ElSpace :size="6">
                <ElTag size="small" :type="maintenanceStateTone(row.maintenanceState)">{{ maintenanceStateLabel(row.maintenanceState) }}</ElTag>
                <ElButton v-if="isMaintenanceActive(row.maintenanceState)" link type="primary" @click.stop="openDetail(row as WorkerRow)">查看进度</ElButton>
              </ElSpace>
              <ElText type="info" truncated>{{ row.health?.maintenance?.stage ? maintenanceStageLabel(row.health.maintenance.stage) : '—' }} · {{ row.health?.maintenance?.operation_id }}</ElText>
            </div>
            <ElText v-else type="info">无记录</ElText>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="132">
          <template #default="{ row }">
            <RowMenu
              primary="查看"
              :aria-label="(row as WorkerRow).name"
              :disabled="busyId === String(row.employee.id)"
              :items="workerMenu(row as WorkerRow)"
              @primary="openDetail(row as WorkerRow)"
              @command="(command) => onWorkerCommand(row as WorkerRow, command)"
            />
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="store.error && !store.employees.length" :image-size="72" description="Worker 数据加载失败">
            <ElText type="info">{{ store.error }}</ElText>
            <ElButton :icon="RefreshCw" @click="refreshAll">重新加载</ElButton>
          </ElEmpty>
          <ElEmpty v-else-if="!store.loading && !healthLoading" :image-size="72" :description="store.employees.length ? '没有匹配 Worker' : '暂无 Worker'">
            <ElText type="info">{{ store.employees.length ? '调整搜索或筛选条件后重试。' : '创建第一台 Worker 后即可开始管理。' }}</ElText>
            <ElButton v-if="!store.employees.length" type="primary" :icon="Plus" @click="beginCreate">新建 Worker</ElButton>
          </ElEmpty>
        </template>
      </ElTable>
    </section>

    <footer class="astra-page__footer">
      <ElText type="info" data-testid="employee-total">共 {{ filtered.length }} 台</ElText>
      <nav aria-label="Worker 列表分页">
        <ElPagination v-model:current-page="page" :page-size="pageSize" :total="filtered.length" layout="prev, pager, next" data-testid="employee-pagination" />
      </nav>
    </footer>

    <EmployeeConfigModal :open="modalOpen" :employee="modalEmployee" :creating="modalCreating" :categories="store.categories" @close="closeModal" @saved="onModalSaved" />
  </div>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElButton, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElSpace, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import { getWorkerHealth, submitMaintenance, type WorkerHealth } from '@/api/maintenance'
import type { Employee } from '@/api/types'
import AstraFilterPanel from '@/components/astra/AstraFilterPanel.vue'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import RowMenu, { type RowMenuItem } from '@/components/astra/RowMenu.vue'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
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

const store = useAstraStore()
const route = useRoute()
const router = useRouter()
const query = ref('')
const connection = ref(typeof route.query.status === 'string' ? route.query.status : '')
const maintenance = ref('')
const enabled = ref('')
const page = ref(1)
const pageSize = 10
const modalOpen = ref(false)
const modalCreating = ref(false)
const modalEmployee = ref<Employee | null>(null)
const busyId = ref('')
const healthByEnvironment = ref<Record<number, WorkerHealth>>({})
const healthLoading = ref(false)
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

const environmentEmployees = computed(() => store.employees.filter((employee) => employee.execution_environment))
const rows = computed<WorkerRow[]>(() => environmentEmployees.value.map((employee) => {
  const environment = employee.execution_environment
  if (!environment) throw new Error('unreachable')
  return toWorkerRow(employee, environment, store.tasks, healthByEnvironment.value[environment.id] ?? null)
}))
const filtered = computed(() => rows.value.filter((row) =>
  workerMatches(row, query.value)
  && (!connection.value || row.connection === connection.value)
  && (!maintenance.value || maintenanceFilter(row) === maintenance.value)
  && (!enabled.value || enabledState(row) === enabled.value),
))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
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

async function refreshAll() {
  await store.refresh()
  await loadHealth()
}
function openDetail(row: WorkerRow) {
  void router.push(`/settings/workers/${row.employee.id}`)
}
function workerMenu(row: WorkerRow): RowMenuItem[] {
  return [
    { command: 'edit', label: '编辑' },
    { command: 'check', label: '检查', disabled: isMaintenanceActive(row.maintenanceState) },
  ]
}
function onWorkerCommand(row: WorkerRow, command: string) {
  if (command === 'edit') beginEdit(row.employee)
  else if (command === 'check') void checkWorker(row)
}
async function checkWorker(row: WorkerRow) {
  busyId.value = String(row.employee.id)
  try {
    const created = await submitMaintenance(row.environment.id, 'check')
    notify(`检查已提交：${created.id}`)
    await loadHealth()
  } catch (error) {
    notify(error instanceof Error ? error.message : '检查提交失败', 'error')
  } finally {
    busyId.value = ''
  }
}

function applySearch() {
  page.value = 1
  if (!store.loading) void refreshAll()
}
function beginCreate() { modalEmployee.value = null; modalCreating.value = true; modalOpen.value = true }
function beginEdit(employee: Employee | null) { if (!employee) return; modalEmployee.value = employee; modalCreating.value = false; modalOpen.value = true }
function closeModal() { modalOpen.value = false; modalEmployee.value = null }
async function onModalSaved(id: string) {
  await store.refresh()
  await loadHealth()
  if (modalOpen.value) {
    modalCreating.value = false
    modalEmployee.value = store.employees.find((item) => item.id === id) ?? null
  }
}

watch([query, connection, maintenance, enabled], () => { page.value = 1 })
watch(totalPages, (value) => { page.value = Math.min(page.value, value) })
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
  // 深链打开弹框时确保员工数据已就绪，避免与全局刷新竞争。
  if (!store.employees.length && (editId || create || !store.loading)) await store.refresh()
  if (editId) beginEdit(store.employees.find((item) => item.id === editId) ?? null)
  else if (create) beginCreate()
})
</script>

<style scoped>
/* 页面骨架、标题区、筛选面板、表格面板与页脚统一走 styles/page-layout.css，
   这里只保留表格单元格内部的排版。 */
.employee-cell { display: flex; align-items: flex-start; gap: 10px; min-width: 0; }
.employee-cell__meta, .environment-cell, .worker-health-cell { display: grid; gap: 2px; min-width: 0; }
.worker-health-cell { justify-items: start; gap: 4px; }
.employee-cell__meta strong, .task-link strong, .environment-cell strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.employee-cell__id { font-size: var(--astra-font-helper); }
.task-link, .task-link:hover { color: inherit; text-decoration: none; }
.task-link:hover strong { color: var(--astra-primary); }
</style>
