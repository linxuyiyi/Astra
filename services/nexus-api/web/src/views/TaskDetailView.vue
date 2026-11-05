<template>
  <section class="astra-page" v-loading="loading" data-testid="task-detail">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="reload">重试</el-button>
    </el-alert>
    <template v-if="task">
      <AstraPageHeader :title="task.title" :description="task.current_progress">
        <el-tag :type="statusTagType(task.status)">{{ statusLabel(task.status) }}</el-tag>
        <span>Worker · {{ task.worker_name || (task.agent_worker_id ? '不可用' : '未分配') }}</span>
        <template #actions>
          <el-button @click="router.push({ name: 'tasks' })">返回任务大厅</el-button>
          <el-button v-if="primaryAction" type="primary" :disabled="primaryAction.disabled" :loading="acting" @click="act(primaryAction.command)">
            {{ primaryAction.label }}
          </el-button>
          <RowMenu :aria-label="task.title" :items="secondaryActions" :disabled="acting" @command="act" />
        </template>
      </AstraPageHeader>
      <el-alert v-if="task.pending_message_id" title="提交尚未确认，连接恢复后自动重发，可能重复执行。" type="warning" :closable="false" />
      <el-tabs v-model="tab" class="task-tabs">
        <el-tab-pane label="原生工作区" name="workspace" class="task-pane">
          <iframe v-if="frameUrl" :key="frameUrl" :src="frameUrl" title="OpenCode 任务工作区" allow="clipboard-read; clipboard-write" />
          <el-empty v-else-if="!checkingWorkspace" :description="workspaceMessage">
            <el-button v-if="task.current_session_id" @click="loadWorkspace">重试连接</el-button>
          </el-empty>
          <p v-else class="workspace-loading">正在读取原生工作区</p>
        </el-tab-pane>
        <el-tab-pane label="任务详情" name="details" class="task-pane task-pane--scroll">
          <el-descriptions :column="2" border>
            <el-descriptions-item label="任务编号">{{ task.id }}</el-descriptions-item>
            <el-descriptions-item label="更新时间">{{ formatTime(task.updated_at) }}</el-descriptions-item>
          </el-descriptions>
          <h3>任务描述</h3>
          <p class="description">{{ task.description }}</p>
          <template v-if="task.acceptance_criteria.length">
            <h3>验收标准</h3>
            <ul><li v-for="criterion in task.acceptance_criteria" :key="criterion.id">{{ criterion.content }}</li></ul>
          </template>
        </el-tab-pane>
        <el-tab-pane label="事件" name="events" class="task-pane">
          <el-table :data="events" height="100%" row-key="id">
            <el-table-column label="时间" width="180">
              <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column prop="message" label="事件" />
            <template #empty><el-empty :image-size="72" description="暂无任务事件" /></template>
          </el-table>
        </el-tab-pane>
        <el-tab-pane label="经验复盘" name="review" class="task-pane task-pane--scroll">
          <el-empty v-if="!reviews.length" description="确认完成后按平台开关触发复盘" />
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
import RowMenu from '@/components/astra/RowMenu.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { runTaskAction, taskActions } from '@/features/tasks/actions'
import { statusLabel, statusTagType, formatTime } from '@/utils/format'

const route = useRoute()
const router = useRouter()
const id = computed(() => String(route.params.id))
const task = ref<Task>()
const binding = ref<TaskWorkspaceBinding>()
const events = ref<AstraEvent[]>([])
const reviews = ref<ExperienceReviewJob[]>([])
const loading = ref(true)
const checkingWorkspace = ref(false)
const acting = ref(false)
const error = ref('')
const editing = ref(false)
const tab = ref('workspace')
const actions = computed(() => task.value ? taskActions(task.value) : [])
const primaryAction = computed(() => actions.value.find(item => ['start', 'pause', 'resume'].includes(item.command)))
const secondaryActions = computed(() => actions.value.filter(item => item !== primaryAction.value))
const frameUrl = computed(() => {
  const session = task.value?.current_session_id
  const runtime = binding.value?.runtime
  if (!session || runtime?.session_id !== session || !runtime.workspace_ui) return ''
  return `${runtime.workspace_ui}?task=${encodeURIComponent(id.value)}&session=${encodeURIComponent(session)}`
})
const workspaceMessage = computed(() =>
  task.value?.current_session_id
    ? binding.value?.runtime.workspace_ui_error || '工作区暂不可用，请重试'
    : '任务尚未创建原生会话',
)
let timer: ReturnType<typeof setInterval> | undefined
let updating = false

async function loadWorkspace() {
  const taskId = id.value
  if (!task.value?.current_session_id || checkingWorkspace.value) return
  checkingWorkspace.value = true
  try {
    const result = await api.taskWorkspace(taskId)
    if (id.value === taskId) binding.value = result
  } catch (reason) {
    if (id.value === taskId) ElMessage.error(String(reason))
  } finally {
    checkingWorkspace.value = false
  }
}
async function load() {
  if (updating) return
  updating = true
  const taskId = id.value
  try {
    const [next, history, jobs] = await Promise.all([
      api.task(taskId), api.taskEvents(taskId), api.experienceReviewJobs(taskId),
    ])
    if (id.value !== taskId) return
    const changed = next.current_session_id !== task.value?.current_session_id
    task.value = next
    events.value = history
    reviews.value = jobs
    error.value = ''
    if (changed) void loadWorkspace()
  } catch (reason) {
    if (id.value === taskId) error.value = String(reason)
  } finally {
    updating = false
    loading.value = false
    if (id.value !== taskId && route.name === 'task-detail') void load()
  }
}
function reload() {
  void load()
  void loadWorkspace()
}
function saved() {
  editing.value = false
  void load()
}
async function act(command: string) {
  if (!task.value || acting.value) return
  if (command === 'edit') {
    editing.value = true
    return
  }
  acting.value = true
  try {
    if (await runTaskAction(task.value, command)) {
      if (command === 'delete') await router.push({ name: 'tasks' })
      else if (command === 'reconnect') reload()
      else await load()
    }
  } catch (reason) {
    ElMessage.error(String(reason))
    await load()
  } finally {
    acting.value = false
  }
}
async function retryReview(jobId: string) {
  try {
    await api.retryExperienceReview(jobId)
    await load()
  } catch (reason) {
    ElMessage.error(String(reason))
  }
}
watch(id, () => {
  task.value = undefined
  binding.value = undefined
  loading.value = true
  void load()
})
onMounted(() => {
  void load()
  timer = setInterval(load, 4000)
})
onUnmounted(() => clearInterval(timer))
</script>

<style scoped>
.astra-page > .task-tabs {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}
.task-tabs :deep(.el-tabs__content) { flex: 1; min-height: 0; }
.task-pane { height: 100%; min-height: 0; }
.task-pane--scroll { overflow-y: auto; padding-right: 12px; scrollbar-gutter: stable; }
iframe { display: block; width: 100%; height: 100%; border: 0; }
.description { white-space: pre-wrap; overflow-wrap: anywhere; }
.workspace-loading { color: var(--astra-text-muted); }
</style>
