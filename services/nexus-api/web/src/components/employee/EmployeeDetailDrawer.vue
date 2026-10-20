<template>
  <ElDrawer
    class="employee-detail-drawer"
    :model-value="Boolean(props.employee)"
    :title="model ? `${model.name} Worker 详情` : 'Worker 详情'"
    size="620px"
    @close="emit('close')"
  >
    <template #header>
      <div v-if="model" class="employee-detail-drawer__identity">
        <EmployeeAvatar :employee="model.employee" size="large" />
        <div class="employee-detail-drawer__summary">
          <small>{{ model.employee.id }}</small>
          <h2>{{ model.name }}</h2>
          <p>{{ model.employee.role }}</p>
          <div class="employee-detail-drawer__meta">
            <ElTag size="small" :type="statusTagType(model.employee.runtime_state)">{{ model.employee.runtime_status_label }}</ElTag>
            <ElText size="small" type="info">最近活动 {{ formatTime(model.lastActiveAt) }}</ElText>
          </div>
        </div>
      </div>
    </template>

    <div v-if="model" class="employee-detail-drawer__sections">
      <ElCard shadow="never">
        <template #header>
          <div class="employee-detail-drawer__card-header">
            <h3>运行状态</h3>
            <RouterLink v-if="model.currentTask" class="employee-detail-drawer__link" :to="`/tasks/${model.currentTask.id}`">进入任务详情 <ArrowUpRight :size="14" /></RouterLink>
          </div>
        </template>
        <div v-if="model.currentTask" class="employee-detail-drawer__current-task">
          <strong>{{ model.currentTask.title }}</strong>
          <ElText v-if="platformCopy(model.currentTask.current_progress)" size="small" type="info">{{ platformCopy(model.currentTask.current_progress) }}</ElText>
          <ElDescriptions :column="2" border size="small">
            <ElDescriptionsItem label="执行状态"><ElTag size="small" :type="statusTagType(model.currentTask.runtime_state)">{{ model.currentTask.runtime_status_label }}</ElTag></ElDescriptionsItem>
            <ElDescriptionsItem label="开始时间">{{ formatTime(model.currentTask.started_at) }}</ElDescriptionsItem>
          </ElDescriptions>
        </div>
        <ElText v-else size="small" type="info">当前无执行任务</ElText>
      </ElCard>

      <ElCard shadow="never">
        <template #header><h3>基本信息</h3></template>
        <ElDescriptions :column="2" border size="small">
          <ElDescriptionsItem label="Worker 编号">{{ model.employee.id }}</ElDescriptionsItem>
          <ElDescriptionsItem label="岗位">{{ model.employee.role }}</ElDescriptionsItem>
          <ElDescriptionsItem label="所在区域">{{ model.areaName }}</ElDescriptionsItem>
          <ElDescriptionsItem label="启用状态"><ElTag size="small" :type="model.employee.enabled ? 'success' : 'info'" :effect="model.employee.enabled ? 'light' : 'plain'">{{ model.employee.enabled ? '已启用' : '已停用' }}</ElTag></ElDescriptionsItem>
          <ElDescriptionsItem label="职责" :span="2">{{ model.employee.description || model.employee.role }}</ElDescriptionsItem>
        </ElDescriptions>
      </ElCard>

      <ElCard shadow="never">
        <template #header><h3>工作配置</h3></template>
        <ElDescriptions :column="2" border size="small">
          <ElDescriptionsItem label="执行方式">{{ environmentLabel(model.employee.execution_environment) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="Agent / 模型">{{ model.employee.execution_environment?.model_name || '使用默认配置' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="工作环境">{{ model.employee.execution_environment?.name || '未配置' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="自动领取">{{ model.employee.auto_claim ? '已开启' : '已关闭' }}</ElDescriptionsItem>
          <ElDescriptionsItem label="自动执行">{{ model.employee.auto_execute ? '已开启' : '已关闭' }}</ElDescriptionsItem>
        </ElDescriptions>
      </ElCard>

      <ElCard shadow="never">
        <template #header>
          <div class="employee-detail-drawer__card-header">
            <h3>最近任务</h3>
            <RouterLink class="employee-detail-drawer__link" :to="`/workers/${model.employee.id}#tasks`">查看全部任务 <ArrowUpRight :size="14" /></RouterLink>
          </div>
        </template>
        <ElTable v-if="recentTasks.length" :data="recentTasks" size="small">
          <ElTableColumn label="任务" min-width="200">
            <template #default="{ row }"><RouterLink class="employee-detail-drawer__link" :to="`/tasks/${row.id}`">{{ row.title }}</RouterLink></template>
          </ElTableColumn>
          <ElTableColumn label="最近更新" width="110">
            <template #default="{ row }"><ElText size="small" type="info">{{ formatTime(row.updated_at) }}</ElText></template>
          </ElTableColumn>
          <ElTableColumn label="状态" width="110">
            <template #default="{ row }"><ElTag size="small" :type="statusTagType(row.runtime_state)">{{ row.runtime_status_label }}</ElTag></template>
          </ElTableColumn>
        </ElTable>
        <ElText v-else size="small" type="info">暂无任务记录</ElText>
      </ElCard>
    </div>

    <template #footer>
      <ElButton v-if="model" type="primary" :icon="Settings2" @click="onEdit">编辑</ElButton>
    </template>
  </ElDrawer>
</template>

<script setup lang="ts">
import { ArrowUpRight, Settings2 } from 'lucide-vue-next'
import { computed, ref, watch } from 'vue'
import { RouterLink } from 'vue-router'
import { ElButton, ElCard, ElDescriptions, ElDescriptionsItem, ElDrawer, ElTable, ElTableColumn, ElTag, ElText } from 'element-plus'
import type { Employee, Task } from '@/api/types'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import { toEmployeeViewModel } from '@/features/employees/presentation'
import { environmentLabel, formatTime, platformCopy, statusTagType } from '@/utils/format'

const props = defineProps<{ employee: Employee | null; tasks: Task[] }>()
const emit = defineEmits<{ close: []; edit: [employee: Employee] }>()
const cachedEmployee = ref<Employee | null>(props.employee)
watch(() => props.employee, (employee) => { if (employee) cachedEmployee.value = employee })
const model = computed(() => cachedEmployee.value ? toEmployeeViewModel(cachedEmployee.value, props.tasks) : null)
const recentTasks = computed(() => cachedEmployee.value
  ? props.tasks.filter((task) => task.employee_id === cachedEmployee.value?.id).sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 3)
  : [])
function onEdit() { if (cachedEmployee.value) emit('edit', cachedEmployee.value) }
</script>

<style scoped>
.employee-detail-drawer :deep(.el-drawer__header) { align-items: flex-start; margin-bottom: 18px; padding-top: 18px; }
.employee-detail-drawer :deep(.el-drawer__body) { padding-top: 0; }
.employee-detail-drawer :deep(.el-card) { --el-card-border-color: var(--astra-border); --el-card-border-radius: 11px; }
.employee-detail-drawer :deep(.el-card__header) { padding: 11px 15px; }
.employee-detail-drawer :deep(.el-card__body) { padding: 14px 15px; }
.employee-detail-drawer__identity { display: flex; align-items: flex-start; gap: 14px; min-width: 0; }
.employee-detail-drawer__summary { min-width: 0; }
.employee-detail-drawer__summary small { color: var(--astra-primary); font-size: var(--astra-font-helper); letter-spacing: .08em; }
.employee-detail-drawer__summary h2 { overflow: hidden; margin: 1px 0; font-size: var(--astra-font-section); line-height: normal; text-overflow: ellipsis; white-space: nowrap; }
.employee-detail-drawer__summary p { overflow: hidden; margin: 0; color: var(--astra-text-secondary); text-overflow: ellipsis; white-space: nowrap; }
.employee-detail-drawer__meta { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 12px; margin-top: 8px; }
.employee-detail-drawer__sections { display: grid; gap: 14px; }
.employee-detail-drawer__card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.employee-detail-drawer__card-header h3, .employee-detail-drawer :deep(.el-card__header > h3) { margin: 0; font-size: var(--astra-font-label); line-height: normal; }
.employee-detail-drawer__link, .employee-detail-drawer__link:hover { color: var(--astra-primary); font-size: var(--astra-font-label); }
.employee-detail-drawer__current-task > strong { display: block; }
.employee-detail-drawer__current-task > span { margin: 4px 0 12px; }
.employee-detail-drawer__current-task :deep(.el-descriptions) { margin-top: 12px; }
@media (max-width: 600px) { .employee-detail-drawer :deep(.el-drawer__body), .employee-detail-drawer :deep(.el-drawer__header), .employee-detail-drawer :deep(.el-drawer__footer) { padding-inline: 16px; } }
</style>
