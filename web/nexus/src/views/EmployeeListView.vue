<template>
  <div class="astra-page" data-testid="employee-list-page">
    <AstraPageHeader title="Agent Worker" hide-title testid="employee-header">
      <template #actions>
        <ElButton :icon="RefreshCw" :loading="store.loading" data-testid="employee-refresh" @click="store.refresh()">刷新</ElButton>
        <ElButton type="primary" :icon="Plus" data-testid="employee-create" @click="beginCreate">新建 Worker</ElButton>
      </template>
    </AstraPageHeader>

    <AstraFilterPanel label="Worker 筛选" testid="employee-filter">
      <ElSelect v-model="status" class="astra-filter__select" data-testid="employee-filter-status" aria-label="Worker 状态" placeholder="状态：全部" clearable>
        <ElOption v-for="option in statusOptions" :key="option.value" :value="option.value" :label="option.label" />
      </ElSelect>
      <ElSelect v-model="role" class="astra-filter__select" data-testid="employee-filter-role" aria-label="Worker 职责" placeholder="职责：全部" clearable>
        <ElOption v-for="item in roles" :key="item" :value="item" :label="item" />
      </ElSelect>
      <ElSelect v-model="area" class="astra-filter__select" data-testid="employee-filter-area" aria-label="所属区域" placeholder="区域：全部" clearable>
        <ElOption v-for="item in workspaceAreas" :key="item.id" :value="item.id" :label="item.name" />
      </ElSelect>
      <ElSelect v-model="enabled" class="astra-filter__select" data-testid="employee-filter-enabled" aria-label="启用状态" placeholder="启用：全部" clearable>
        <ElOption value="enabled" label="已启用" />
        <ElOption value="disabled" label="已停用" />
      </ElSelect>
      <template #search>
        <ElInput v-model="query" class="astra-filter__keyword" data-testid="employee-filter-keyword" aria-label="搜索 Worker" placeholder="搜索 Worker 名称 / 编号 / 职责 / 任务" clearable>
          <template #prefix><Search :size="15" /></template>
        </ElInput>
        <ElButton type="primary" :icon="Search" data-testid="employee-search" @click="applySearch">搜索</ElButton>
      </template>
    </AstraFilterPanel>

    <section class="astra-table-panel astra-table-panel--clickable" aria-label="Worker 列表">
      <ElTable v-loading="store.loading" :data="paged" height="100%" row-key="id" @row-click="onRowClick">
        <ElTableColumn label="Worker" min-width="220">
          <template #default="{ row }">
            <div class="employee-cell">
              <EmployeeAvatar :employee="row.employee" size="small" />
              <span class="employee-cell__meta">
                <strong>{{ row.name }}</strong>
                <ElText type="info" class="employee-cell__id">{{ row.id }}</ElText>
              </span>
            </div>
          </template>
        </ElTableColumn>
        <ElTableColumn label="职责" prop="role" min-width="140" show-overflow-tooltip />
        <ElTableColumn label="运行状态" width="120">
          <template #default="{ row }">
            <ElTag :type="statusTagType(row.status)">{{ row.statusText }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="当前任务" min-width="210">
          <template #default="{ row }">
            <RouterLink v-if="row.currentTask" class="task-link" :to="`/tasks/${row.currentTask.id}`" :title="row.currentTaskName" @click.stop>
              <strong>{{ row.currentTaskName }}</strong>
              <ElText type="info" truncated>{{ row.currentTask.runtime_status_label }}</ElText>
            </RouterLink>
            <ElText v-else type="info">—</ElText>
          </template>
        </ElTableColumn>
        <ElTableColumn label="启用" width="100">
          <template #default="{ row }">
            <ElTag :type="row.employee.enabled ? 'success' : 'info'" :effect="row.employee.enabled ? 'light' : 'plain'">{{ row.employee.enabled ? '已启用' : '已停用' }}</ElTag>
          </template>
        </ElTableColumn>
        <ElTableColumn label="操作" width="132">
          <template #default="{ row }">
            <RowMenu
              primary="查看"
              :aria-label="(row as EmployeeViewModel).name"
              :disabled="busyId === row.id"
              :items="workerMenu(row as EmployeeViewModel)"
              @primary="openDetail(row.id)"
              @command="(command) => onWorkerCommand(row as EmployeeViewModel, command)"
            />
          </template>
        </ElTableColumn>
        <template #empty>
          <ElEmpty v-if="store.error && !store.employees.length" :image-size="72" description="Worker 数据加载失败">
            <ElText type="info">{{ store.error }}</ElText>
            <ElButton :icon="RefreshCw" @click="store.refresh()">重新加载</ElButton>
          </ElEmpty>
          <ElEmpty v-else-if="!store.loading" :image-size="72" :description="store.employees.length ? '没有匹配 Worker' : '暂无 Worker'">
            <ElText type="info">{{ store.employees.length ? '调整搜索或筛选条件后重试。' : '创建第一位 Worker 后即可开始管理。' }}</ElText>
            <ElButton v-if="!store.employees.length" type="primary" :icon="Plus" @click="beginCreate">新建 Worker</ElButton>
          </ElEmpty>
        </template>
      </ElTable>
    </section>

    <footer class="astra-page__footer">
      <ElText type="info" data-testid="employee-total">共 {{ filtered.length }} 条</ElText>
      <nav aria-label="Worker 列表分页">
        <ElPagination v-model:current-page="page" :page-size="pageSize" :total="filtered.length" layout="prev, pager, next" data-testid="employee-pagination" />
      </nav>
    </footer>

    <EmployeeDetailDrawer :employee="detailEmployee" :tasks="store.tasks" @close="detailId = ''" @edit="beginEdit(detailEmployee)" />

    <EmployeeConfigModal :open="modalOpen" :employee="modalEmployee" :creating="modalCreating" :categories="store.categories" @close="closeModal" @saved="onModalSaved" />
  </div>
</template>

<script setup lang="ts">
import { Plus, RefreshCw, Search } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElButton, ElEmpty, ElInput, ElOption, ElPagination, ElSelect, ElTable, ElTableColumn, ElTag, ElText, vLoading } from 'element-plus'
import { api } from '@/api/client'
import type { Employee } from '@/api/types'
import AstraFilterPanel from '@/components/astra/AstraFilterPanel.vue'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import RowMenu, { type RowMenuItem } from '@/components/astra/RowMenu.vue'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import EmployeeDetailDrawer from '@/components/employee/EmployeeDetailDrawer.vue'
import { confirmDialog } from '@/composables/useConfirm'
import { notify } from '@/composables/useToast'
import { employeeMatches, toEmployeeViewModel, workspaceAreas, type EmployeeViewModel } from '@/features/employees/presentation'
import { useAstraStore } from '@/stores/astra'
import { statusTone } from '@/utils/format'

const store = useAstraStore()
const route = useRoute()
const query = ref('')
const status = ref(typeof route.query.status === 'string' ? route.query.status : '')
const role = ref('')
const area = ref('')
const enabled = ref('')
const page = ref(1)
const pageSize = 10
const detailId = ref('')
const models = computed(() => store.employees.map((employee) => toEmployeeViewModel(employee, store.tasks)))
const roles = computed(() => [...new Set(store.employees.map((employee) => employee.role))].sort((left, right) => left.localeCompare(right, 'zh-CN')))
const filtered = computed(() => models.value.filter((item) =>
  employeeMatches(item, query.value)
  && (!status.value || item.status === status.value)
  && (!role.value || item.role === role.value)
  && (!area.value || item.areaId === area.value)
  && (!enabled.value || item.employee.enabled === (enabled.value === 'enabled')),
))
const totalPages = computed(() => Math.max(1, Math.ceil(filtered.value.length / pageSize)))
const paged = computed(() => filtered.value.slice((page.value - 1) * pageSize, page.value * pageSize))
const detailEmployee = computed(() => store.employees.find((employee) => employee.id === detailId.value) ?? null)
const modalOpen = ref(false)
const modalCreating = ref(false)
const modalEmployee = ref<Employee | null>(null)
const busyId = ref('')
const statusOptions = [
  { value: 'WORKING', label: '工作中' },
  { value: 'IDLE', label: '空闲' },
  { value: 'WAIT_HUMAN', label: '需要人工处理' },
  { value: 'PAUSED', label: '已暂停' },
  { value: 'ERROR', label: '异常' },
  { value: 'OFFLINE', label: '离线' },
]
const TAG_TYPES = { info: 'primary', success: 'success', warning: 'warning', danger: 'danger', neutral: 'info' } as const
const statusTagType = (value: string) => TAG_TYPES[statusTone(value)]
function openDetail(id: string) { detailId.value = id }
function workerMenu(row: EmployeeViewModel): RowMenuItem[] {
  return [
    { command: 'edit', label: '编辑' },
    { command: 'toggle', label: row.employee.enabled ? '停用' : '启用' },
    { command: 'delete', label: '删除' },
  ]
}
function onWorkerCommand(row: EmployeeViewModel, command: string) {
  if (command === 'edit') beginEdit(row.employee)
  else if (command === 'toggle') void toggleEnabled(row.employee)
  else if (command === 'delete') void removeEmployee(row.employee)
}
function onRowClick(row: EmployeeViewModel) { openDetail(row.id) }
function applySearch() {
  page.value = 1
  if (!store.loading) void store.refresh()
}
function beginCreate() { modalEmployee.value = null; modalCreating.value = true; modalOpen.value = true }
function beginEdit(employee: Employee | null) { if (!employee) return; detailId.value = ''; modalEmployee.value = employee; modalCreating.value = false; modalOpen.value = true }
function closeModal() { modalOpen.value = false; modalEmployee.value = null }
async function onModalSaved(id: string) {
  const wasCreating = modalCreating.value
  await store.refresh()
  if (modalOpen.value) {
    modalCreating.value = false
    modalEmployee.value = store.employees.find((item) => item.id === id) ?? null
  }
  notify(wasCreating ? 'Worker 已创建' : 'Worker 已更新')
}
async function toggleEnabled(employee: Employee) {
  busyId.value = employee.id
  try { await api.updateEmployee(employee.id, { enabled: !employee.enabled }); await store.refresh(); notify(employee.enabled ? 'Worker 已停用' : 'Worker 已启用') }
  catch (reason) { notify(reason instanceof Error ? reason.message : '操作失败', 'error') }
  finally { busyId.value = '' }
}
async function removeEmployee(employee: Employee) {
  const confirmed = await confirmDialog({ title: '删除 Worker', message: `确认删除「${employee.name}」？删除后无法恢复。`, confirmLabel: '删除 Worker', danger: true })
  if (!confirmed) return
  busyId.value = employee.id
  try { await api.deleteEmployee(employee.id); if (detailId.value === employee.id) detailId.value = ''; await store.refresh(); notify('Worker 已删除') }
  catch (reason) { notify(reason instanceof Error ? reason.message : '删除失败', 'error') }
  finally { busyId.value = '' }
}
watch([query, status, role, area, enabled], () => { page.value = 1 })
watch(totalPages, (value) => { page.value = Math.min(page.value, value) })
watch(() => store.employees, (employees) => {
  if (modalEmployee.value) modalEmployee.value = employees.find((item) => item.id === modalEmployee.value?.id) ?? null
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
.employee-cell { display: flex; align-items: center; gap: 10px; min-width: 0; }
.employee-cell__meta, .task-link, .environment-cell { display: grid; min-width: 0; }
.employee-cell__meta strong, .task-link strong { overflow: hidden; color: var(--astra-text-primary); font-size: var(--astra-font-label); text-overflow: ellipsis; white-space: nowrap; }
.employee-cell__id { font-size: var(--astra-font-helper); }
.task-link, .task-link:hover { color: inherit; text-decoration: none; }
.task-link:hover strong { color: var(--astra-primary); }
</style>
