<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { ElMessage } from 'element-plus'

import {
  getEmployee,
  listEmployees,
  reconcileEmployee,
  updateEmployee,
} from '@/api/employees'
import { errorMessage, isUncertainWrite } from '@/api/errors'
import { canToggleEnabled, isSwitchInteractive } from '@/api/guards'
import { isReleasedResult, type Employee, type SyncState } from '@/api/types'
import EmployeeEditorDrawer from '@/components/EmployeeEditorDrawer.vue'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { loadAllResources, resourceName } from '@/stores/resources'

const employees = ref<Employee[]>([])
const total = ref(0)
const query = ref('')
const loading = ref(false)
const loadError = ref<string | null>(null)

const selectedId = ref<string | null>(null)
const detail = ref<Employee | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

const toggling = reactive<Record<string, boolean>>({})
const reconciling = reactive<Record<string, boolean>>({})

const drawerOpen = ref(false)
const drawerMode = ref<'create' | 'edit'>('create')
const drawerEmployee = ref<Employee | null>(null)
const openingEditor = ref(false)

const isNarrow = useMediaQuery('(max-width: 650px)')

let listSeq = 0
let detailSeq = 0
let searchTimer: ReturnType<typeof setTimeout> | null = null

function truncate(text: string, max: number): string {
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function syncLabel(state: SyncState): string {
  switch (state) {
    case 'creating':
      return '创建中'
    case 'updating':
      return '更新中'
    case 'incomplete':
      return '未完成'
    case 'deleting':
      return '删除中'
    default:
      return ''
  }
}

function skillLabels(employee: Employee): string[] {
  return employee.skills.slice(0, 2).map((id) => resourceName('skills', id))
}

function extraSkillCount(employee: Employee): number {
  return Math.max(0, employee.skills.length - 2)
}

function syncRow(employee: Employee): void {
  const index = employees.value.findIndex((item) => item.id === employee.id)
  if (index >= 0) employees.value[index] = employee
  else employees.value.unshift(employee)
}

function clearSelection(): void {
  detailSeq += 1
  selectedId.value = null
  detail.value = null
  detailError.value = null
  detailLoading.value = false
}

async function selectEmployee(employeeId: string): Promise<void> {
  selectedId.value = employeeId
  const seq = ++detailSeq
  detailLoading.value = true
  detailError.value = null
  detail.value = null
  try {
    const data = await getEmployee(employeeId)
    if (seq !== detailSeq) return
    detail.value = data
    syncRow(data)
  } catch (error) {
    if (seq !== detailSeq) return
    detailError.value = errorMessage(error)
  } finally {
    if (seq === detailSeq) detailLoading.value = false
  }
}

async function loadEmployees(
  options: { selectFallback?: boolean } = {},
): Promise<void> {
  const selectFallback = options.selectFallback ?? true
  const seq = ++listSeq
  loading.value = true
  loadError.value = null
  try {
    const data = await listEmployees(query.value)
    if (seq !== listSeq) return
    employees.value = data.employees
    total.value = data.total
    const current = selectedId.value
    if (current && data.employees.some((item) => item.id === current)) {
      // 每次成功刷新列表也刷新选中详情，避免保留旧 native_available/状态。
      await selectEmployee(current)
      return
    }
    if (selectFallback) {
      const first = data.employees[0]
      if (first) await selectEmployee(first.id)
      else clearSelection()
    } else {
      clearSelection()
    }
  } catch (error) {
    if (seq !== listSeq) return
    loadError.value = errorMessage(error)
    employees.value = []
    total.value = 0
    clearSelection()
  } finally {
    if (seq === listSeq) loading.value = false
  }
}

watch(query, () => {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void loadEmployees()
  }, 300)
})

function onRowClick(row: Employee): void {
  if (row.id !== selectedId.value) void selectEmployee(row.id)
}

function rowClass(data: { row: Employee }): string {
  return data.row.id === selectedId.value ? 'is-selected' : ''
}

async function onToggleEnabled(employee: Employee, next: boolean): Promise<void> {
  if (toggling[employee.id]) return
  if (!canToggleEnabled(employee.sync_state, employee.enabled, next)) return
  toggling[employee.id] = true
  try {
    const updated = await updateEmployee(employee.id, { enabled: next })
    syncRow(updated)
    if (selectedId.value === employee.id) detail.value = updated
    ElMessage.success(
      next ? `${updated.name} 已启用` : `${updated.name} 已禁用`,
    )
  } catch (error) {
    if (isUncertainWrite(error)) {
      // 结果未确认：刷新真实状态/进入核对路径，不宣称失败即已回滚。
      ElMessage.warning('启停结果未确认，正在刷新真实状态。')
      await loadEmployees()
    } else {
      // 可靠失败：不修改 model-value，开关自动回到服务端原值。
      ElMessage.error(errorMessage(error))
    }
  } finally {
    toggling[employee.id] = false
  }
}

async function onReconcile(employee: Employee): Promise<void> {
  if (reconciling[employee.id]) return
  reconciling[employee.id] = true
  try {
    const result = await reconcileEmployee(employee.id)
    if (isReleasedResult(result)) {
      ElMessage.info('未完成占位已释放，可重新创建该员工。')
      if (selectedId.value === employee.id) clearSelection()
      await loadEmployees()
    } else {
      syncRow(result)
      if (selectedId.value === employee.id) detail.value = result
      ElMessage.success('已重新核对员工状态。')
    }
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    reconciling[employee.id] = false
  }
}

function openCreate(): void {
  drawerMode.value = 'create'
  drawerEmployee.value = null
  drawerOpen.value = true
}

async function openEdit(employeeId: string): Promise<void> {
  if (openingEditor.value) return
  openingEditor.value = true
  try {
    const data = await getEmployee(employeeId)
    drawerMode.value = 'edit'
    drawerEmployee.value = data
    drawerOpen.value = true
  } catch (error) {
    ElMessage.error(errorMessage(error))
  } finally {
    openingEditor.value = false
  }
}

async function onSaved(employee: Employee): Promise<void> {
  // 新建：显式清空筛选，保证新员工可见且计数与服务端一致。
  if (drawerMode.value === 'create' && query.value) {
    query.value = ''
  }
  clearSelection()
  await loadEmployees({ selectFallback: false })
  // 编辑后不再匹配当前筛选时不污染结果：清空选中，保持服务端筛选与计数。
  if (employees.value.some((item) => item.id === employee.id)) {
    await selectEmployee(employee.id)
  }
}

const visibleDetail = computed(() => detail.value)

onMounted(() => {
  void loadEmployees()
  void loadAllResources()
})

onBeforeUnmount(() => {
  if (searchTimer) clearTimeout(searchTimer)
})
</script>

<template>
  <div class="manage-view">
    <div class="title-row">
      <h1 class="h-title">员工管理</h1>
      <el-button type="primary" @click="openCreate">＋ 创建数字员工</el-button>
    </div>

    <div class="management-grid">
      <section class="panel management-main">
        <div class="manage-toolbar">
          <div class="list-heading">
            <b>员工列表</b>
            <span v-if="!loading && !loadError" class="overview-count">
              共 {{ total }} 位员工
            </span>
          </div>
          <div class="toolbar-right">
            <el-button
              text
              size="small"
              :loading="loading"
              title="刷新员工列表与当前档案"
              @click="loadEmployees()"
            >
              刷新
            </el-button>
            <el-input
              v-model="query"
              class="search"
              placeholder="搜索员工 / 职责"
              clearable
            />
          </div>
        </div>

        <div v-if="loading" class="list-state">加载中…</div>

        <el-alert
          v-else-if="loadError"
          type="error"
          :closable="false"
          show-icon
          title="读取员工列表失败"
        >
          <p>{{ loadError }}</p>
          <el-button size="small" @click="loadEmployees()">重试</el-button>
        </el-alert>

        <div v-else-if="employees.length === 0 && query" class="list-state">
          没有找到符合条件的员工
        </div>

        <div v-else-if="employees.length === 0" class="list-state empty-create">
          <p>还没有数字员工</p>
          <el-button type="primary" @click="openCreate">
            ＋ 创建数字员工
          </el-button>
        </div>

        <el-table
          v-else
          :data="employees"
          row-key="id"
          :row-class-name="rowClass"
          @row-click="onRowClick"
        >
          <el-table-column label="数字员工" :min-width="isNarrow ? 130 : 210">
            <template #default="{ row }">
              <div class="person-cell">
                <span class="avatar" :class="{ off: !row.enabled }">
                  {{ row.avatar }}
                </span>
                <span class="person-meta">
                  <b :title="row.name">{{ row.name }}</b>
                  <small
                    v-if="!isNarrow"
                    class="mono"
                    :title="row.id"
                  >{{ row.id }}</small>
                </span>
              </div>
            </template>
          </el-table-column>

          <el-table-column v-if="!isNarrow" label="职责" min-width="200">
            <template #default="{ row }">
              <b class="domain">{{ row.domain }}</b>
              <div class="role-desc">
                {{ truncate((row as Employee).responsibilities, 90) }}
              </div>
            </template>
          </el-table-column>

          <el-table-column v-if="!isNarrow" label="技能" min-width="140">
            <template #default="{ row }">
              <div class="chips">
                <span
                  v-for="(name, index) in skillLabels(row as Employee)"
                  :key="index"
                  class="mini-chip"
                >
                  {{ name }}
                </span>
                <span
                  v-if="extraSkillCount(row as Employee) > 0"
                  class="inline-note"
                >
                  +{{ extraSkillCount(row as Employee) }}
                </span>
                <span v-if="!row.skills.length" class="muted small">—</span>
              </div>
            </template>
          </el-table-column>

          <el-table-column label="状态" :width="isNarrow ? 76 : 128">
            <template #default="{ row }">
              <div class="status-cell" @click.stop>
                <el-switch
                  :model-value="row.enabled"
                  :loading="toggling[row.id]"
                  :disabled="
                    !isSwitchInteractive(row.sync_state, row.enabled)
                  "
                  @change="
                    (value) => onToggleEnabled(row as Employee, Boolean(value))
                  "
                />
                <span
                  v-if="row.sync_state !== 'ready'"
                  class="sync-note"
                >
                  {{ syncLabel(row.sync_state) }}
                </span>
              </div>
            </template>
          </el-table-column>

          <el-table-column
            label="操作"
            :width="isNarrow ? 96 : 168"
            align="right"
            fixed="right"
          >
            <template #default="{ row }">
              <div class="row-actions" @click.stop>
                <el-button
                  v-if="!isNarrow"
                  link
                  type="primary"
                  size="small"
                  disabled
                  title="数字员工页面尚未接入，后续阶段提供"
                >
                  对话
                </el-button>
                <el-button
                  link
                  type="primary"
                  size="small"
                  :disabled="row.sync_state !== 'ready' || openingEditor"
                  @click="openEdit(row.id)"
                >
                  编辑
                </el-button>
                <el-button
                  v-if="row.sync_state !== 'ready'"
                  link
                  type="warning"
                  size="small"
                  :loading="reconciling[row.id]"
                  @click="onReconcile(row as Employee)"
                >
                  核对
                </el-button>
              </div>
            </template>
          </el-table-column>
        </el-table>
      </section>

      <aside class="panel management-side">
        <div v-if="detailLoading" class="side-state">加载中…</div>

        <el-alert
          v-else-if="detailError"
          type="error"
          :closable="false"
          show-icon
          title="读取员工详情失败"
        >
          <p>{{ detailError }}</p>
          <el-button
            size="small"
            @click="selectedId && selectEmployee(selectedId)"
          >
            重试
          </el-button>
        </el-alert>

        <template v-else-if="visibleDetail">
          <div class="side-head">
            <b class="panel-title">员工档案</b>
            <el-button
              size="small"
              :disabled="
                visibleDetail.sync_state !== 'ready' || openingEditor
              "
              @click="openEdit(visibleDetail.id)"
            >
              编辑
            </el-button>
          </div>

          <div class="profile-hero">
            <span class="avatar lg" :class="{ off: !visibleDetail.enabled }">
              {{ visibleDetail.avatar }}
            </span>
            <h3>{{ visibleDetail.name }}</h3>
            <p>{{ visibleDetail.domain }}</p>
            <div class="status-row">
              <span class="tag" :class="{ ash: !visibleDetail.enabled }">
                {{ visibleDetail.enabled ? '● 已启用' : '○ 已禁用' }}
              </span>
              <span
                v-if="visibleDetail.sync_state !== 'ready'"
                class="tag warn"
              >
                {{ syncLabel(visibleDetail.sync_state) }}
              </span>
              <span
                v-if="visibleDetail.name_source === 'local'"
                class="tag warn"
              >
                名称未同步原生
              </span>
            </div>
            <template v-if="visibleDetail.sync_state !== 'ready'">
              <p class="side-warning">
                {{
                  visibleDetail.sync_state === 'deleting'
                    ? '该员工正在删除中，不可编辑或启停。'
                    : '该员工存在未完成的写入，仅可禁用；请先核对为一致状态后再编辑或启用。'
                }}
              </p>
              <el-button
                v-if="visibleDetail.sync_state !== 'deleting'"
                size="small"
                :loading="reconciling[visibleDetail.id]"
                @click="onReconcile(visibleDetail)"
              >
                重新核对
              </el-button>
            </template>
          </div>

          <div class="section-label">职责范围</div>
          <div class="detail-excerpt">{{ visibleDetail.responsibilities }}</div>

          <div class="divider" />

          <div class="section-label">知识资源</div>
          <div class="resource-list">
            <span
              v-for="id in visibleDetail.knowledge"
              :key="id"
              class="mini-chip"
              :title="id"
            >
              {{ resourceName('knowledge', id) }}
            </span>
            <span v-if="!visibleDetail.knowledge.length" class="muted small">
              暂无配置
            </span>
          </div>

          <div class="section-label">技能</div>
          <div class="resource-list">
            <span
              v-for="id in visibleDetail.skills"
              :key="id"
              class="mini-chip"
              :title="id"
            >
              {{ resourceName('skills', id) }}
            </span>
            <span v-if="!visibleDetail.skills.length" class="muted small">
              暂无配置
            </span>
          </div>

          <div class="section-label">工具</div>
          <div class="resource-list">
            <span
              v-for="id in visibleDetail.tools"
              :key="id"
              class="mini-chip"
              :title="id"
            >
              ⚙ {{ resourceName('mcps', id) }}
            </span>
            <span v-if="!visibleDetail.tools.length" class="muted small">
              暂无配置
            </span>
          </div>

          <div class="divider" />
          <div class="kv">
            <span>员工 ID</span>
            <strong class="mono">{{ visibleDetail.id }}</strong>
          </div>

          <div class="archive-actions">
            <el-button
              type="primary"
              disabled
              title="数字员工页面尚未接入，后续阶段提供"
            >
              与 {{ visibleDetail.name }} 交流 ↗
            </el-button>
          </div>
        </template>

        <div v-else class="side-state">
          <template v-if="loadError">
            员工列表加载失败，暂无法显示档案
          </template>
          <template v-else-if="loading">正在加载员工列表…</template>
          <template v-else-if="employees.length === 0 && query">
            没有匹配的员工
          </template>
          <template v-else-if="employees.length === 0">
            创建第一个数字员工，直接定义他的职责与能力
          </template>
          <template v-else>选择左侧员工查看档案</template>
        </div>
      </aside>
    </div>

    <EmployeeEditorDrawer
      v-model="drawerOpen"
      :mode="drawerMode"
      :employee="drawerEmployee"
      @saved="onSaved"
      @refresh="loadEmployees"
    />
  </div>
</template>

<style scoped>
.manage-view {
  min-width: 0;
}

.management-grid {
  display: grid;
  grid-template-columns: minmax(550px, 1fr) 345px;
  align-items: start;
  gap: 16px;
}

.management-main {
  padding: 18px;
  min-width: 0;
}

.management-side {
  padding: 18px;
  min-width: 0;
}

.manage-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px 10px;
  margin-bottom: 14px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
  margin-left: auto;
}

.list-heading {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex: none;
  white-space: nowrap;
}

.list-heading b {
  font-size: 14px;
}

.overview-count {
  color: #839b8c;
  font-size: 12px;
}

.search {
  width: 265px;
}

.list-state {
  padding: 60px 15px;
  text-align: center;
  color: #829d8c;
}

.empty-create p {
  margin: 0 0 14px;
}

.person-cell {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.avatar {
  display: grid;
  place-items: center;
  width: 38px;
  height: 38px;
  border-radius: 13px;
  background: var(--mint);
  font-size: 19px;
  flex: none;
}

.avatar.lg {
  width: 46px;
  height: 46px;
  border-radius: 15px;
  font-size: 23px;
}

.avatar.off {
  filter: grayscale(0.85);
  opacity: 0.6;
}

.person-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  min-width: 0;
}

.person-meta b {
  font-size: 12px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.person-meta small {
  display: block;
  max-width: 160px;
  overflow: hidden;
  color: #8ba093;
  font-size: 10px;
  white-space: nowrap;
  text-overflow: ellipsis;
}

.domain {
  font-size: 12px;
}

.role-desc {
  max-width: 310px;
  margin-top: 4px;
  color: #617f70;
  font-size: 11px;
  line-height: 1.7;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 5px;
}

.mini-chip {
  display: inline-flex;
  align-items: center;
  padding: 3px 7px;
  border: 1px solid #d5eade;
  border-radius: 7px;
  background: #edf8f2;
  color: #286a4b;
  font-size: 11px;
}

.inline-note {
  color: #839b8c;
  font-size: 10px;
}

.status-cell {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
}

.sync-note {
  color: #c08a3c;
  font-size: 10px;
  white-space: nowrap;
}

.row-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0;
  white-space: nowrap;
}

.row-actions :deep(.el-button) {
  padding: 4px 5px;
}

.row-actions :deep(.el-button + .el-button) {
  margin-left: 4px;
}

.side-state {
  padding: 60px 10px;
  text-align: center;
  color: #829d8c;
  line-height: 1.8;
}

.side-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}

.panel-title {
  font-size: 14px;
}

.profile-hero {
  position: relative;
  overflow: hidden;
  padding: 19px;
  border: 1px solid #e3f2e9;
  border-radius: 16px;
  background: linear-gradient(135deg, #f1fbf6, #edf6f6 67%, #f7fcfc);
}

.profile-hero h3 {
  margin: 10px 0 5px;
  font-size: 17px;
}

.profile-hero > p {
  margin: 0;
  color: #668879;
  font-size: 11px;
  line-height: 1.7;
}

.status-row {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 12px;
}

.tag {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 8px;
  background: var(--mint);
  color: var(--deep);
  font-size: 11px;
}

.tag.ash {
  background: #f0f3f1;
  color: #8ba093;
}

.tag.warn {
  background: #fdf4e4;
  color: #b57e26;
}

.side-warning {
  margin: 12px 0 10px !important;
  padding: 10px 12px;
  border: 1px solid #e7eee3;
  border-radius: 10px;
  background: #f7faf5;
  color: #829889;
}

.section-label {
  margin: 16px 0 8px;
  color: #91a59a;
  font-size: 10px;
  font-weight: 750;
  letter-spacing: 1px;
}

.detail-excerpt {
  color: var(--soft-ink);
  font-size: 12px;
  line-height: 1.85;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.resource-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}

.divider {
  height: 1px;
  margin: 16px 0 0;
  background: #eef3ef;
}

.kv {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 14px;
  color: #7b9386;
  font-size: 11px;
}

.kv strong {
  color: var(--ink);
  font-size: 11px;
  overflow-wrap: anywhere;
  text-align: right;
}

.archive-actions {
  margin-top: 18px;
}

.archive-actions :deep(.el-button) {
  width: 100%;
}

:deep(.el-table .is-selected > td) {
  background: #f3faf6;
}

:deep(.el-table tbody tr:hover > td) {
  background: #f8fcf9;
}

:deep(.el-table th.el-table__cell) {
  background: #f8fbf9;
  color: #91a59a;
  font-size: 11px;
}

@media (max-width: 1250px) {
  .management-grid {
    grid-template-columns: minmax(500px, 1fr) 300px;
  }
}

@media (max-width: 1000px) {
  .management-grid {
    grid-template-columns: 1fr;
  }
  .search {
    width: 200px;
  }
}

@media (max-width: 650px) {
  .management-main {
    padding: 10px;
  }
  .manage-toolbar {
    align-items: flex-start;
  }
  .list-heading {
    width: 100%;
  }
  .toolbar-right {
    width: 100%;
    margin-left: 0;
  }
  .search {
    flex: 1 1 auto;
    width: auto;
    min-width: 0;
  }
}
</style>
