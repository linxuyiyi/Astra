<template>
  <section class="astra-page" v-loading="loading" data-testid="task-detail">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="reload">重试</el-button>
    </el-alert>
    <template v-if="task">
      <AstraPageHeader :title="task.title" :description="task.current_progress">
        <SemanticTag :tone="semanticTone(statusTone(task.status))" :label="statusLabel(task.status)" />
        <dl class="task-context" aria-label="任务执行上下文">
          <div><dt>Task ID</dt><dd><IdDisplay :value="task.id" /></dd></div>
          <div><dt>AgentWorker</dt><dd>{{ task.worker_name || (task.agent_worker_id ? '不可用' : '未分配') }}</dd></div>
          <div v-if="task.worker_online === true">
            <dt>连接</dt><dd>在线<span v-if="task.worker_last_checked_at"> · 最近检查 {{ formatTime(task.worker_last_checked_at) }}</span></dd>
          </div>
          <div><dt>Session ID</dt><dd><IdDisplay v-if="task.current_session_id" :value="task.current_session_id" /><span v-else>—</span></dd></div>
          <div><dt>Git 仓库</dt><dd :title="repo?.name">{{ repo?.name || (repoError ? '暂不可读' : '—') }}</dd></div>
          <div><dt>当前分支</dt><dd :title="repo?.branch">{{ repo?.branch || (repo?.commit ? '分离 HEAD' : '—') }}</dd></div>
        </dl>
        <template #actions>
          <el-button @click="router.push({ name: 'tasks' })">返回任务大厅</el-button>
          <el-button v-if="primaryAction" type="primary" :disabled="primaryAction.disabled" :loading="acting" @click="act(primaryAction.command)">
            {{ primaryAction.label }}
          </el-button>
          <RowMenu :aria-label="task.title" :items="secondaryActions" :disabled="acting" @command="act" />
        </template>
      </AstraPageHeader>
      <p v-if="task.worker_online === false" class="task-connection" role="status">
        Worker 离线<span v-if="task.worker_last_checked_at"> · 最近检查 {{ formatTime(task.worker_last_checked_at) }}</span>
      </p>
      <el-alert v-if="task.pending_message_id" title="提交尚未确认，连接恢复后自动重发，可能重复执行。" type="warning" :closable="false" />
      <el-tabs v-model="tab" class="task-tabs">
        <el-tab-pane label="原生工作区" name="workspace" class="task-pane">
          <iframe v-if="frameUrl" :key="frameUrl" :src="frameUrl" title="OpenCode 任务工作区" allow="clipboard-read; clipboard-write" />
          <el-empty v-else-if="!checkingWorkspace" :description="workspaceMessage">
            <el-button v-if="task.current_session_id && task.waiting_reason !== 'session_missing'" @click="reload">重试连接</el-button>
          </el-empty>
          <p v-else class="workspace-loading">正在读取原生工作区</p>
        </el-tab-pane>
        <el-tab-pane label="任务详情" name="details" class="task-pane task-pane--scroll">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="更新时间">{{ formatTime(task.updated_at) }}</el-descriptions-item>
          </el-descriptions>
          <h3>任务描述</h3>
          <p class="description">{{ task.description }}</p>
          <template v-if="task.acceptance_criteria.length">
            <h3>验收标准</h3>
            <ul><li v-for="criterion in task.acceptance_criteria" :key="criterion.id">{{ criterion.content }}</li></ul>
          </template>
        </el-tab-pane>
        <el-tab-pane label="事件" name="events" class="task-pane task-pane--events">
          <el-alert v-if="contentError" :title="contentError" type="error" :closable="false"><el-button @click="loadTab()">重试</el-button></el-alert>
          <div class="event-pages">
            <el-button :disabled="!hasOlderEvents" @click="loadTab('older')">更早事件</el-button>
            <el-button v-if="browsingEvents" @click="loadTab('latest')">返回最新</el-button>
          </div>
          <el-table :data="events" height="100%" row-key="id">
            <el-table-column label="时间" width="180">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="message" label="事件" />
            <template #empty><el-empty :image-size="72" description="暂无任务事件" /></template>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="经验复盘" name="review" class="task-pane task-pane--scroll">
          <el-alert v-if="contentError" :title="contentError" type="error" :closable="false"><el-button @click="loadTab()">重试</el-button></el-alert>
          <el-empty v-else-if="!reviews.length" description="确认完成后按平台开关触发复盘" />
          <el-card v-for="job in reviews" :key="job.id" shadow="never">
            <p>{{ job.status }} · {{ job.phase }}</p>
            <p>{{ job.error_message || job.zero_reason }}</p>
            <router-link v-for="id in job.candidate_ids" :key="id" :to="`/library?knowledge=${id}`">查看候选 {{ id }} </router-link>
            <el-button v-if="job.status === 'FAILED' || job.status === 'RETRY_WAIT'" @click="retryReview(job.id)">重试</el-button>
          </el-card>
        </el-tab-pane>
      </el-tabs>
      <TaskCreateDrawer :open="editing" :task="task" @close="editing = false" @saved="saved" />
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { api } from '@/api/client'
import type { Task, TaskWorkspaceBinding, AstraEvent, ExperienceReviewJob } from '@/api/types'
import AstraPageHeader from '@/components/astra/AstraPageHeader.vue'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import { semanticTone } from '@/features/workers/semantic'
import RowMenu from '@/components/astra/RowMenu.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { runTaskAction, taskActions } from '@/features/tasks/actions'
import { statusLabel, statusTone, formatTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id))
const task = ref<Task>()
const binding = ref<TaskWorkspaceBinding>()
const events = ref<AstraEvent[]>([])
const eventLimit = 100
const hasOlderEvents = ref(false)
const browsingEvents = ref(false)
let eventCursor: number | undefined
const reviews = ref<ExperienceReviewJob[]>([])
const loading = ref(true)
const checkingWorkspace = ref(false)
const acting = ref(false)
const error = ref('')
const editing = ref(false)
const tabs = ['workspace', 'details', 'events', 'review']
const tab = computed({
  get: () => tabs.includes(String(route.query.tab)) ? String(route.query.tab) : 'workspace',
  set: value => { if (value !== tab.value) void router.replace({ query: { ...route.query, tab: value } }) },
})
const repo = ref<Awaited<ReturnType<typeof api.taskRepo>>>()
const repoError = ref(false)
const workspaceError = ref('')
const contentError = ref('')
const actions = computed(() => task.value ? taskActions(task.value) : [])
const primaryAction = computed(() => actions.value.find(item => ['start', 'pause', 'resume', 'confirm'].includes(item.command)))
const secondaryActions = computed(() => actions.value.filter(item => item !== primaryAction.value &&
  (item.command !== 'workspace' || tab.value !== 'workspace')))
const frameUrl = computed(() => {
  const session = task.value?.current_session_id
  const runtime = binding.value?.runtime
  if (!session || task.value?.waiting_reason === 'session_missing' || runtime?.session_id !== session || !runtime.workspace_ui) return ''
  return `${runtime.workspace_ui}?task=${encodeURIComponent(id.value)}&session=${encodeURIComponent(session)}`
})
const workspaceMessage = computed(() => {
  if (task.value?.waiting_reason === 'session_missing') return '原生 Session 已不存在，请重新开始'
  if (task.value?.worker_online === false) return 'Worker 离线，等待连接恢复'
  if (!task.value?.current_session_id) return '任务尚未创建原生会话'
  return workspaceError.value || binding.value?.runtime.workspace_ui_error || '工作区暂不可用，请重试'
})
let timer: ReturnType<typeof setInterval> | undefined
let taskRequest: AbortController | undefined
let workspaceRequest: AbortController | undefined
let repoRequest: AbortController | undefined
let tabRequest: AbortController | undefined

async function loadWorkspace() {
  if (!task.value?.current_session_id || task.value.waiting_reason === 'session_missing' || workspaceRequest) return
  const controller = new AbortController()
  workspaceRequest = controller
  checkingWorkspace.value = true
  workspaceError.value = ''
  try {
    const result = await api.taskWorkspace(id.value, controller.signal)
    if (!controller.signal.aborted) binding.value = result
  } catch (reason) {
    if (!controller.signal.aborted) workspaceError.value = String(reason)
  } finally {
    if (workspaceRequest === controller) {
      workspaceRequest = undefined
      checkingWorkspace.value = false
    }
  }
}
async function loadRepo() {
  if (!task.value?.workspace || repoRequest) return
  const controller = new AbortController()
  repoRequest = controller
  try {
    const result = await api.taskRepo(id.value, controller.signal)
    if (!controller.signal.aborted) {
      repo.value = result.commit ? result : undefined
      repoError.value = false
    }
  } catch {
    if (!controller.signal.aborted) repoError.value = true
  } finally {
    if (repoRequest === controller) repoRequest = undefined
  }
}
async function loadTab(mode: 'auto' | 'older' | 'latest' = 'auto') {
  const name = tab.value
  if (tabRequest || (name !== 'events' && name !== 'review')) return
  if (name === 'events' && mode === 'auto' && browsingEvents.value) return
  const controller = new AbortController()
  tabRequest = controller
  try {
    if (name === 'events') {
      const query = new URLSearchParams({ limit: String(eventLimit) })
      const before = mode === 'older' ? events.value.at(-1)?.id : undefined
      const after = mode === 'auto' ? eventCursor : undefined
      if (before !== undefined) query.set('before_id', String(before))
      if (after !== undefined) query.set('after_id', String(after))
      const result = await api.taskEvents(id.value, query, controller.signal)
      if (controller.signal.aborted) return
      if (before !== undefined) {
        if (result.length) {
          events.value = result
          browsingEvents.value = true
        }
        hasOlderEvents.value = result.length === eventLimit
      } else if (after !== undefined) {
        if (result.length) {
          eventCursor = result.at(-1)!.id
          const merged = [...result.reverse(), ...events.value]
          hasOlderEvents.value ||= merged.length > eventLimit
          events.value = merged.slice(0, eventLimit)
        }
      } else {
        events.value = result
        eventCursor = result[0]?.id ?? 0
        browsingEvents.value = false
        hasOlderEvents.value = result.length === eventLimit
      }
    } else {
      const result = await api.experienceReviewJobs(id.value, controller.signal)
      if (!controller.signal.aborted) reviews.value = result
    }
    if (!controller.signal.aborted) contentError.value = ''
  } catch (reason) {
    if (!controller.signal.aborted) contentError.value = String(reason)
  } finally {
    if (tabRequest === controller) tabRequest = undefined
  }
}
async function load() {
  if (taskRequest) return
  const controller = new AbortController()
  taskRequest = controller
  try {
    const next = await api.task(id.value, controller.signal)
    if (controller.signal.aborted) return
    const previous = task.value
    task.value = next
    error.value = ''
    const changed = next.current_session_id !== previous?.current_session_id || next.agent_worker_id !== previous?.agent_worker_id
    const connectionChanged = next.worker_online !== previous?.worker_online
    if (changed) {
      workspaceRequest?.abort()
      workspaceRequest = undefined
      checkingWorkspace.value = false
      binding.value = undefined
    }
    if (changed) {
      repoRequest?.abort()
      repoRequest = undefined
      repo.value = undefined
      repoError.value = false
    }
    if (changed || next.status !== previous?.status || next.current_progress !== previous?.current_progress || connectionChanged) {
      if (!frameUrl.value) void loadWorkspace()
      void loadRepo()
    }
  } catch (reason) {
    if (!controller.signal.aborted) error.value = String(reason)
  } finally {
    if (taskRequest === controller) {
      taskRequest = undefined
      loading.value = false
    }
  }
}
function refreshTask() {
  taskRequest?.abort()
  taskRequest = undefined
  return load()
}
function reload() {
  void refreshTask()
  void loadWorkspace()
  void loadRepo()
  void loadTab()
}
function saved() {
  editing.value = false
  void refreshTask()
}
async function act(command: string) {
  if (!task.value || acting.value) return
  if (command === 'workspace') {
    tab.value = 'workspace'
    return
  }
  if (command === 'edit') {
    editing.value = true
    return
  }
  acting.value = true
  try {
    if (await runTaskAction(task.value, command)) {
      if (command === 'delete') await router.push({ name: 'tasks' })
      else if (command === 'reconnect') reload()
      else await refreshTask()
    }
  } catch (reason) {
    ElMessage.error(String(reason))
    await refreshTask()
  } finally {
    acting.value = false
  }
}
async function retryReview(jobId: string) {
  try {
    await api.retryExperienceReview(jobId)
    await loadTab()
  } catch (reason) {
    ElMessage.error(String(reason))
  }
}
function abortRequests() {
  taskRequest?.abort()
  workspaceRequest?.abort()
  repoRequest?.abort()
  tabRequest?.abort()
  taskRequest = workspaceRequest = repoRequest = tabRequest = undefined
}
watch(id, () => {
  abortRequests()
  task.value = binding.value = repo.value = undefined
  checkingWorkspace.value = repoError.value = editing.value = false
  error.value = contentError.value = workspaceError.value = ''
  events.value = []
  eventCursor = undefined
  browsingEvents.value = hasOlderEvents.value = false
  reviews.value = []
  loading.value = true
  void load()
  void loadTab()
})
watch(tab, name => {
  tabRequest?.abort()
  tabRequest = undefined
  contentError.value = ''
  if (name === 'workspace' && !frameUrl.value) void loadWorkspace()
  if (name === 'details') void loadRepo()
  void loadTab()
})
onMounted(() => {
  void load()
  void loadTab()
  timer = setInterval(() => {
    if (!document.hidden) {
      void load()
      void loadTab()
    }
  }, 4000)
})
onUnmounted(() => {
  clearInterval(timer)
  abortRequests()
})
</script>

<style scoped>
.astra-page > .task-tabs {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}
.task-tabs :deep(.el-tabs__content) { flex: 1; min-height: 0; overflow: hidden; }
.task-pane { height: 100%; min-height: 0; }
.task-pane--events { display: flex; flex-direction: column; }
.event-pages { display: flex; justify-content: flex-end; margin-bottom: 6px; }
.task-connection { margin: 0; color: var(--astra-warning); }
.task-context { display: flex; flex-wrap: wrap; gap: 4px 14px; margin: 0; min-width: 0; }
.task-context div { display: flex; align-items: center; gap: 4px; min-width: 0; }
.task-context dt { color: var(--astra-text-muted); white-space: nowrap; }
.task-context dd { margin: 0; max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-pane--scroll { overflow-y: auto; padding-right: 12px; scrollbar-gutter: stable; }
iframe { display: block; width: 100%; height: 100%; border: 0; }
.description { white-space: pre-wrap; overflow-wrap: anywhere; }
.workspace-loading { color: var(--astra-text-muted); }
</style>
