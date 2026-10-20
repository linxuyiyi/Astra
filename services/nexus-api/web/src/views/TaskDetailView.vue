<template>
  <ElContainer direction="vertical" class="task-detail" data-testid="task-workspace">
    <ElHeader v-if="task" class="task-header">
      <ElButton :tag="RouterLink" to="/tasks" :icon="ArrowLeft">返回任务列表</ElButton>
      <div class="task-title">
        <p class="task-kicker">任务目标</p>
        <h1>{{ task.description || task.title }}</h1>
        <div class="task-title__row">
          <RuntimeStatusBadge :projection="task" :show-detail="false" />
          <ElText type="info">{{ task.title }} · {{ task.id }}</ElText>
        </div>
        <p class="task-next">下一步：{{ nextStep }}</p>
        <ElText v-if="sessionFact" type="warning">{{ sessionFact }}</ElText>
      </div>
      <ElTag v-if="repo" class="repo" type="info" effect="plain" :title="repo.commit">{{ repoSummary }}</ElTag>
      <ElSelect v-if="binding?.sessions.length" v-model="selectedSession" class="task-header__session" data-testid="session-select" :data-session-id="selectedSession" aria-label="选择会话">
        <ElOption v-for="(item, index) in binding.sessions" :key="item.id" :value="item.id" :label="`会话 #${binding.sessions.length - index} · ${item.current ? '当前' : '历史只读'}`" />
      </ElSelect>
      <div class="task-header__actions">
        <ElButton v-if="primaryDetail" type="primary" :disabled="busy || loading" :loading="loading && primaryDetail.key === 'refresh'" @click="primaryDetail.run()">{{ primaryDetail.label }}</ElButton>
        <ElDropdown trigger="click" @command="onDetailCommand">
          <ElButton :disabled="busy">更多</ElButton>
          <template #dropdown>
            <ElDropdownMenu>
              <ElDropdownItem command="refresh" data-testid="task-detail-refresh">刷新</ElDropdownItem>
              <ElDropdownItem v-if="task.status === 'RUNNING' || task.status === 'WAITING' || task.pending_message_id" command="stop">终止任务</ElDropdownItem>
              <ElDropdownItem v-if="task.current_session_id && task.status !== 'RUNNING' && !task.pending_message_id" command="restart">重新开始</ElDropdownItem>
              <ElDropdownItem v-if="task.workspace" command="changes">代码变更</ElDropdownItem>
              <ElDropdownItem command="info">任务信息</ElDropdownItem>
              <ElDropdownItem command="events" data-testid="task-events-open">实时事件</ElDropdownItem>
              <ElDropdownItem command="usage" data-testid="task-usage-open">用量</ElDropdownItem>
            </ElDropdownMenu>
          </template>
        </ElDropdown>
      </div>
    </ElHeader>

    <ElMain class="task-detail__main">
      <ElAlert
        v-if="error || task?.runtime_error || task?.pending_message_id"
        class="workspace-alert"
        :type="error || task?.runtime_error ? 'error' : 'warning'"
        show-icon
        :closable="false"
        :title="error || task?.runtime_error || '正在核对 Session 输入；启动中断时可先停止后重新开始。'"
      >
        <div class="workspace-alert__body">
          <span>最后已知状态：{{ task?.runtime_status_label }}</span>
          <ElButton :loading="loading" :disabled="busy || loading" @click="load">刷新状态</ElButton>
          <ElButton :disabled="busy || loading" @click="reconnect">重连</ElButton>
          <ElButton v-if="task?.pending_message_id" :disabled="busy || loading" @click="action('retry-submission')">核对并重试未确认输入</ElButton>
        </div>
      </ElAlert>
      <ElAlert v-if="workspaceUiError" class="workspace-alert" type="warning" show-icon :closable="false" :title="workspaceUiError" />
      <iframe v-else-if="frameUrl" :key="frameKey" :src="frameUrl" title="OpenCode Task Workspace" class="task-detail__frame" allow="clipboard-read; clipboard-write" />
      <ElEmpty v-else-if="binding" :image-size="80" :description="task?.current_session_id ? 'Session 绑定异常，请重连检查；系统不会自动创建新 Session。' : '任务尚未创建 Session，请先启动任务。'" />
    </ElMain>

    <ElDrawer v-model="showChanges" title="代码变更" size="min(800px, 100%)">
      <ElTabs v-model="changeMode" @tab-change="loadChanges">
        <ElTabPane label="任务累计变更" name="task" />
        <ElTabPane label="当前未提交" name="git" />
      </ElTabs>
      <ElAlert v-if="changesError" type="error" :title="changesError" :closable="false" />
      <ElText v-if="changesLoading">正在读取 Git 变更…</ElText>
      <ElEmpty v-else-if="!changesError && !changes.length" description="暂无代码变更" />
      <ElCollapse v-else>
        <ElCollapseItem v-for="item in changes" :key="item.file" :title="`${item.file}  +${item.additions} / -${item.deletions}`">
          <pre class="task-diff">{{ item.patch || '二进制文件或无文本差异' }}</pre>
        </ElCollapseItem>
      </ElCollapse>
    </ElDrawer>
    <ElDrawer v-if="task" v-model="showInfo" class="task-info" title="任务信息" size="min(480px, 100%)">
      <dl class="task-facts">
        <div><dt>任务目标</dt><dd>{{ task.description || '未写目标' }}</dd></div>
        <div><dt>当前状态</dt><dd>{{ task.runtime_status_label }}</dd></div>
        <div><dt>下一步</dt><dd>{{ nextStep }}</dd></div>
        <div><dt>责任数字员工</dt><dd>{{ responsibleName }}</dd></div>
        <div><dt>执行 Worker</dt><dd>{{ employeeName }}</dd></div>
        <div>
          <dt>验收标准</dt>
          <dd>
            <ol v-if="task.acceptance_criteria.length" class="task-info__criteria"><li v-for="criterion in task.acceptance_criteria" :key="criterion.id">{{ criterion.content }}</li></ol>
            <span v-else>未设置验收标准</span>
          </dd>
        </div>
      </dl>
      <ElCollapse>
        <ElCollapseItem title="技术信息" name="tech">
          <dl class="task-facts">
            <div><dt>任务编号</dt><dd>{{ task.id }}</dd></div>
            <div><dt>类型 / 分类</dt><dd>{{ taskTypeLabel(task.task_type) }} · {{ categoryName }}</dd></div>
            <div><dt>优先级</dt><dd>{{ priorityLabel(task.priority) }}</dd></div>
            <div><dt>会话结果</dt><dd>{{ sessionFact || '尚未开始' }}</dd></div>
            <div v-if="task.human_required_reason"><dt>等待原因</dt><dd>{{ task.human_required_reason }}</dd></div>
            <div><dt>时间</dt><dd>创建 {{ formatTime(task.created_at) }} · 开始 {{ formatTime(task.started_at) }} · {{ duration }}</dd></div>
            <div><dt>来源</dt><dd>{{ task.source_session_id || '无会话' }} · {{ task.source_conversation_id || '无对话' }}</dd></div>
            <div><dt>工作目录</dt><dd><code class="task-info__path">{{ binding?.runtime.workspace || '尚未准备' }}</code></dd></div>
            <div><dt>执行会话</dt><dd>{{ selectedSession || '尚未创建' }}</dd></div>
          </dl>
        </ElCollapseItem>
      </ElCollapse>
    </ElDrawer>
    <ElDrawer v-model="showEvents" title="实时事件" size="min(640px, 100%)" data-testid="task-events-drawer">
      <ElEmpty v-if="!events.length" description="还没有任务事件" />
      <ElTimeline v-else>
        <ElTimelineItem v-for="item in events" :key="item.id" :timestamp="formatTime(item.created_at)">
          <strong>{{ item.event_type }}</strong>
          <div>{{ item.message }}</div>
        </ElTimelineItem>
      </ElTimeline>
    </ElDrawer>
    <ElDrawer v-model="showUsage" title="用量" size="min(520px, 100%)" data-testid="task-usage-drawer">
      <ElAlert v-if="usageError" :title="usageError" type="error" :closable="false" />
      <template v-else-if="usage">
        <ElDescriptions :column="1" border size="small">
          <ElDescriptionsItem label="总 Token">{{ formatTokens(usage.summary.total_tokens) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="输入 / 输出">{{ formatTokens(usage.summary.input_tokens) }} / {{ formatTokens(usage.summary.output_tokens) }}</ElDescriptionsItem>
          <ElDescriptionsItem label="模型调用">{{ usage.summary.llm_call_count ?? 0 }}</ElDescriptionsItem>
          <ElDescriptionsItem label="工具调用">{{ usage.summary.tool_call_count ?? 0 }}</ElDescriptionsItem>
          <ElDescriptionsItem label="会话数">{{ usage.sessions }}</ElDescriptionsItem>
        </ElDescriptions>
        <ElTable v-if="usage.tools.length" :data="usage.tools" size="small" style="margin-top: 16px">
          <ElTableColumn prop="tool_name" label="工具" />
          <ElTableColumn prop="call_count" label="次数" width="80" />
          <ElTableColumn prop="success_count" label="成功" width="80" />
          <ElTableColumn prop="failure_count" label="失败" width="80" />
        </ElTable>
      </template>
      <ElEmpty v-else description="正在读取用量" />
    </ElDrawer>
  </ElContainer>
</template>

<script setup lang="ts">
import { ArrowLeft } from 'lucide-vue-next'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElAlert, ElButton, ElCollapse, ElCollapseItem, ElContainer, ElDrawer, ElDropdown, ElDropdownItem, ElDropdownMenu, ElEmpty, ElHeader, ElMain, ElOption, ElSelect, ElTable, ElTableColumn, ElTabPane, ElTabs, ElTag, ElText, ElTimeline, ElTimelineItem } from 'element-plus'
import { api } from '@/api/client'
import type { AstraEvent, Task, TaskUsage, TaskWorkspaceBinding } from '@/api/types'
import FieldHelp from '@/components/forms/FieldHelp.vue'
import RuntimeStatusBadge from '@/components/runtime/RuntimeStatusBadge.vue'
import { useAstraStore } from '@/stores/astra'
import { employeeDisplayName, formatTaskDuration, formatTime, formatTokens, priorityLabel, taskTypeLabel } from '@/utils/format'
import { listEmployees } from '../api'
import type { Employee as NexusEmployee } from '../types'

const route = useRoute()
const store = useAstraStore()
const task = ref<Task | null>(null)
const binding = ref<TaskWorkspaceBinding | null>(null)
const repo = ref<{ name: string; branch: string; commit: string; dirty: boolean; changes: number; task_changes?: { changed_files?: number; commits?: string[] } } | null>(null)
const selectedSession = ref('')
const error = ref('')
const busy = ref(false)
const loading = ref(false)
const showInfo = ref(false)
const frameKey = ref(0)

const owners = ref<NexusEmployee[]>([])
const employeeName = computed(() => {
  const workerId = task.value?.worker_id || task.value?.employee_id
  const employee = store.employees.find(item => item.id === workerId)
  return employee?.execution_environment?.name || (employee ? employeeDisplayName(employee.name) : '未分配 Worker')
})
const responsibleName = computed(() => {
  const id = task.value?.responsible_employee_id
  if (!id) return '未指定'
  return owners.value.find(item => item.id === id)?.name ?? id
})
const showEvents = ref(false)
const showUsage = ref(false)
const events = ref<AstraEvent[]>([])
const usage = ref<TaskUsage | null>(null)
const usageError = ref('')
let eventSource: EventSource | null = null
function closeEvents() { eventSource?.close(); eventSource = null }
async function openEvents() {
  const id = String(route.params.id)
  showEvents.value = true
  events.value = await api.taskEvents(id).catch(() => [])
  closeEvents()
  eventSource = new EventSource('/api/events/stream')
  eventSource.addEventListener('update', (message) => {
    const data = JSON.parse((message as MessageEvent).data) as { id: number; type?: string; task_id: string | null; employee_id: string | null; phase: string; message: string; created_at: string }
    if (data.task_id !== id || events.value.some(item => item.id === data.id)) return
    events.value = [...events.value, { id: data.id, task_id: data.task_id, employee_id: data.employee_id, event_type: data.type || '', phase: data.phase, message: data.message, detail: {}, created_at: data.created_at }]
  })
}
async function openUsage() {
  showUsage.value = true
  usageError.value = ''
  usage.value = null
  try { usage.value = await api.taskUsage(String(route.params.id)) }
  catch (reason) { usageError.value = reason instanceof Error ? reason.message : '用量读取失败' }
}
const categoryName = computed(() => store.categories.find(item => item.id === task.value?.category_id)?.name ?? '未分类')
const repoSummary = computed(() => repo.value
  ? `${repo.value.name} / ${repo.value.branch || 'detached'} · ${repo.value.commit?.slice(0, 8)} · 累计 ${repo.value.task_changes?.changed_files ?? 0} 个文件 / ${repo.value.task_changes?.commits?.length ?? 0} 次提交 · 未提交 ${repo.value.changes}`
  : '')
const currentSession = computed(() => binding.value?.sessions.find(item => item.current))
const sessionFact = computed(() => {
  const current = currentSession.value
  if (!current && task.value?.runtime_connection !== 'UNAVAILABLE') return ''
  if (task.value?.runtime_connection === 'UNAVAILABLE' || current?.status === 'UNKNOWN') return '连接异常/状态待确认'
  if (task.value?.status === 'SUCCEEDED') return '已确认完成'
  if (current?.status === 'SUCCEEDED') return '本轮已完成，待确认'
  if (current?.status === 'FAILED' || current?.status === 'INTERRUPTED') return task.value?.human_required_reason || '执行异常，待恢复'
  if (current?.status === 'WAITING') return task.value?.human_required_reason || '等待回答或授权'
  if (current?.status === 'RUNNING') return '执行中'
  return ''
})
const canFinish = computed(() => task.value?.status === 'WAITING'
  && currentSession.value?.status === 'SUCCEEDED'
  && !task.value.pending_message_id)
const nextStep = computed(() => {
  const current = task.value
  if (!current) return ''
  if (current.pending_message_id) return '有一条输入还没确认，先核对并重试'
  if (current.human_required_reason) return current.human_required_reason
  if (!current.current_session_id) return '启动任务，让 Worker 开始执行'
  if (canFinish.value) return '本轮已结束，请确认是否完成'
  if (current.status === 'RUNNING') return 'Worker 正在执行，直接看下面的工作区'
  if (current.status === 'WAITING') return sessionFact.value || '等待处理后再继续'
  if (current.status === 'SUCCEEDED') return '任务已完成，可以查看总结'
  if (current.status === 'FAILED') return '执行失败，可以重新开始'
  if (current.status === 'CANCELED') return '任务已取消'
  return sessionFact.value || '查看当前状态'
})
const primaryDetail = computed(() => {
  const current = task.value
  if (!current) return null
  if (!current.current_session_id && !current.pending_message_id) return { key: 'start', label: '启动任务', run: () => action('start') }
  if (canFinish.value) return { key: 'confirm', label: '确认完成', run: () => action('confirm') }
  if (current.pending_message_id) return { key: 'retry', label: '核对并重试', run: () => action('retry-submission') }
  return { key: 'refresh', label: '刷新状态', run: () => load() }
})
function onDetailCommand(command: string | number) {
  const name = String(command)
  if (name === 'refresh') void load()
  else if (name === 'stop') void action('stop')
  else if (name === 'restart') void action('restart')
  else if (name === 'changes') openChanges()
  else if (name === 'info') showInfo.value = true
  else if (name === 'events') void openEvents()
  else if (name === 'usage') void openUsage()
}
const showChanges = ref(false)
const changesLoading = ref(false)
const changesError = ref('')
const changeMode = ref('task')
const changes = ref<Array<{ file: string; patch: string; additions: number; deletions: number }>>([])
let diffRequest: AbortController | null = null
async function loadRepo(id: string) {
  try {
    const value = await api.taskRepo(id)
    if (id === String(route.params.id)) repo.value = value
  } catch { if (id === String(route.params.id)) repo.value = null }
}
async function loadChanges() {
  if (!task.value?.current_session_id) return
  const id = String(route.params.id)
  const sessionId = task.value.current_session_id
  const mode = changeMode.value
  // 快速切换模式时取消上一个请求：只有当前请求能写数据、错误与加载态。
  diffRequest?.abort()
  const controller = new AbortController()
  diffRequest = controller
  changesLoading.value = true; changesError.value = ''
  // 读取有界等待与失败提示：长时间无响应时给出可读错误，而不是永久停留在加载态。
  const deadline = window.setTimeout(() => controller.abort(), 20000)
  try {
    const response = await fetch(`${import.meta.env.VITE_API_BASE ?? ''}/api/tasks/${encodeURIComponent(id)}/opencode/${encodeURIComponent(sessionId)}/vcs/diff?mode=${mode}`, { signal: controller.signal })
    const data = await response.json()
    if (diffRequest !== controller) return
    if (!response.ok) throw new Error(data.detail || '代码变更读取失败')
    if (id === String(route.params.id) && mode === changeMode.value) changes.value = data
    if (id === String(route.params.id)) void loadRepo(id)
  } catch (reason) {
    if (diffRequest !== controller) return
    if (id === String(route.params.id) && mode === changeMode.value) {
      changesError.value = controller.signal.aborted
        ? '读取 Git 变更超时，请重试'
        : reason instanceof Error ? reason.message : '代码变更读取失败'
    }
  } finally {
    window.clearTimeout(deadline)
    if (diffRequest === controller && id === String(route.params.id) && mode === changeMode.value) changesLoading.value = false
  }
}
async function openChanges() { showChanges.value = true; await loadChanges() }
const selectedSessionBinding = computed(() => binding.value?.sessions.find(item => item.id === selectedSession.value))
const workspaceUiError = computed(() => {
  if (!selectedSession.value) return ''
  return selectedSessionBinding.value?.workspace_ui_error || binding.value?.runtime.workspace_ui_error || ''
})
const frameUrl = computed(() => {
  if (!selectedSession.value || workspaceUiError.value) return ''
  const workspaceUi = selectedSessionBinding.value?.workspace_ui || binding.value?.runtime.workspace_ui
  if (!workspaceUi) return ''
  return `${workspaceUi}?task=${encodeURIComponent(String(route.params.id))}&session=${encodeURIComponent(selectedSession.value)}`
})
const now = ref(Date.now())
const duration = computed(() => {
  void now.value
  return task.value?.started_at ? formatTaskDuration(task.value) : '尚未开始'
})

let timer: ReturnType<typeof setInterval> | undefined
let loadRequest: { id: string; promise: Promise<void> } | null = null
function load() {
  const id = String(route.params.id)
  if (loadRequest?.id === id) return loadRequest.promise
  loading.value = true
  const promise = loadTask(id).finally(() => {
    if (loadRequest?.promise === promise) { loadRequest = null; loading.value = false }
  })
  loadRequest = { id, promise }
  return promise
}
async function loadTask(id: string) {
  try {
    const [nextTask, nextBinding] = await Promise.all([api.task(id), api.taskWorkspace(id)])
    if (id !== String(route.params.id)) return
    task.value = nextTask; binding.value = nextBinding
    if (!nextBinding.sessions.some(item => item.id === selectedSession.value)) selectedSession.value = nextTask.current_session_id || ''
    error.value = ''
    if (nextTask.workspace) await loadRepo(id)
    else repo.value = null
  } catch (reason) { if (id === String(route.params.id)) error.value = reason instanceof Error ? reason.message : '任务加载失败' }
}
async function action(name: string) {
  busy.value = true
  try { task.value = await api.taskAction(String(route.params.id), name); selectedSession.value = task.value.current_session_id || ''; await load() }
  catch (reason) { error.value = reason instanceof Error ? reason.message : '操作失败' }
  finally { busy.value = false }
}
async function reconnect() { await action('reconnect'); frameKey.value++ }
watch(() => route.params.id, () => { selectedSession.value = ''; closeEvents(); showEvents.value = false; void load() }, { immediate: true })
onMounted(() => {
  timer = setInterval(() => { now.value = Date.now() }, 1000)
  void listEmployees().then(items => { owners.value = items }).catch(() => { owners.value = [] })
})
onBeforeUnmount(() => { clearInterval(timer); diffRequest?.abort(); closeEvents() })
</script>

<style scoped>
.task-detail{--el-border-color-lighter:var(--astra-border);height:100%;min-height:480px;overflow:hidden}
.task-header{--el-header-height:auto;display:flex;flex-wrap:wrap;align-items:flex-start;gap:12px;padding:12px 20px;border-bottom:1px solid var(--astra-border);background:var(--astra-bg-panel)}
.task-title{flex:1;min-width:240px;display:grid;gap:4px}
.task-kicker{margin:0;color:var(--astra-text-muted)}
.task-title h1{display:-webkit-box;overflow:hidden;margin:0;font-size:var(--astra-font-section);line-height:1.4;-webkit-box-orient:vertical;-webkit-line-clamp:2}
.task-title__row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
.task-next{margin:0;line-height:1.5}
.task-facts{display:grid;gap:12px;margin:0 0 16px}
.task-facts div{display:grid;gap:2px}
.task-facts dt{color:var(--astra-text-muted)}
.task-facts dd{margin:0;line-height:1.55}
.task-header__session{width:220px;flex:none}
.task-header__actions{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-left:auto}
.repo{max-width:400px}
.repo :deep(.el-tag__content){overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.task-detail__main{--el-main-padding:0;display:flex;flex-direction:column;min-height:0;overflow:hidden;background:var(--astra-bg-base)}
.workspace-alert{flex:none;border-radius:0}
.workspace-alert__body{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-top:6px}
.task-detail__frame{flex:1;width:100%;min-height:0;border:0;background:#fff}
.task-detail__main :deep(.el-empty){margin:auto}
.task-info__paragraph{margin:0;white-space:pre-line;line-height:1.6}
.task-info__criteria{margin:0;padding-left:18px;display:grid;gap:4px;line-height:1.6}
.task-info__path{overflow-wrap:anywhere}
.task-diff{white-space:pre;overflow:auto;font-family:monospace;font-size:12px}
</style>
