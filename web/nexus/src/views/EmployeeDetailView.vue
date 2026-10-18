<template>
  <div v-if="employee" class="employee-detail" data-testid="employee-detail-page">
    <ElBreadcrumb separator="/" class="employee-detail__nav">
      <ElBreadcrumbItem><RouterLink to="/workers">Worker</RouterLink></ElBreadcrumbItem>
      <ElBreadcrumbItem>{{ employeeDisplayName(employee.name) }}</ElBreadcrumbItem>
    </ElBreadcrumb>

    <ElCard shadow="never">
      <div class="employee-detail__hero">
        <EmployeeAvatar :employee="employee" size="large" />
        <div class="employee-detail__identity">
          <div class="employee-detail__identity-top">
            <small>{{ employee.id }}</small>
            <ElTag size="small" :type="statusTagType(employee.runtime_state)">{{ employee.runtime_status_label }}</ElTag>
          </div>
          <h2>{{ employeeDisplayName(employee.name) }}</h2>
          <ElText size="small" type="info">{{ employee.role }} · {{ employee.description || '暂无职责说明' }}</ElText>
        </div>
        <ElSpace :size="10">
          <ElButton :icon="Settings2" @click="editOpen = true">编辑</ElButton>
          <ElButton v-if="employee.status !== 'PAUSED' && !employee.current_task_count" :icon="Pause" @click="employeeAction('pause')">暂停 Worker</ElButton>
          <ElButton v-else-if="employee.status === 'PAUSED' && !employee.current_task_count" type="primary" :icon="Play" @click="employeeAction('resume')">恢复 Worker</ElButton>
        </ElSpace>
      </div>
    </ElCard>

    <ElTabs v-model="detailSection" class="employee-detail__tabs">
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
        <ElStatistic :title="metric.label" :value="metric.value as number"><template v-if="metric.unit" #suffix>{{ metric.unit }}</template></ElStatistic>
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

      <ElTabPane label="配置" name="config">
    <section class="employee-detail__panels">
      <ElCard shadow="never">
        <template #header>
          <div class="employee-detail__card-title"><h3>工作范围</h3><ElText size="small" type="info">用于自动分配的任务类型和分类</ElText></div>
        </template>
        <div class="employee-detail__scope">
          <div><ElText size="small" type="info">支持任务类型</ElText><div class="employee-detail__tags"><ElTag v-for="item in employee.task_types" :key="item" size="small" type="primary" effect="light">{{ taskTypeLabel(item) }}</ElTag><ElText v-if="!employee.task_types.length" size="small">—</ElText></div></div>
          <div><ElText size="small" type="info">支持任务分类</ElText><div class="employee-detail__tags"><ElTag v-for="item in employee.category_ids" :key="item" size="small" type="primary" effect="light">{{ categoryName(item) }}</ElTag><ElText v-if="!employee.category_ids.length" size="small">—</ElText></div></div>
        </div>
      </ElCard>

      <ElCard shadow="never">
        <template #header>
          <div class="employee-detail__card-header">
            <div class="employee-detail__card-title"><h3>工作环境</h3><ElText size="small" type="info">Worker 长期复用的 Agent 工作空间</ElText></div>
            <ElButton size="small" :icon="RefreshCw" :loading="checking" @click="testEnvironment">{{ checking ? '检查中' : '测试连接' }}</ElButton>
          </div>
        </template>
        <template v-if="employee.execution_environment">
          <ElDescriptions :column="2" border size="small">
            <ElDescriptionsItem label="环境名称">{{ employee.execution_environment.name }}</ElDescriptionsItem>
            <ElDescriptionsItem label="执行方式">{{ environmentLabel(employee.execution_environment) }}</ElDescriptionsItem>
            <ElDescriptionsItem label="服务器">{{ employee.execution_environment.mode === 'LINUX_REMOTE' ? `${employee.execution_environment.username}@${employee.execution_environment.host}:${employee.execution_environment.port}` : '本机' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="Agent">{{ employee.execution_environment.opencode_path }}</ElDescriptionsItem>
            <ElDescriptionsItem label="工作空间">{{ employee.execution_environment.workspace_root }}</ElDescriptionsItem>
            <ElDescriptionsItem label="环境状态"><ElTag size="small" :type="statusTagType(employee.channel_health.availability === 'offline' ? 'OFFLINE' : employee.execution_environment.status)">{{ employee.channel_health.availability === 'offline' ? '离线' : statusLabel(employee.execution_environment.status) }}</ElTag></ElDescriptionsItem>
            <ElDescriptionsItem label="Worker">{{ employee.channel_health.worker_online ? '在线' : '离线' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="OpenCode">{{ employee.channel_health.opencode_reachable ? '可达' : '不可达' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="仓库">{{ employee.channel_health.repository_bound ? '已绑定' : '未绑定' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="执行">{{ employee.channel_health.executing ? '执行中' : '未在执行' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="人工">{{ employee.channel_health.waiting_human ? '等待人工' : '无需等待' }}</ElDescriptionsItem>
            <ElDescriptionsItem label="最近检查" :span="2">{{ formatTime(employee.execution_environment.last_checked_at) }}</ElDescriptionsItem>
          </ElDescriptions>
          <div v-if="environmentChecks.length" class="employee-detail__checks">
            <div v-for="check in environmentChecks" :key="check.name" class="employee-detail__check">
              <CheckCircle2 v-if="check.passed" :size="14" class="employee-detail__check-icon is-passed" />
              <CircleX v-else :size="14" class="employee-detail__check-icon is-failed" />
              <div><strong>{{ check.name }}</strong><ElText size="small" type="info">{{ check.detail }}</ElText></div>
            </div>
          </div>
        </template>
        <ElText v-else size="small" type="info">尚未配置工作环境，可在“编辑”中添加。</ElText>
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
import { ArrowUpRight, CheckCircle2, CircleX, Pause, Play, RefreshCw, Settings2 } from 'lucide-vue-next'
import { computed, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { ElAlert, ElBreadcrumb, ElBreadcrumbItem, ElButton, ElCard, ElDescriptions, ElDescriptionsItem, ElEmpty, ElRadioButton, ElRadioGroup, ElSpace, ElStatistic, ElTabPane, ElTable, ElTableColumn, ElTabs, ElTag, ElText } from 'element-plus'
import { api } from '@/api/client'
import type { EmployeeUsage, Task } from '@/api/types'
import EmployeeAvatar from '@/components/employee/EmployeeAvatar.vue'
import EmployeeConfigModal from '@/components/employee/EmployeeConfigModal.vue'
import UsageTrendChart from '@/components/runtime/UsageTrendChart.vue'
import { notify } from '@/composables/useToast'
import { useAstraStore } from '@/stores/astra'
import { employeeDisplayName, environmentLabel, formatDurationSeconds, formatTaskDuration, formatTime, formatTokens, platformCopy, priorityLabel, statusLabel, statusTagType, taskTypeLabel } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const store = useAstraStore()
const id = computed(() => String(route.params.id))
const usage = ref<EmployeeUsage | null>(null)
const usageError = ref('')
const days = ref(7)
const checking = ref(false)
const editOpen = ref(false)
const environmentChecks = ref<Array<{ name: string; passed: boolean; detail: string }>>([])
type DetailSection = 'tasks' | 'usage' | 'config'
function sectionFromHash(hash: string): DetailSection {
  const name = hash.replace('#', '')
  return name === 'usage' || name === 'config' ? name : 'tasks'
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
const currentTask = computed(() => employeeTasks.value.find((task) => ACTIVE_STATUSES.includes(task.status)))
const employeeQueue = computed(() => employeeTasks.value
  .filter((task) => ACTIVE_STATUSES.includes(task.status))
  .sort((a, b) => (PRIORITY_WEIGHT[b.priority] ?? 0) - (PRIORITY_WEIGHT[a.priority] ?? 0) || a.created_at.localeCompare(b.created_at)))
const historicalTasks = computed(() => employeeTasks.value.filter((task) => ['SUCCEEDED', 'FAILED', 'CANCELED'].includes(task.status)))
const latestError = computed(() => employeeTasks.value.filter((task) => task.execution_error).sort((a, b) => b.updated_at.localeCompare(a.updated_at))[0])
const latestErrorCopy = computed(() => latestError.value
  ? `最近异常：${platformCopy(String(latestError.value.execution_error?.reason ?? latestError.value.current_progress))} · ${formatTime(latestError.value.updated_at)}`
  : '')
const metrics = computed(() => {
  const today = employee.value?.today_usage
  return [
    { label: '今日 Token', value: formatTokens(today?.total_tokens), unit: '', hint: 'Agent 实际用量' },
    { label: '工具调用', value: today?.tool_call_count ?? '—', unit: '次', hint: '成功与失败均保留' },
    { label: '今日完成', value: employee.value?.today_completed ?? '—', unit: '项', hint: 'Worker 已提交结果' },
    { label: '处理中', value: statusCount('RUNNING'), unit: '项', hint: '当前工作' },
    { label: '等待人工', value: statusCount('WAITING'), unit: '项', hint: '需要补充信息' },
    { label: '今日工作时长', value: formatDurationSeconds(today?.duration_seconds), unit: '', hint: '按任务开始到结束计算' },
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

async function loadUsage() {
  usageError.value = ''
  try { usage.value = await api.employeeUsage(id.value, days.value) }
  catch (error) { usageError.value = error instanceof Error ? error.message : '用量数据加载失败' }
}
async function load() { await store.refresh(); await loadUsage() }
onMounted(load)
async function onSaved() { await store.refresh() }
async function employeeAction(action: 'pause' | 'resume') {
  try { await api.employeeAction(id.value, action); await store.refresh() }
  catch (error) { notify(error instanceof Error ? error.message : 'Worker 操作失败', 'error') }
}
async function testEnvironment() {
  checking.value = true
  try { const result = await api.testEnvironment(id.value); environmentChecks.value = result.checks; await store.refresh() }
  catch (error) { notify(error instanceof Error ? error.message : '环境检查失败', 'error') }
  finally { checking.value = false }
}
</script>

<style scoped>
.employee-detail { display: grid; gap: 14px; height: 100%; min-height: 480px; padding: 20px 24px 24px; overflow: auto; }
.employee-detail__tabs :deep(.el-tab-pane) { display: grid; gap: 14px; }
.employee-detail__nav { font-size: var(--astra-font-label); }
.employee-detail__nav :deep(.el-breadcrumb__inner a) { color: var(--astra-text-muted); }
.employee-detail__nav :deep(.el-breadcrumb__inner.is-link a:hover), .employee-detail__nav :deep(.el-breadcrumb__inner.is-link):hover a { color: var(--astra-primary); }
.employee-detail__hero { display: flex; align-items: center; gap: 16px; }
.employee-detail__identity { display: grid; gap: 2px; flex: 1; min-width: 0; }
.employee-detail__identity-top { display: flex; align-items: center; gap: 10px; }
.employee-detail__identity-top small { color: var(--astra-primary); font-size: var(--astra-font-helper); letter-spacing: .08em; }
.employee-detail__identity h2 { margin: 0; font-size: var(--astra-font-section); line-height: normal; }
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
.employee-detail__panel--wide { grid-column: 1 / -1; }
.employee-detail__link, .employee-detail__link:hover { color: var(--astra-primary); font-size: var(--astra-font-label); display: inline-flex; align-items: center; gap: 4px; }
.employee-detail__current-task { display: grid; gap: 2px; margin-bottom: 12px; }
.employee-detail__current-task small { color: var(--astra-text-muted); font-size: var(--astra-font-helper); }
.employee-detail__current-task strong { font-size: var(--astra-font-emphasis); }
.employee-detail__inline-alert { margin-top: 12px; }
.employee-detail__scope { display: grid; gap: 12px; }
.employee-detail__scope > div { display: grid; gap: 6px; }
.employee-detail__tags { display: flex; flex-wrap: wrap; gap: 6px; }
.employee-detail__checks { display: grid; gap: 8px; margin-top: 12px; }
.employee-detail__check { display: flex; align-items: flex-start; gap: 8px; }
.employee-detail__check > svg { flex: none; margin-top: 3px; }
.employee-detail__check strong { display: block; font-size: var(--astra-font-body); }
.employee-detail__check-icon.is-passed { color: var(--el-color-success); }
.employee-detail__check-icon.is-failed { color: var(--el-color-danger); }
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
