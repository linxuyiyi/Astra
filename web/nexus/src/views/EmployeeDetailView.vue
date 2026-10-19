<template>
  <div v-if="employee" class="employee-detail" data-testid="employee-detail-page">
    <ElBreadcrumb separator="/" class="employee-detail__nav">
      <ElBreadcrumbItem><RouterLink to="/workers">Worker</RouterLink></ElBreadcrumbItem>
      <ElBreadcrumbItem>{{ environment?.name || employeeDisplayName(employee.name) }}</ElBreadcrumbItem>
    </ElBreadcrumb>

    <ElCard shadow="never">
      <div class="employee-detail__hero">
        <EmployeeAvatar :employee="employee" size="large" />
        <div class="employee-detail__identity">
          <div class="employee-detail__identity-top">
            <small>环境资源 #{{ environment?.id ?? '—' }} · 关联员工 {{ employee.id }}</small>
            <ElTag size="small" :type="health?.enabled === false ? 'info' : health?.enabled === true ? 'success' : 'info'" effect="plain">{{ health ? (health.enabled ? 'Worker 已启用' : 'Worker 已禁用') : 'Worker 启用状态未获取' }}</ElTag>
            <ElTag v-if="employee.enabled === false" size="small" type="info" effect="plain">平台调度已停用</ElTag>
          </div>
          <h2>{{ environment?.name || employeeDisplayName(employee.name) }}</h2>
          <ElText size="small" type="info">{{ employeeDisplayName(employee.name) }} · {{ employee.role }}</ElText>
          <div class="employee-detail__badges">
            <ElTag size="small" :type="connectionTone(connection)">连接 {{ connectionLabel(connection) }}</ElTag>
            <ElTag size="small" :type="runtimeHealthTone(health)">OpenCode {{ runtimeHealthLabel(health) }}</ElTag>
            <ElTag v-if="maintenanceState" size="small" :type="maintenanceStateTone(maintenanceState)">维护 {{ maintenanceStateLabel(maintenanceState) }}</ElTag>
            <ElTag size="small" :type="schedulableTone(health)">{{ schedulableLabel(health) }}</ElTag>
          </div>
        </div>
        <ElSpace :size="10">
          <ElButton :icon="RefreshCw" :loading="maintenance.loading.value" @click="maintenance.load">刷新</ElButton>
          <ElButton :icon="Settings2" @click="editOpen = true">编辑</ElButton>
        </ElSpace>
      </div>
    </ElCard>

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

    <ElTabs v-model="detailSection" class="employee-detail__tabs">
      <ElTabPane label="运行概览" name="overview">
        <section class="employee-detail__panels employee-detail__panels--overview">
          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>基础环境</h3><ElText size="small" type="info">Worker 作为可部署执行资源的身份与接入信息</ElText></div>
            </template>
            <ElDescriptions v-if="environment" :column="2" border size="small">
              <ElDescriptionsItem label="环境名称">{{ environment.name || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="环境资源 ID">#{{ environment.id }}</ElDescriptionsItem>
              <ElDescriptionsItem label="关联员工">{{ employeeDisplayName(employee.name) }}（{{ employee.id }}）</ElDescriptionsItem>
              <ElDescriptionsItem label="执行方式">{{ environmentLabel(environment) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="服务器">{{ serverAddress(environment) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作目录" :span="2">{{ environment.workspace_root || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="OpenCode 可执行文件" :span="2">{{ environment.opencode_path || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="OpenCode 端口">{{ environment.opencode_server_port }}</ElDescriptionsItem>
              <ElDescriptionsItem label="模型">{{ environment.model_name || '使用默认配置' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="SSH 凭据">{{ environment.has_ssh_password ? '已配置（不回显）' : '未配置' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="最近环境检查" :span="2">{{ formatTime(environment.last_checked_at) }}</ElDescriptionsItem>
            </ElDescriptions>
            <ElText v-else size="small" type="info">尚未配置执行环境，可在“编辑”中添加。</ElText>
          </ElCard>

          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>代码仓库</h3><ElText size="small" type="info">目标工程仓库与最近部署记录</ElText></div>
            </template>
            <ElDescriptions v-if="repository.configured" :column="2" border size="small">
              <ElDescriptionsItem label="仓库地址" :span="2">{{ repository.url || `仓库 ${repository.id ?? '—'}` }}</ElDescriptionsItem>
              <ElDescriptionsItem label="目标分支">{{ repository.targetBranch || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="当前分支">{{ repository.branch || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="仓库 ID">{{ repository.id || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作目录" :span="2">{{ environment?.workspace_root || '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="当前提交">{{ repository.commit || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工作区">{{ dirtyLabel(repository.dirty) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="认证">{{ repository.authentication || '未核验' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="失败原因" :span="2">{{ repository.error || '—' }}</ElDescriptionsItem>
            </ElDescriptions>
            <ElEmpty v-else-if="!health" :image-size="54" description="尚未获取仓库状态。" />
            <ElEmpty v-else :image-size="54" description="未绑定代码仓库；该 Worker 不执行仓库任务。" />
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-header">
                <div class="employee-detail__card-title"><h3>装备配置</h3><ElText size="small" type="info">配置、部署、验证分开显示；文件存在或端口监听不等于验证成功</ElText></div>
                <ElButton size="small" :icon="RefreshCw" :loading="maintenance.loading.value" @click="maintenance.load">刷新装备</ElButton>
              </div>
            </template>
            <ElTable v-if="equipment.length" :data="equipment" size="small">
              <ElTableColumn label="装备" min-width="190">
                <template #default="{ row }">
                  <div class="employee-detail__equipment-cell">
                    <strong>{{ row.label }}</strong>
                    <ElText v-if="row.detail" type="info">{{ row.detail }}</ElText>
                  </div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="已配置" width="100">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.configured)">{{ equipmentStateLabel(row.configured) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="已部署" width="100">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.deployed)">{{ equipmentStateLabel(row.deployed) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="已验证" width="100">
                <template #default="{ row }"><ElTag size="small" :type="equipmentTone(row.verified)">{{ equipmentStateLabel(row.verified) }}</ElTag></template>
              </ElTableColumn>
              <ElTableColumn label="目标版本" min-width="130"><template #default="{ row }">{{ row.desired_revision || '未核验' }}</template></ElTableColumn>
              <ElTableColumn label="实际版本" min-width="130"><template #default="{ row }">{{ row.actual_revision || '未核验' }}</template></ElTableColumn>
              <ElTableColumn label="差异 / 失败" min-width="220">
                <template #default="{ row }">
                  <ElText v-if="row.error" type="danger">{{ row.error }}</ElText>
                  <ElText v-else-if="equipmentDiff(row as WorkerEquipmentItem)" type="warning">{{ equipmentDiff(row as WorkerEquipmentItem) }}</ElText>
                  <ElText v-else-if="row.verified === false" type="danger">验证未通过</ElText>
                  <ElText v-else type="info">—</ElText>
                </template>
              </ElTableColumn>
            </ElTable>
            <ElAlert
              v-else
              type="info"
              show-icon
              :closable="false"
              title="尚未完成装备检查"
            >
              执行“检查”后，这里会显示装备的配置、部署、验证状态及版本差异。
            </ElAlert>
          </ElCard>

          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>运行状态</h3><ElText size="small" type="info">连接、运行时健康、可调度性与当前任务分别计算</ElText></div>
            </template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="连接通道">{{ channelLabel(health?.connection?.channel) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="连接状态"><ElTag size="small" :type="connectionTone(connection)">{{ connectionLabel(connection) }}</ElTag></ElDescriptionsItem>
              <ElDescriptionsItem label="最后心跳">{{ formatTime(health?.connection?.last_heartbeat_at) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="OpenCode 健康"><ElTag size="small" :type="runtimeHealthTone(health)">{{ runtimeHealthLabel(health) }}</ElTag></ElDescriptionsItem>
              <ElDescriptionsItem label="健康检查时间">{{ formatTime(health?.runtime_health?.last_checked_at) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="可调度性"><ElTag size="small" :type="schedulableTone(health)">{{ schedulableLabel(health) }}</ElTag></ElDescriptionsItem>
              <ElDescriptionsItem label="维护状态"><ElTag size="small" :type="maintenanceStateTone(maintenanceState)">{{ maintenanceStateLabel(maintenanceState) }}</ElTag></ElDescriptionsItem>
              <ElDescriptionsItem label="最近维护阶段">{{ maintenanceStageLabel(health?.maintenance?.stage) }}</ElDescriptionsItem>
            </ElDescriptions>
            <div class="employee-detail__blocking">
              <ElText size="small" type="info">阻塞对象</ElText>
              <ul v-if="blocking.length">
                <li v-for="reason in blocking" :key="reason">{{ reason }}</li>
              </ul>
              <ElText v-else size="small" type="info">当前没有阻塞项</ElText>
            </div>
            <div class="employee-detail__current-task">
              <template v-if="currentTask">
                <small>{{ currentTask.id }}</small>
                <RouterLink class="employee-detail__link" :to="`/tasks/${currentTask.id}`">
                  <strong>{{ currentTask.title }}</strong>
                  <ArrowUpRight :size="13" />
                </RouterLink>
                <ElSpace :size="8">
                  <ElTag size="small" :type="statusTagType(currentTask.runtime_state)">{{ currentTask.runtime_status_label }}</ElTag>
                  <ElTag v-if="waitingTask" size="small" type="warning">等待人工确认</ElTag>
                </ElSpace>
              </template>
              <template v-else>
                <strong>当前无执行任务</strong>
                <ElText size="small" type="info">{{ idleHint }}</ElText>
              </template>
            </div>
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-title"><h3>维护记录</h3><ElText size="small" type="info">操作独立持久化；离开页面不取消服务端任务，取消必须显式请求</ElText></div>
            </template>
            <WorkerMaintenancePanel
              :controller="maintenance"
              @changed="onMaintenanceChanged"
            />
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="任务" name="tasks">
        <section id="tasks" class="employee-detail__panels">
          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-header">
                <div class="employee-detail__card-title"><h3>当前工作</h3><ElText size="small" type="info">任务状态、实时进展和运行时长</ElText></div>
                <RouterLink v-if="currentTask" class="employee-detail__link" :to="`/tasks/${currentTask.id}`">查看任务 <ArrowUpRight :size="13" /></RouterLink>
              </div>
            </template>
            <div v-if="currentTask">
              <div class="employee-detail__current-task">
                <small>{{ currentTask.id }}</small>
                <strong>{{ currentTask.title }}</strong>
              </div>
              <ElDescriptions :column="3" border size="small">
                <ElDescriptionsItem label="类型">{{ taskTypeLabel(currentTask.task_type) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="分类">{{ categoryName(currentTask.category_id) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="优先级">{{ priorityLabel(currentTask.priority) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="开始时间">{{ formatTime(currentTask.started_at) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前状态"><ElTag size="small" :type="statusTagType(currentTask.runtime_state)">{{ currentTask.runtime_status_label }}</ElTag></ElDescriptionsItem>
                <ElDescriptionsItem label="运行时长">{{ formatTaskDuration(currentTask) }}</ElDescriptionsItem>
                <ElDescriptionsItem label="当前进展" :span="3">{{ platformCopy(currentTask.current_progress) || '暂无进展更新' }}</ElDescriptionsItem>
                <ElDescriptionsItem label="执行会话" :span="3">{{ currentTask.current_session_id ?? '执行会话尚未创建' }}</ElDescriptionsItem>
              </ElDescriptions>
            </div>
            <ElEmpty v-else :image-size="60" description="当前空闲，Worker 正在等待下一项匹配任务。" />
            <ElAlert v-if="latestError" class="employee-detail__inline-alert" type="warning" show-icon :closable="false" :title="latestErrorCopy" />
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-title"><h3>Worker 任务队列</h3><ElText size="small" type="info">当前任务优先，其后按优先级和创建时间</ElText></div>
            </template>
            <ElTable :data="employeeQueue" size="small" @row-click="openTask">
              <ElTableColumn label="顺序" width="70" align="right"><template #default="{ $index }">{{ $index + 1 }}</template></ElTableColumn>
              <ElTableColumn label="任务" min-width="240">
                <template #default="{ row }">
                  <div class="employee-detail__task-cell"><strong>{{ row.title }}</strong><small>{{ row.id }}</small></div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="优先级" width="90"><template #default="{ row }">{{ priorityLabel(row.priority) }}</template></ElTableColumn>
              <ElTableColumn label="状态" width="120"><template #default="{ row }"><ElTag size="small" :type="statusTagType(row.runtime_state)">{{ row.runtime_status_label }}</ElTag></template></ElTableColumn>
              <ElTableColumn label="当前进展" min-width="220" show-overflow-tooltip><template #default="{ row }">{{ platformCopy(row.current_progress) }}</template></ElTableColumn>
              <template #empty><ElText size="small" type="info">当前队列为空</ElText></template>
            </ElTable>
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-title"><h3>历史任务</h3><ElText size="small" type="info">已完成、未完成和已取消任务的消耗均会保留</ElText></div>
            </template>
            <ElTable :data="historicalTasks" size="small" @row-click="openTask">
              <ElTableColumn label="任务" min-width="240">
                <template #default="{ row }">
                  <div class="employee-detail__task-cell"><strong>{{ row.title }}</strong><small>{{ row.id }}</small></div>
                </template>
              </ElTableColumn>
              <ElTableColumn label="类型" width="80"><template #default="{ row }">{{ taskTypeLabel(row.task_type) }}</template></ElTableColumn>
              <ElTableColumn label="分类" width="140" show-overflow-tooltip><template #default="{ row }">{{ categoryName(row.category_id) }}</template></ElTableColumn>
              <ElTableColumn label="优先级" width="90"><template #default="{ row }">{{ priorityLabel(row.priority) }}</template></ElTableColumn>
              <ElTableColumn label="状态" width="120"><template #default="{ row }"><ElTag size="small" :type="statusTagType(row.runtime_state)">{{ row.runtime_status_label }}</ElTag></template></ElTableColumn>
              <ElTableColumn label="运行时长" width="130"><template #default="{ row }">{{ formatTaskDuration(row as Task) }}</template></ElTableColumn>
              <template #empty><ElText size="small" type="info">暂无历史任务</ElText></template>
            </ElTable>
          </ElCard>
        </section>
      </ElTabPane>

      <ElTabPane label="用量" name="usage">
        <section class="employee-detail__metrics" aria-label="今日工作摘要">
          <ElCard v-for="metric in metrics" :key="metric.label" shadow="never">
            <ElStatistic :title="metric.label" :value="metric.value" :formatter="metric.formatter" />
            <p class="employee-detail__metric-hint">{{ metric.hint }}</p>
          </ElCard>
        </section>

        <ElAlert v-if="usageError" type="error" show-icon :closable="false" :title="usageError">
          <ElButton size="small" type="primary" @click="loadUsage">重试</ElButton>
        </ElAlert>

        <section class="employee-detail__panels">
          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>今日工作消耗</h3><ElText size="small" type="info">实际用量，缺失数据不估算</ElText></div>
            </template>
            <ElDescriptions :column="4" border size="small">
              <ElDescriptionsItem label="输入 Token">{{ formatTokens(employee.today_usage.input_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="输出 Token">{{ formatTokens(employee.today_usage.output_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="缓存读取">{{ formatTokens(employee.today_usage.cache_read_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="缓存写入">{{ formatTokens(employee.today_usage.cache_write_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="Token 总计">{{ formatTokens(employee.today_usage.total_tokens) }}</ElDescriptionsItem>
              <ElDescriptionsItem label="工具调用">{{ employee.today_usage.tool_call_count ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平均每任务 Token">{{ averageTaskTokens }}</ElDescriptionsItem>
              <ElDescriptionsItem label="平均每任务工具调用">{{ averageTaskTools }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-header">
                <div class="employee-detail__card-title"><h3>Token 趋势</h3><ElText size="small" type="info">输入、输出与缓存读取</ElText></div>
                <ElRadioGroup v-model="days" size="small" @change="loadUsage">
                  <ElRadioButton :value="7">近 7 天</ElRadioButton>
                  <ElRadioButton :value="30">近 30 天</ElRadioButton>
                </ElRadioGroup>
              </div>
            </template>
            <div class="employee-detail__chart"><UsageTrendChart :data="usage?.trend ?? []" /></div>
          </ElCard>

          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>工具调用概况</h3><ElText size="small" type="info">近 {{ days }} 天</ElText></div>
            </template>
            <ElDescriptions :column="2" border size="small">
              <ElDescriptionsItem label="工具调用总数">{{ usage?.summary.tool_call_count ?? '—' }}</ElDescriptionsItem>
              <ElDescriptionsItem label="成功次数">{{ usage?.summary.tool_success_count ?? 0 }}</ElDescriptionsItem>
              <ElDescriptionsItem label="失败次数">{{ usage?.summary.tool_failure_count ?? 0 }}</ElDescriptionsItem>
              <ElDescriptionsItem label="成功率">{{ toolSuccessRate }}</ElDescriptionsItem>
            </ElDescriptions>
          </ElCard>

          <ElCard shadow="never">
            <template #header>
              <div class="employee-detail__card-title"><h3>模型调用统计</h3><ElText size="small" type="info">近 {{ days }} 天各模型实际用量</ElText></div>
            </template>
            <ElTable :data="usage?.models ?? []" size="small">
              <ElTableColumn label="模型" prop="model_name" min-width="170" show-overflow-tooltip />
              <ElTableColumn label="会话数" prop="session_count" width="80" align="right" />
              <ElTableColumn label="Token" width="100" align="right"><template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template></ElTableColumn>
              <ElTableColumn label="LLM 调用" prop="llm_call_count" width="90" align="right" />
              <ElTableColumn label="工具调用" prop="tool_call_count" width="90" align="right" />
              <template #empty><ElEmpty :image-size="56" description="暂无模型用量数据" /></template>
            </ElTable>
          </ElCard>

          <ElCard shadow="never" class="employee-detail__panel--wide">
            <template #header>
              <div class="employee-detail__card-title"><h3>工具调用 Top</h3><ElText size="small" type="info">近 {{ days }} 天实际工具事件</ElText></div>
            </template>
            <ElTable :data="usage?.tools.slice(0, 8) ?? []" size="small">
              <ElTableColumn label="工具" prop="tool_name" min-width="170" show-overflow-tooltip />
              <ElTableColumn label="成功" prop="success_count" width="90" align="right" />
              <ElTableColumn label="失败" prop="failure_count" width="90" align="right" />
              <ElTableColumn label="调用次数" prop="call_count" width="100" align="right" />
              <template #empty><ElEmpty :image-size="56" description="暂无工具数据，Agent 返回工具事件后会在此汇总。" /></template>
            </ElTable>
          </ElCard>
        </section>
      </ElTabPane>
    </ElTabs>

    <EmployeeConfigModal :open="editOpen" :employee="employee" :creating="false" :categories="store.categories" @close="editOpen = false" @saved="onSaved" />
  </div>
  <div v-else-if="!store.loading" class="employee-detail">
    <ElEmpty :image-size="80" description="未找到该 Worker，可能已被删除。">
      <ElButton @click="router.push('/workers')">返回 Worker 列表</ElButton>
    </ElEmpty>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpRight, RefreshCw, Settings2 } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElBreadcrumb, ElBreadcrumbItem, ElButton, ElCard, ElDescriptions, ElDescriptionsItem, ElEmpty, ElRadioButton, ElRadioGroup, ElSpace, ElStatistic, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag, ElText } from 'element-plus'
import { api } from '@/api/client'
import type { EmployeeUsage, Task } from '@/api/types'
import type { WorkerEquipmentItem } from '@/api/maintenance'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import UsageTrendChart from '@/components/runtime/UsageTrendChart.vue'
import WorkerMaintenancePanel from '@/components/worker/WorkerMaintenancePanel.vue'
import { useWorkerMaintenance } from '@/composables/useWorkerMaintenance'
import { useAstraStore } from '@/stores/astra'
import {
  blockingReasons,
  connectionLabel,
  connectionTone,
  equipmentDiff,
  equipmentItems,
  equipmentStateLabel,
  maintenanceStageLabel,
  isMaintenanceActive,
  maintenanceStateLabel,
  maintenanceStateTone,
  runtimeHealthLabel,
  runtimeHealthTone,
  schedulableLabel,
  schedulableTone,
  serverAddress,
  workerRepository,
} from '@/features/workers/presentation'
import { employeeDisplayName, environmentLabel, formatDurationSeconds, formatTaskDuration, formatTime, formatTokens, platformCopy, priorityLabel, statusTagType, taskTypeLabel } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const store = useAstraStore()
const id = computed(() => String(route.params.id))
const usage = ref<EmployeeUsage | null>(null)
const usageError = ref('')
const days = ref(7)
const editOpen = ref(false)
type DetailSection = 'overview' | 'tasks' | 'usage'
function sectionFromHash(hash: string): DetailSection {
  const name = hash.replace('#', '')
  return name === 'usage' || name === 'tasks' ? name : 'overview'
}
const detailSection = ref<DetailSection>(sectionFromHash(route.hash))
watch(() => route.hash, (hash) => { detailSection.value = sectionFromHash(hash) })
watch(detailSection, (value) => {
  const next = `#${value}`
  if (route.hash === next) return
  void router.replace({ hash: next })
})

const ACTIVE_STATUSES = ['PENDING', 'RUNNING', 'WAITING', 'FAILED']
const PRIORITY_WEIGHT: Record<string, number> = { VERY_HIGH: 4, HIGH: 3, MEDIUM: 2, LOW: 1 }

const employee = computed(() => store.employees.find((item) => item.id === id.value))
const employeeTasks = computed(() => store.tasks.filter((task) => task.employee_id === id.value))
const environment = computed(() => employee.value?.execution_environment ?? null)
const maintenance = useWorkerMaintenance(computed(() => environment.value?.id ?? null))
const health = maintenance.health
const maintenanceState = computed(() => health.value?.maintenance?.state ?? null)
const connection = computed(() => health.value?.connection.state ?? 'unknown')
const blocking = computed(() => blockingReasons(health.value))
const maintenanceActive = computed(() => isMaintenanceActive(health.value?.maintenance?.state) || isMaintenanceActive(maintenance.operation.value?.state))
const idleHint = computed(() => {
  if (maintenanceActive.value) return '当前无执行任务，但有进行中的维护操作，部分维护入口受限。'
  if (health.value?.maintenance?.state === 'INTERRUPTED') return '当前无执行任务，维护操作待核对，请先检查或恢复。'
  if (!health.value) return '尚未获取 Worker 运行状态；部署、更新和重启仍可提交，是否安全由后端最终检查。'
  return '当前无执行任务，Worker 空闲，可执行维护操作。'
})
const equipment = computed(() => equipmentItems(health.value))
const repository = computed(() => workerRepository(environment.value, health.value))
const currentTask = computed(() => employeeTasks.value.find((task) => ACTIVE_STATUSES.includes(task.status)))
const waitingTask = computed(() => employeeTasks.value.find((task) => task.status === 'WAITING'))
const employeeQueue = computed(() => employeeTasks.value
  .filter((task) => ACTIVE_STATUSES.includes(task.status))
  .sort((a, b) => (PRIORITY_WEIGHT[b.priority] ?? 0) - (PRIORITY_WEIGHT[a.priority] ?? 0) || a.created_at.localeCompare(b.created_at)))
const historicalTasks = computed(() => employeeTasks.value.filter((task) => ['SUCCEEDED', 'FAILED', 'CANCELED'].includes(task.status)))
const latestError = computed(() => employeeTasks.value.filter((task) => task.execution_error).sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0])
const latestErrorCopy = computed(() => latestError.value
  ? `最近异常：${platformCopy(String(latestError.value.execution_error?.reason ?? latestError.value.current_progress))} · ${formatTime(latestError.value.updated_at)}`
  : '')
interface DetailMetric {
  label: string
  value: number
  hint: string
  formatter: () => string
}
const metrics = computed<DetailMetric[]>(() => {
  const today = employee.value?.today_usage
  return [
    { label: '今日 Token', value: today?.total_tokens ?? 0, hint: 'Agent 实际用量', formatter: () => formatTokens(today?.total_tokens) },
    { label: '工具调用', value: today?.tool_call_count ?? 0, hint: '成功与失败均保留', formatter: () => today?.tool_call_count == null ? '—' : `${today.tool_call_count} 次` },
    { label: '今日完成', value: employee.value?.today_completed ?? 0, hint: 'Worker 已提交结果', formatter: () => employee.value?.today_completed == null ? '—' : `${employee.value.today_completed} 项` },
    { label: '处理中', value: statusCount('RUNNING'), hint: '当前工作', formatter: () => `${statusCount('RUNNING')} 项` },
    { label: '等待人工', value: statusCount('WAITING'), hint: '需要补充信息', formatter: () => `${statusCount('WAITING')} 项` },
    { label: '今日工作时长', value: today?.duration_seconds ?? 0, hint: '按任务开始到结束计算', formatter: () => formatDurationSeconds(today?.duration_seconds) },
  ]
})
const averageTaskTokens = computed(() => {
  const today = employee.value?.today_usage
  return today?.total_tokens != null && today.task_count ? formatTokens(Math.round(today.total_tokens / today.task_count)) : '—'
})
const averageTaskTools = computed(() => {
  const today = employee.value?.today_usage
  return today?.tool_call_count != null && today.task_count ? (today.tool_call_count / today.task_count).toFixed(1) : '—'
})
const toolSuccessRate = computed(() => {
  const summary = usage.value?.summary
  if (!summary?.tool_call_count) return '—'
  return `${((summary.tool_success_count / summary.tool_call_count) * 100).toFixed(1)}%`
})

function categoryName(categoryId: string | null) { return store.categories.find((item) => item.id === categoryId)?.name ?? '—' }
function statusCount(status: string) { return employeeTasks.value.filter((task) => task.status === status).length }
function openTask(task: Task) { router.push(`/tasks/${task.id}`) }
function equipmentTone(value: boolean | null) {
  if (value === true) return 'success'
  if (value === false) return 'danger'
  return 'info'
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

async function loadUsage() {
  usageError.value = ''
  try { usage.value = await api.employeeUsage(id.value, days.value) }
  catch (error) { usageError.value = error instanceof Error ? error.message : '用量数据加载失败' }
}
async function load() { await store.refresh(); await loadUsage() }
onMounted(load)
async function onSaved() { await store.refresh() }
async function onMaintenanceChanged() { await store.refresh() }
</script>

<style scoped>
.employee-detail { display: grid; gap: 14px; height: 100%; min-height: 480px; padding: 20px 24px 24px; overflow: auto; }
.employee-detail__tabs :deep(.el-tab-pane) { display: grid; gap: 14px; }
.employee-detail__nav { font-size: var(--astra-font-label); }
.employee-detail__nav :deep(.el-breadcrumb__inner a) { color: var(--astra-text-muted); }
.employee-detail__nav :deep(.el-breadcrumb__inner.is-link a:hover), .employee-detail__nav :deep(.el-breadcrumb__inner.is-link):hover a { color: var(--astra-primary); }
.employee-detail__hero { display: flex; align-items: center; gap: 16px; }
.employee-detail__identity { display: grid; gap: 2px; flex: 1; min-width: 0; }
.employee-detail__identity-top { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.employee-detail__identity-top small { color: var(--astra-primary); font-size: var(--astra-font-helper); letter-spacing: .04em; }
.employee-detail__identity h2 { margin: 0; font-size: var(--astra-font-section); line-height: normal; }
.employee-detail__badges { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.employee-detail :deep(.el-card) { --el-card-border-color: var(--astra-border); --el-card-border-radius: 11px; }
.employee-detail :deep(.el-card__header) { padding: 11px 15px; }
.employee-detail :deep(.el-card__body) { padding: 14px 15px; }
.employee-detail__card-header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.employee-detail__card-title { display: grid; gap: 2px; min-width: 0; }
.employee-detail__card-title h3 { margin: 0; font-size: var(--astra-font-label); line-height: normal; }
.employee-detail__metrics { display: grid; grid-template-columns: repeat(6, minmax(0, 1fr)); gap: 12px; }
.employee-detail__metrics :deep(.el-statistic__head) { color: var(--astra-text-muted); font-size: var(--astra-font-control); }
.employee-detail__metrics :deep(.el-statistic__content) { color: var(--astra-text-primary); font-size: var(--astra-font-subtitle); font-weight: var(--astra-weight-bold); }
.employee-detail__metric-hint { margin: 4px 0 0; color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.employee-detail__panels { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; align-items: start; }
.employee-detail__panels--overview { grid-template-columns: minmax(0, 1fr); }
.employee-detail :deep(.el-descriptions__cell) { min-width: 0; }
.employee-detail :deep(.el-descriptions__label) { white-space: nowrap; }
.employee-detail :deep(.el-descriptions__content) { overflow-wrap: anywhere; word-break: break-word; white-space: normal; }
.employee-detail__panel--wide { grid-column: 1 / -1; }
.employee-detail__link, .employee-detail__link:hover { color: var(--astra-primary); font-size: var(--astra-font-label); display: inline-flex; align-items: center; gap: 4px; }
.employee-detail__current-task { display: grid; gap: 4px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--astra-border); }
.employee-detail__current-task small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.employee-detail__current-task strong { font-size: var(--astra-font-emphasis); }
.employee-detail__inline-alert { margin-top: 12px; }
.employee-detail__blocking { display: grid; gap: 4px; margin-top: 12px; }
.employee-detail__blocking ul { margin: 0; padding-left: 18px; color: var(--astra-text-secondary); font-size: var(--astra-font-helper); line-height: 1.7; }
.employee-detail__equipment-cell { display: grid; gap: 2px; min-width: 0; }
.employee-detail__equipment-cell strong { font-size: var(--astra-font-label); }
.employee-detail__chart { height: 300px; }
.employee-detail__chart > * { height: 100%; }
.employee-detail__panels :deep(.el-table__row) { cursor: pointer; }
.employee-detail__task-cell { display: grid; min-width: 0; }
.employee-detail__task-cell strong, .employee-detail__task-cell small { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.employee-detail__task-cell strong { color: var(--astra-text-primary); font-size: var(--astra-font-label); }
.employee-detail__task-cell small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
@media (max-width: 1280px) { .employee-detail__metrics { grid-template-columns: repeat(3, minmax(0, 1fr)); } }
@media (max-width: 1020px) { .employee-detail__panels { grid-template-columns: 1fr; } }
@media (max-width: 720px) { .employee-detail__metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); } .employee-detail__hero { flex-wrap: wrap; } }
</style>
