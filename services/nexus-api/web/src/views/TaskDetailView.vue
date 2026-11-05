<template>
  <div class="task-page" v-loading="loading">
    <el-alert v-if="error" :title="error" type="error" show-icon
      ><el-button @click="load">重试</el-button></el-alert
    >
    <template v-if="task">
      <div class="toolbar">
        <el-button @click="router.push('/tasks')">返回</el-button>
        <h2>{{ task.title }}</h2>
        <el-tag>{{ statusLabel(task.status) }}</el-tag>
        <el-button v-if="task.status === 'PENDING'" @click="act('start')"
          >开始</el-button
        >
        <el-button v-if="task.status === 'RUNNING'" @click="act('pause')"
          >停止回复</el-button
        >
        <el-button v-if="task.status === 'WAITING'" @click="act('resume')"
          >继续</el-button
        >
        <el-button
          v-if="task.status === 'WAITING' && !task.pending_message_id"
          type="success"
          @click="act('confirm')"
          >确认完成</el-button
        >
        <el-button
          v-if="['RUNNING', 'WAITING', 'PENDING'].includes(task.status)"
          @click="cancel"
          >取消任务</el-button
        >
        <el-button
          v-if="['SUCCEEDED', 'CANCELED', 'WAITING'].includes(task.status)"
          @click="restart"
          >重新开始</el-button
        >
        <el-button
          v-if="['RUNNING', 'WAITING'].includes(task.status)"
          type="danger"
          plain
          @click="forceStop"
          >确认强制停止</el-button
        >
      </div>
      <p>
        {{ task.current_progress }} · Worker：{{
          task.agent_worker_id || '未分配'
        }}
      </p>
      <el-alert
        v-if="task.pending_message_id"
        title="提交尚未确认，连接恢复后自动重发；可能重复执行。"
        type="warning"
        :closable="false"
      />
      <el-tabs v-model="tab">
        <el-tab-pane label="原生工作区" name="workspace">
          <iframe
            v-if="frameUrl"
            :key="task.current_session_id || ''"
            :src="frameUrl"
            title="OpenCode Task Workspace"
            allow="clipboard-read; clipboard-write"
          />
          <el-empty
            v-else
            :description="
              binding?.runtime.workspace_ui_error || '任务尚未创建原生会话'
            "
          />
        </el-tab-pane>
        <el-tab-pane label="任务详情" name="details"
          ><p class="description">{{ task.description }}</p>
          <p v-for="criterion in task.acceptance_criteria" :key="criterion.id">
            {{ criterion.id }} · {{ criterion.content }}
          </p>
          <el-button
            v-if="['PENDING', 'WAITING', 'RUNNING'].includes(task.status)"
            @click="editing = true"
            >编辑</el-button
          ></el-tab-pane
        >
        <el-tab-pane label="事件" name="events"
          ><el-table :data="events"
            ><el-table-column label="时间" width="180"
              ><template #default="{ row }">{{
                formatTime(row.created_at)
              }}</template></el-table-column
            ><el-table-column prop="message" label="事件" /></el-table
        ></el-tab-pane>
        <el-tab-pane label="经验复盘" name="review"
          ><el-empty
            v-if="!reviews.length"
            description="确认完成后按平台开关触发复盘"
          /><el-card v-for="job in reviews" :key="job.id"
            ><p>{{ job.status }} · {{ job.phase }}</p>
            <p>{{ job.error_message || job.zero_reason }}</p>
            <router-link
              v-for="id in job.candidate_ids"
              :key="id"
              :to="`/library?knowledge=${id}`"
              >查看候选 {{ id }} </router-link
            ><el-button
              v-if="job.status === 'FAILED' || job.status === 'RETRY_WAIT'"
              @click="retryReview(job.id)"
              >重试</el-button
            ></el-card
          ></el-tab-pane
        >
      </el-tabs>
      <TaskCreateDrawer
        :open="editing"
        :task="task"
        @close="editing = false"
        @saved="configurationSaved"
      />
    </template>
  </div>
</template>
<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { api } from '@/api/client'
import type {
  Task,
  TaskWorkspaceBinding,
  AstraEvent,
  ExperienceReviewJob,
} from '@/api/types'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { statusLabel, formatTime } from '@/utils/format'
const route = useRoute(),
  router = useRouter(),
  task = ref<Task>(),
  binding = ref<TaskWorkspaceBinding>(),
  events = ref<AstraEvent[]>([]),
  reviews = ref<ExperienceReviewJob[]>([]),
  loading = ref(false),
  error = ref(''),
  editing = ref(false),
  tab = ref('workspace')
const id = () => String(route.params.id)
const frameUrl = computed(() =>
  binding.value?.runtime.workspace_ui && task.value?.current_session_id
    ? `${binding.value.runtime.workspace_ui}?task=${encodeURIComponent(id())}&session=${encodeURIComponent(task.value.current_session_id)}`
    : '',
)
let timer: ReturnType<typeof setInterval> | undefined
let updating = false
function configurationSaved() {
  editing.value = false
  void load()
}
async function load() {
  if (updating) return
  updating = true
  try {
    const data = await Promise.all([
      api.task(id()),
      api.taskWorkspace(id()),
      api.taskEvents(id()),
      api.experienceReviewJobs(id()),
    ])
    ;[task.value, binding.value, events.value, reviews.value] = data
    error.value = ''
  } catch (e) {
    error.value = String(e)
  } finally {
    updating = false
    loading.value = false
  }
}
async function act(action: string, body: Record<string, unknown> = {}) {
  loading.value = true
  try {
    task.value = await api.taskAction(id(), action, body)
    await load()
  } catch (e) {
    error.value = String(e)
  } finally {
    loading.value = false
  }
}
async function cancel() {
  try {
    await ElMessageBox.confirm(
      '确认停止原生执行并取消任务？连接失败时保留资源占用。',
      '取消任务',
    )
    await act('stop')
  } catch {}
}
async function restart() {
  try {
    await ElMessageBox.confirm(
      '停止当前执行并创建新 Session？复盘仅使用之后可定位的当前 Session。',
      '重新开始',
    )
    await act('restart')
  } catch {}
}
async function forceStop() {
  try {
    const result = await ElMessageBox.prompt(
      '仅在已确认执行停止，或明确接受原任务仍可能写入工作区的后果时使用。请输入原因。',
      '强制释放',
      { inputValidator: (v) => Boolean(v?.trim()) },
    )
    await act('confirm-execution-stop', { reason: result.value })
  } catch {}
}
async function retryReview(jobId: string) {
  try {
    await api.retryExperienceReview(jobId)
    await load()
  } catch (e) {
    error.value = String(e)
  }
}
onMounted(() => {
  loading.value = true
  void load()
  timer = setInterval(load, 4000)
})
onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>
<style scoped>
.task-page {
  padding: 20px;
  height: 100%;
  overflow: auto;
}
.toolbar {
  display: flex;
  align-items: center;
  gap: 10px;
}
.toolbar h2 {
  flex: 1;
  font-size: 20px;
}
iframe {
  width: 100%;
  height: calc(100vh - 240px);
  border: 0;
}
.description {
  white-space: pre-wrap;
}
.el-card {
  margin: 12px 0;
}
</style>
