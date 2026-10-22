<template>
  <div v-if="employee" class="worker-detail" data-testid="employee-detail-page">
    <section class="worker-hero">
      <EmployeeAvatar :employee="employee" :emoji="digitalEmployee?.avatar" size="large" />
      <div class="worker-hero__identity">
        <div class="worker-hero__eyebrow">
          <span>Agent Worker</span>
          <span>环境 #{{ environment?.id ?? '—' }}</span>
          <span>{{ environment ? (environment.mode === 'LINUX_REMOTE' ? 'Linux 远程' : '本地') : '未配置环境' }}</span>
        </div>
        <h2>{{ workerTitle }}</h2>
        <p>
          <template v-if="digitalEmployee">{{ digitalEmployee.name }} · {{ digitalEmployee.domain }}</template>
          <template v-else-if="employee.digital_employee_id">数字员工 {{ employee.digital_employee_id }}</template>
          <template v-else>公共 Worker</template>
        </p>
        <div class="worker-hero__badges">
          <SemanticTag :tone="schedulableTone(health)" :label="schedulableLabel(health)" />
          <ElButton v-if="blocking.length" link type="primary" :aria-expanded="showAllBlockers" @click="showAllBlockers = !showAllBlockers">
            {{ showAllBlockers ? '收起原因' : `查看原因（${blocking.length}）` }}
          </ElButton>
        </div>
      </div>
      <ElSpace class="worker-hero__actions" wrap :size="8">
        <ElButton :icon="Settings2" @click="editOpen = true">编辑</ElButton>
        <ElButton :icon="RefreshCw" :loading="maintenance.loading.value" @click="refreshAll">刷新</ElButton>
      </ElSpace>
    </section>

    <ElAlert
      v-if="maintenance.healthError.value"
      type="error"
      show-icon
      :closable="false"
      title="健康与维护接口暂不可用，以下仅显示已保存的环境记录。"
    >
      <p>{{ maintenance.healthError.value }}</p>
      <ElButton size="small" @click="maintenance.load">重新加载</ElButton>
    </ElAlert>

    <div v-if="showAllBlockers && blocking.length" class="worker-blocking" role="status">
      <strong>暂不可接任务的原因</strong>
      <ul class="blocking-list"><li v-for="reason in blocking" :key="reason">{{ reason }}</li></ul>
    </div>

    <ElAlert v-if="store.error" type="error" show-icon :closable="false" :title="store.error"><ElButton @click="refreshAll">重新加载</ElButton></ElAlert>

    <ElTabs v-model="detailSection" class="worker-detail__tabs">
      <ElTabPane label="任务" name="tasks">
        <section id="tasks" class="worker-grid">
          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>当前工作</h3>
                <RouterLink v-if="currentTask" class="worker-link" :to="`/tasks/${currentTask.id}`">查看任务 <ArrowUpRight :size="13" /></RouterLink>
              </div>
            </template>
            <div v-if="currentTask">
              <div class="current-task">
                <small>{{ currentTask.id }}</small>
                <strong>{{ currentTask.title }}</strong>
              </div>
              <ElDescriptions :column="3" border size="small">
                <ElDescriptionsItem label="类型">{{ taskTypeLabel(currentTask.task_type) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="分类">{{ categoryName(currentTask.category_id) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="优先级">{{ priorityLabel(currentTask.priority) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="开始时间">{{ formatTime(currentTask.started_at) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前状态">
                  <SemanticTag :tone="semanticTone(statusTone(currentTask.runtime_state))" :label="currentTask.runtime_status_label" />
                </ElDescriptionsItem>
                <ElDescriptionsItem label="运行时长">{{ formatTaskDuration(currentTask) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前进展" :span="3">{{ platformCopy(currentTask.current_progress) || '暂无进展更新' }}</ElDescriptionsItem>
                <ElDescriptionsItem label="执行会话" :span="3">{{ currentTask.current_session_id ?? '执行会话尚未创建' }}</ElDescriptionsItem>
              </ElDescriptions>
            </div>
            <div v-else class="worker-current-empty">
              <p>{{ currentTaskEmptyText }}</p>
              <RouterLink v-if="currentTaskId" class="worker-link" :to="`/tasks/${currentTaskId}`">查看占用任务 {{ currentTaskId }}</RouterLink>
            </div>
          </ElCard>

          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header><div class="worker-card__title"><h3>任务记录</h3><ElText>{{ employeeTasks.length }} 项</ElText></div></template>
            <ElTable v-loading="store.loading" :data="pagedTasks" size="small" @row-click="openTask">
              <ElTableColumn label="任务" min-width="220" show-overflow-tooltip>
                <template #default="{ row }"><div class="task-cell"><strong>{{ row.title }}</strong><small>{{ row.id }}</small></div></template>
              </ElTableColumn>
              <ElTableColumn label="状态" width="120">
                <template #default="{ row }"><SemanticTag :tone="semanticTone(statusTone(row.runtime_state))" :label="row.runtime_status_label" /></template>
              </ElTableColumn>
              <ElTableColumn label="开始时间" width="130"><template #default="{ row }">{{ formatTime(row.started_at) }}</template></ElTableColumn>
              <ElTableColumn label="结束时间" width="130"><template #default="{ row }">{{ formatTime(row.finished_at) }}</template></ElTableColumn>
              <ElTableColumn label="操作" width="70" align="right"><template #default="{ row }"><ElButton link type="primary" @click.stop="openTask(row as Task)">查看</ElButton></template></ElTableColumn>
              <template #empty><ElEmpty :image-size="48" description="暂无任务记录" /></template>
            </ElTable>
            <ElPagination v-if="employeeTasks.length > 10" v-model:current-page="taskPage" :page-size="10" :total="employeeTasks.length" layout="total, prev, pager, next" class="worker-task-pagination" />
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="配置" name="config">
        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>基本信息</h3></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="Worker 名称" :span="2">{{ workerTitle }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Worker ID">{{ employee.id }}</ElDescriptionsItem>
              <ElDescriptionsItem label="环境 ID">#{{ environment?.id ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="数字员工" :span="2">{{ digitalEmployee ? `${digitalEmployee.name}（${digitalEmployee.id}）` : (employee.digital_employee_id || '未关联') }}</ElDescriptionsItem>
              <ElDescriptionsItem label="环境说明" :span="2">{{ environment?.description || '—' }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工作范围与派单</h3></div></template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="任务类型" :span="2">{{ taskTypesLabel }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平台调度">{{ employee.enabled ? '允许' : '已停止' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="自动领取">{{ employee.auto_claim ? '开启' : '关闭' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="自主执行">{{ employee.auto_execute ? '开启' : '关闭' }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard v-if="environment?.mode === 'LINUX_REMOTE'" shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>登录信息</h3><ElText>Linux 远程</ElText></div></template>
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="服务器">{{ environment.host || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="端口">{{ environment.port }}</ElDescriptionsItem>
              <ElDescriptionsItem label="用户名">{{ environment.username || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="SSH 密码">{{ environment.has_ssh_password ? '已配置（不回显）' : '未配置' }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>工程信息</h3></div></template>
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="工作空间">{{ environment?.workspace_root || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="仓库地址">{{ repository.url || environment?.repository_url || '未绑定仓库' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="目标分支">{{ environment?.repository_branch || environment?.branch || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="仓库认证">{{ repository.authentication || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="当前提交">{{ repository.commit || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作区">{{ dirtyLabel(repository.dirty) }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="worker-card">
            <template #header><div class="worker-card__title"><h3>编码 Agent</h3></div></template>
            <ElDescriptions :column="1" border size="small">
              <ElDescriptionsItem label="编程工具">OpenCode</ElDescriptionsItem>
              <ElDescriptionsItem label="编程模型">{{ environment?.model_name || '使用平台默认模型' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="可执行文件">{{ environment?.opencode_path || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Server 端口">{{ environment?.opencode_server_port ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Worker 通道">{{ channelLabel(health?.connection?.channel) }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="维护" name="maintenance">
        <section class="worker-grid">
          <ElCard shadow="never" class="worker-card worker-card--wide">
            <template #header>
              <div class="worker-card__title">
                <h3>部署与维护</h3>
              </div>
            </template>
            <WorkerMaintenancePanel :controller="maintenance" :mode="environment?.mode ?? null" @changed="onMaintenanceChanged" />
          </ElCard>

          <ElCollapse class="worker-diagnostics worker-card--wide">
            <ElCollapseItem title="运行诊断与装备检查" name="diagnostics">
              <div class="worker-diagnostic-status">
                <SemanticTag :tone="connectionTone(connection)" :label="`连接 ${connectionLabel(connection)}`" />
                <SemanticTag :tone="runtimeHealthTone(health)" :label="`OpenCode ${runtimeHealthLabel(health)}`" />
                <ElText>最近检查 {{ formatTime(health?.runtime_health.last_checked_at) }}</ElText>
              </div>
            <ElTable v-if="equipment.length" :data="equipment" size="small">
              <ElTableColumn type="expand" width="40">
                <template #default="{ row }">
                  <ElDescriptions :column="2" border size="small" class="equipment-detail">
                    <ElDescriptionsItem label="已配置">{{ equipmentStateLabel(row.configured) }}</ElDescriptionsItem>
                    <ElDescriptionsItem label="已部署">{{ equipmentStateLabel(row.deployed) }}</ElDescriptionsItem>
                    <ElDescriptionsItem label="目标版本">{{ row.desired_revision || '未核验' }}</ElDescriptionsItem>
                    <ElDescriptionsItem label="实际版本">{{ row.actual_revision || '未核验' }}</ElDescriptionsItem>
                    <ElDescriptionsItem v-if="row.detail" label="说明" :span="2">{{ row.detail }}</ElDescriptionsItem>
                    <ElDescriptionsItem v-if="equipmentDiff(row as WorkerEquipmentItem)" label="版本差异" :span="2">{{ equipmentDiff(row as WorkerEquipmentItem) }}</ElDescriptionsItem>
                  </ElDescriptions>
                </template>
              </ElTableColumn>
              <ElTableColumn label="组件" min-width="220">
                <template #default="{ row }">
                  <div class="equipment-cell">
                    <strong>{{ row.label }}</strong>
                    <ElText v-if="row.detail">{{ row.detail }}</ElText>
                  </div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="验证结果" width="110">
                <template #default="{ row }">
                  <SemanticTag :tone="equipmentTone(row.verified)" :label="equipmentResultLabel(row.verified)" />
                </template>
              </ElTableColumn>
              <ElTableColumn label="原因" min-width="240">
                <template #default="{ row }">
                  <ElText v-if="row.error" type="danger">{{ row.error }}</ElText>
                  <ElText v-else-if="row.verified === false" type="danger">验证未通过</ElText>
                  <ElText v-else-if="row.verified === null">尚未执行验证</ElText>
                  <ElText v-else>—</ElText>
                </template>
              </ElTableColumn>
            </ElTable>
            <ElAlert v-else show-icon :closable="false" title="尚未完成装备检查">
              执行“检查”后，这里会显示组件的验证结果与原因；配置与版本明细在组件行展开。
            </ElAlert>
            </ElCollapseItem>
          </ElCollapse>
        </section>
      </ElTabPane>
    </ElTabs>

    <EmployeeConfigModal
      :open="editOpen"
      :employee="employee"
      :creating="false"
      :digital-employees="digitalEmployees"
      :digital-loading="digitalLoading"
      @close="editOpen = false"
      @saved="onSaved"
    />
  </div>
  <div v-else-if="!store.loading" class="worker-detail">
    <ElEmpty :image-size="80" :description="store.error || '未找到该 Agent Worker，可能已被删除。'">
      <ElButton v-if="store.error" @click="refreshAll">重新加载</ElButton>
      <ElButton @click="router.push('/settings/workers')">返回 Agent Worker 列表</ElButton>
    </ElEmpty>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, RefreshCw, Settings2 } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import {
  ElAlert,
  ElButton,
  ElCard,
  ElCollapse,
  ElCollapseItem,
  ElDescriptions,
  ElDescriptionsItem,
  ElEmpty,
  ElPagination,
  ElSpace,
  ElTabPane,
  ElTable,
  ElTableColumn,
  ElTabs,
  ElText,
  vLoading,
} from 'element-plus'
import { listEmployees } from '@/api'
import { submitMaintenance } from '@/api/maintenance'
import type { Task } from '@/api/types'
import type { Employee as DigitalEmployee } from '@/types'
import type { WorkerEquipmentItem } from '@/api/maintenance'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import { useWorkerMaintenance } from '@/composables/useWorkerMaintenance'
import { notify } from '@/composables/useToast'
import { useAstraStore } from '@/stores/astra'
import {
  blockingReasons,
  connectionLabel,
  connectionTone,
  equipmentDiff,
  equipmentItems,
  equipmentStateLabel,
  equipmentTone,
  runtimeHealthLabel,
  runtimeHealthTone,
  schedulableLabel,
  schedulableTone,
  workerRepository,
} from '@/features/workers/presentation'
import { semanticTone } from '@/features/workers/semantic'
import { hasOccupancy, occupiedTaskIds } from '@/features/workers/actionPolicy'
import {
  employeeDisplayName,
  formatTaskDuration,
  formatTime,
  platformCopy,
  priorityLabel,
  statusTone,
  taskTypeLabel,
} from '@/utils/format'

type DetailSection = 'tasks' | 'config' | 'maintenance'
const SECTIONS: DetailSection[] = ['tasks', 'config', 'maintenance']
const LEGACY_SECTIONS: Record<string, DetailSection> = { overview: 'tasks', usage: 'tasks' }

const route = useRoute()
const router = useRouter()
const store = useAstraStore()
const id = computed(() => String(route.params.id))
const editOpen = ref(false)
const digitalEmployees = ref<DigitalEmployee[]>([])
const digitalLoading = ref(false)
const showAllBlockers = ref(false)

function sectionFromHash(hash: string): DetailSection {
  const name = hash.replace('#', '')
  if (LEGACY_SECTIONS[name]) return LEGACY_SECTIONS[name]
  return SECTIONS.includes(name as DetailSection) ? name as DetailSection : 'tasks'
}
const detailSection = ref<DetailSection>(sectionFromHash(route.hash))
watch(() => route.hash, (hash) => { detailSection.value = sectionFromHash(hash) })
watch(detailSection, (value) => {
  const next = `#${value}`
  if (route.hash === next) return
  void router.replace({ hash: next })
})

const employee = computed(() => store.employees.find((item) => item.id === id.value))
const employeeTasks = computed(() => store.tasks.filter(task => task.employee_id === id.value || task.worker_id === id.value))
const taskPage = ref(1)
const pagedTasks = computed(() => employeeTasks.value.slice((taskPage.value - 1) * 10, taskPage.value * 10))
watch(id, () => { taskPage.value = 1; showAllBlockers.value = false })
watch(() => employeeTasks.value.length, count => { taskPage.value = Math.min(taskPage.value, Math.max(1, Math.ceil(count / 10))) })
const environment = computed(() => employee.value?.execution_environment ?? null)
const maintenance = useWorkerMaintenance(computed(() => environment.value?.id ?? null))
const health = maintenance.health
const connection = computed(() => health.value?.connection.state ?? 'unknown')
const blocking = computed(() => blockingReasons(health.value))
const equipment = computed(() => equipmentItems(health.value))
const repository = computed(() => workerRepository(environment.value, health.value))
const currentTaskId = computed(() => occupiedTaskIds(health.value)[0] ?? null)
const currentTask = computed(() => store.tasks.find(task => task.id === currentTaskId.value))
const currentTaskEmptyText = computed(() => {
  if (!health.value) return '尚未获取任务占用情况，请刷新重试。'
  if (currentTaskId.value) return '当前占用任务的详情尚未加载。'
  if (hasOccupancy(health.value)) return '有未结束的执行会话，请在维护页检查。'
  return health.value.schedulable ? '当前没有占用任务，可接收匹配任务。' : '当前没有占用任务；恢复可接任务后再派单。'
})
const workerTitle = computed(() => environment.value?.name || employeeDisplayName(employee.value?.name))
const digitalEmployee = computed(() => digitalEmployees.value.find((item) => item.id === employee.value?.digital_employee_id) ?? null)
const taskTypesLabel = computed(() => (employee.value?.task_types ?? []).map((item) => taskTypeLabel(item)).join('、') || '—')

function categoryName(categoryId: string | null) {
  return store.categories.find((item) => item.id === categoryId)?.name ?? '—'
}
function openTask(task: Task) {
  router.push(`/tasks/${task.id}`)
}
function equipmentResultLabel(value: boolean | null) {
  if (value === true) return '通过'
  if (value === false) return '失败'
  return '未核验'
}
function dirtyLabel(value: boolean | null) {
  if (value === true) return '有未提交修改'
  if (value === false) return '工作区干净'
  return '未核验'
}
function channelLabel(value: string | null | undefined) {
  if (value === 'worker') return 'Worker 通道'
  if (value === 'ssh') return 'SSH'
  if (value === 'local') return '本机'
  return value || '—'
}

async function loadDigitalEmployees() {
  digitalLoading.value = true
  try { digitalEmployees.value = await listEmployees() }
  catch { digitalEmployees.value = [] }
  finally { digitalLoading.value = false }
}
async function refreshAll() {
  await Promise.all([store.refresh(), loadDigitalEmployees(), maintenance.load()])
}
async function load() {
  await Promise.all([store.refresh(), loadDigitalEmployees()])
}
onMounted(load)
async function onSaved(id: string, deployRequested: boolean) {
  await store.refresh()
  await loadDigitalEmployees()
  await maintenance.load()
  if (!deployRequested) return
  const saved = store.employees.find((item) => item.id === id)
  const environmentId = saved?.execution_environment?.id
  if (!environmentId) {
    notify('执行环境尚未创建，无法提交部署', 'error')
    return
  }
  try {
    const created = await submitMaintenance(environmentId, 'deploy')
    notify(`一键部署已提交：${created.id}`)
    await maintenance.load()
  } catch (error) {
    notify(error instanceof Error ? error.message : '一键部署提交失败', 'error')
  }
}
async function onMaintenanceChanged() {
  await store.refresh()
}
</script>

<style scoped>
.worker-detail { display: grid; gap: 14px; height: 100%; min-height: 480px; align-content: start; grid-auto-rows: max-content; padding: 20px 24px 24px; overflow: auto; }
.worker-hero { display: flex; align-items: center; gap: 16px; padding: 16px; border: 1px solid var(--astra-border); border-radius: 12px; background: var(--astra-bg-panel); box-shadow: var(--astra-shadow-panel); }
.worker-hero__identity { display: grid; gap: 3px; flex: 1; min-width: 0; }
.worker-hero__eyebrow { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.worker-hero__eyebrow > span:first-child { color: var(--astra-primary); font-weight: var(--astra-weight-bold); letter-spacing: .04em; }
.worker-hero__identity h2 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-subtitle); line-height: 1.25; }
.worker-hero__identity p { margin: 0; color: var(--astra-text-secondary); font-size: var(--astra-font-label); }
.worker-hero__badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.worker-hero__actions { flex: none; }
.worker-blocking { padding: 12px 16px; border: 1px solid var(--astra-border); border-radius: 8px; background: var(--astra-bg-panel); }
.worker-blocking__toggle { margin-top: 2px; }
.worker-detail :deep(.el-card) { --el-card-border-color: var(--astra-border); --el-card-border-radius: 11px; }
.worker-detail :deep(.el-card__header) { padding: 11px 15px; }
.worker-detail :deep(.el-card__body) { padding: 14px 15px; }
.worker-detail__tabs :deep(.el-tab-pane) { display: grid; gap: 14px; }
.worker-card__title { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; flex-wrap: wrap; }
.worker-card__title h3 { margin: 0; color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.worker-link, .worker-link:hover { display: inline-flex; align-items: center; gap: 4px; color: var(--astra-primary); font-size: var(--astra-font-label); text-decoration: none; }
.worker-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }
.worker-card--wide { grid-column: 1 / -1; }
.worker-detail :deep(.el-descriptions__cell) { min-width: 0; }
.worker-detail :deep(.el-descriptions__label) { white-space: nowrap; }
.worker-detail :deep(.el-descriptions__content) { overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
.current-task { display: grid; gap: 5px; }
.current-task small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.current-task strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.blocking-list { margin: 6px 0 0; padding-left: 18px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.7; }
.worker-current-empty { color: var(--astra-text-secondary); }
.worker-current-empty p { margin: 0; line-height: 1.6; }
.worker-task-pagination { margin-top: 12px; justify-content: flex-end; }
.worker-diagnostics { padding: 0 15px; border: 1px solid var(--astra-border); border-radius: 10px; }
.worker-diagnostic-status { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.worker-grid :deep(.el-table__row) { cursor: pointer; }
.task-cell { display: grid; min-width: 0; }
.task-cell strong, .task-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-cell strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.task-cell small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.equipment-cell { display: grid; gap: 2px; min-width: 0; }
.equipment-cell strong { font-size: var(--astra-font-label); }
.equipment-detail { padding: 4px 12px 8px 44px; }
@media (max-width: 1100px) { .worker-grid { grid-template-columns: 1fr; } .worker-card--wide { grid-column: auto; } }
@media (max-width: 720px) { .worker-hero { flex-wrap: wrap; } .worker-hero__actions { width: 100%; } .worker-detail { padding: 14px; } }
</style>
