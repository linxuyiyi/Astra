<template>
  <section class="astra-page task-detail-page" v-loading="loading && !task" data-testid="task-detail">
    <el-alert v-if="error" :title="error" type="error" show-icon :closable="false">
      <el-button @click="reload">重试</el-button>
    </el-alert>

    <template v-if="task">
      <header class="task-header" :aria-label="task.title">
        <div class="task-header__top">
          <div class="task-breadcrumb">
            <button type="button" class="task-back" @click="router.push({ name: 'tasks' })">
              <ArrowLeft :size="14" aria-hidden="true" />
              <span>任务大厅</span>
            </button>
            <span class="task-breadcrumb__sep" aria-hidden="true">/</span>
            <IdDisplay :value="task.id" :max-width="18" />
            <span class="task-chip">{{ taskTypeLabel(task.task_type) }}</span>
            <span v-if="categoryName" class="task-chip">{{ categoryName }}</span>
            <span class="task-priority-chip" :data-priority="task.priority">
              <i class="task-priority-chip__dot" aria-hidden="true" />
              {{ priorityLabel(task.priority) }}优先
            </span>
          </div>

          <div class="task-header__actions">
            <el-button :icon="RefreshCw" :loading="reloading" :disabled="acting" @click="reload">刷新</el-button>
            <el-button
              v-if="primaryAction"
              type="primary"
              :disabled="primaryAction.disabled"
              :loading="acting"
              @click="act(primaryAction.command)"
            >
              {{ primaryAction.label }}
            </el-button>
            <RowMenu
              v-if="secondaryActions.length"
              :aria-label="task.title"
              :items="secondaryActions"
              :disabled="acting"
              @command="act"
            />
          </div>
        </div>

        <div class="task-header__main">
          <div class="task-title-row">
            <h1 class="task-title">{{ task.title }}</h1>
            <SemanticTag :tone="semanticTone(statusTone(task.status))" :label="statusLabel(task.status)" />
          </div>

          <div class="task-context-bar" aria-label="任务执行上下文">
            <span class="ctx-item">
              <i
                class="astra-service-dot"
                :data-state="task.worker_online === true ? 'live' : task.worker_online === false ? 'warning' : 'unknown'"
                aria-hidden="true"
              />
              <span class="ctx-item__label">Worker</span>
              <span class="ctx-item__val">{{ task.worker_name || (task.agent_worker_id ? '不可用' : '未分配') }}</span>
              <span v-if="task.worker_online === false" class="ctx-item__warn">离线</span>
            </span>

            <span v-if="task.current_session_id" class="ctx-item">
              <span class="ctx-item__label">Session</span>
              <IdDisplay :value="task.current_session_id" :max-width="16" />
            </span>

            <span v-if="repo?.name" class="ctx-item">
              <GitBranch :size="13" class="ctx-item__icon" aria-hidden="true" />
              <span class="ctx-item__val" :title="repo.name">{{ repo.name }}</span>
              <span class="ctx-item__branch" :title="repo.branch">{{ repo.branch || '分离 HEAD' }}</span>
              <span v-if="repo.dirty" class="ctx-item__dirty" :title="`工作区有 ${repo.changes} 处未提交更改`">
                ±{{ repo.changes }}
              </span>
            </span>

            <span class="ctx-item ctx-item--time">
              <Clock :size="13" class="ctx-item__icon" aria-hidden="true" />
              <span>更新于 {{ formatTime(task.updated_at) }}</span>
            </span>
          </div>
        </div>
      </header>

      <!-- 待人工介入 / 异常状态焦点条 -->
      <div
        v-if="task.status === 'WAITING'"
        class="attention-strip attention-strip--warning"
        role="status"
      >
        <div class="attention-strip__body">
          <AlertTriangle :size="16" class="attention-strip__icon" aria-hidden="true" />
          <div class="attention-strip__text">
            <strong class="attention-strip__title">{{ waitingInfo.title }}</strong>
            <span class="attention-strip__desc">{{ task.current_progress || waitingInfo.hint }}</span>
          </div>
        </div>
        <div class="attention-strip__actions">
          <el-button
            v-if="canOpenWorkspace && tab !== 'workspace'"
            :type="hasAction('confirm') || hasAction('resume') || hasAction('restart') ? 'default' : 'primary'"
            :icon="Terminal"
            @click="tab = 'workspace'"
          >
            前往工作区处理
          </el-button>
          <span
            v-else-if="canOpenWorkspace && tab === 'workspace' && waitingNeedsWorkspaceReply"
            class="attention-strip__chip"
          >
            请在下方工作区回复
          </span>
          <el-button
            v-if="hasAction('confirm')"
            type="primary"
            :loading="acting"
            @click="act('confirm')"
          >
            确认完成
          </el-button>
          <el-button
            v-if="hasAction('resume')"
            type="primary"
            :loading="acting"
            @click="act('resume')"
          >
            重试启动
          </el-button>
          <el-button
            v-if="hasAction('restart')"
            type="primary"
            :loading="acting"
            @click="act('restart')"
          >
            重新开始
          </el-button>
        </div>
      </div>

      <div
        v-else-if="task.status === 'FAILED' && task.current_progress"
        class="attention-strip attention-strip--danger"
        role="status"
      >
        <div class="attention-strip__body">
          <AlertCircle :size="16" class="attention-strip__icon" aria-hidden="true" />
          <div class="attention-strip__text">
            <strong class="attention-strip__title">执行失败</strong>
            <span class="attention-strip__desc">{{ task.current_progress }}</span>
          </div>
        </div>
        <div v-if="hasAction('restart')" class="attention-strip__actions">
          <el-button type="primary" :loading="acting" @click="act('restart')">重新开始</el-button>
        </div>
      </div>

      <div v-if="task.worker_online === false" class="status-notice" role="status">
        <i class="astra-service-dot" data-state="warning" aria-hidden="true" />
        <span>
          Worker 当前离线<template v-if="task.worker_last_checked_at"> · 最近检查 {{ formatTime(task.worker_last_checked_at) }}</template>
        </span>
        <el-button
          v-if="hasAction('reconnect')"
          link
          type="primary"
          :disabled="acting"
          @click="act('reconnect')"
        >
          重新检查连接
        </el-button>
      </div>

      <div v-if="task.pending_message_id" class="status-notice status-notice--warn" role="status">
        <AlertTriangle :size="14" aria-hidden="true" />
        <span>提交尚未确认，连接恢复后将自动重发。</span>
      </div>

      <el-tabs v-model="tab" class="task-tabs">
        <!-- Tab 1: 原生工作区 -->
        <el-tab-pane label="原生工作区" name="workspace" class="task-pane">
          <iframe
            v-if="frameUrl"
            :key="frameUrl"
            :src="frameUrl"
            title="OpenCode 任务工作区"
            allow="clipboard-read; clipboard-write"
          />
          <div v-else-if="checkingWorkspace" class="workspace-state">
            <span class="workspace-state__spinner" aria-hidden="true" />
            <p class="workspace-state__text">正在连接原生工作区…</p>
          </div>
          <div v-else class="workspace-state">
            <el-empty :image-size="68" :description="workspaceMessage">
              <div class="workspace-state__actions">
                <el-button
                  v-if="task.waiting_reason === 'session_missing' && hasAction('restart')"
                  type="primary"
                  :loading="acting"
                  @click="act('restart')"
                >
                  重新开始
                </el-button>
                <el-button
                  v-else-if="hasAction('start')"
                  type="primary"
                  :loading="acting"
                  @click="act('start')"
                >
                  开始执行
                </el-button>
                <el-button
                  v-else-if="task.current_session_id && task.waiting_reason !== 'session_missing'"
                  @click="reload"
                >
                  重试连接
                </el-button>
              </div>
            </el-empty>
          </div>
        </el-tab-pane>

        <!-- Tab 2: 任务详情（Linear 双栏文档阅读视图） -->
        <el-tab-pane label="任务详情" name="details" class="task-pane task-pane--scroll">
          <div class="detail-grid">
            <div class="detail-main">
              <section class="doc-section">
                <div class="doc-section__head">
                  <h2>任务描述</h2>
                </div>
                <div class="doc-card">
                  <MarkdownView
                    :content="task.description || ''"
                    show-toc
                    allow-source-toggle
                    allow-copy
                    empty-text="暂无任务描述"
                  />
                </div>
              </section>

              <section v-if="task.current_progress" class="doc-section">
                <div class="doc-section__head">
                  <h2>当前进展</h2>
                </div>
                <div class="doc-card doc-card--subtle">
                  <MarkdownView :content="task.current_progress" compact allow-copy />
                </div>
              </section>

              <section v-if="task.acceptance_criteria.length" class="doc-section">
                <div class="doc-section__head">
                  <h2>验收标准</h2>
                  <span class="count-pill">{{ task.acceptance_criteria.length }}</span>
                </div>
                <ol class="criteria-list">
                  <li v-for="(criterion, index) in task.acceptance_criteria" :key="criterion.id" class="criteria-item">
                    <span class="criteria-item__index">{{ String(index + 1).padStart(2, '0') }}</span>
                    <div class="criteria-item__content">
                      <MarkdownView :content="criterion.content" compact />
                    </div>
                  </li>
                </ol>
              </section>

              <section v-if="task.attachments.length" class="doc-section">
                <div class="doc-section__head">
                  <h2>附件</h2>
                  <span class="count-pill">{{ task.attachments.length }}</span>
                </div>
                <div class="attachment-grid">
                  <div v-for="(item, index) in task.attachments" :key="index" class="attachment-chip">
                    <Paperclip :size="14" aria-hidden="true" />
                    <span class="attachment-chip__name" :title="attachmentName(item)">{{ attachmentName(item) }}</span>
                    <span v-if="attachmentSize(item)" class="attachment-chip__size">{{ attachmentSize(item) }}</span>
                  </div>
                </div>
              </section>
            </div>

            <aside class="detail-rail" aria-label="任务属性">
              <section class="meta-card">
                <h3 class="meta-card__title">任务属性</h3>
                <dl class="meta-list">
                  <div class="meta-row">
                    <dt>状态</dt>
                    <dd><SemanticTag :tone="semanticTone(statusTone(task.status))" :label="statusLabel(task.status)" /></dd>
                  </div>
                  <div class="meta-row">
                    <dt>类型</dt>
                    <dd><el-tag size="small" type="info" class="astra-tag--neutral">{{ taskTypeLabel(task.task_type) }}</el-tag></dd>
                  </div>
                  <div class="meta-row">
                    <dt>优先级</dt>
                    <dd>
                      <span class="task-priority-chip" :data-priority="task.priority">
                        <i class="task-priority-chip__dot" aria-hidden="true" />
                        {{ priorityLabel(task.priority) }}
                      </span>
                    </dd>
                  </div>
                  <div class="meta-row">
                    <dt>分类</dt>
                    <dd>{{ categoryName || '—' }}</dd>
                  </div>
                  <div class="meta-row">
                    <dt>来源</dt>
                    <dd>{{ sourceLabel(task.source) }}</dd>
                  </div>
                  <div v-if="task.external_task_id" class="meta-row">
                    <dt>外部单号</dt>
                    <dd><IdDisplay :value="task.external_task_id" :max-width="18" /></dd>
                  </div>
                </dl>
              </section>

              <section class="meta-card">
                <h3 class="meta-card__title">运行环境</h3>
                <dl class="meta-list">
                  <div class="meta-row">
                    <dt>Worker</dt>
                    <dd class="meta-val--worker">
                      <i
                        class="astra-service-dot"
                        :data-state="task.worker_online === true ? 'live' : task.worker_online === false ? 'warning' : 'unknown'"
                        aria-hidden="true"
                      />
                      <span>{{ task.worker_name || (task.agent_worker_id ? '不可用' : '未分配') }}</span>
                    </dd>
                  </div>
                  <div class="meta-row">
                    <dt>Task ID</dt>
                    <dd><IdDisplay :value="task.id" :max-width="18" /></dd>
                  </div>
                  <div class="meta-row">
                    <dt>Session</dt>
                    <dd>
                      <IdDisplay v-if="task.current_session_id" :value="task.current_session_id" :max-width="18" />
                      <span v-else class="is-muted">—</span>
                    </dd>
                  </div>
                  <div class="meta-row">
                    <dt>Git 仓库</dt>
                    <dd :title="repo?.name">{{ repo?.name || (repoError ? '暂不可读' : '—') }}</dd>
                  </div>
                  <div class="meta-row">
                    <dt>当前分支</dt>
                    <dd class="meta-val--mono" :title="repo?.branch">
                      {{ repo?.branch || (repo?.commit ? '分离 HEAD' : '—') }}
                      <span v-if="repo?.dirty" class="ctx-item__dirty">±{{ repo.changes }}</span>
                    </dd>
                  </div>
                  <div v-if="task.workspace" class="meta-row">
                    <dt>工作目录</dt>
                    <dd class="meta-val--mono" :title="task.workspace">{{ task.workspace }}</dd>
                  </div>
                </dl>
              </section>

              <section class="meta-card">
                <h3 class="meta-card__title">时间记录</h3>
                <dl class="meta-list meta-list--tabular">
                  <div class="meta-row">
                    <dt>创建时间</dt>
                    <dd>{{ formatTime(task.created_at) }}</dd>
                  </div>
                  <div v-if="task.started_at" class="meta-row">
                    <dt>开始时间</dt>
                    <dd>{{ formatTime(task.started_at) }}</dd>
                  </div>
                  <div v-if="task.started_at" class="meta-row">
                    <dt>运行耗时</dt>
                    <dd>{{ formatDuration(task.started_at, task.finished_at) }}</dd>
                  </div>
                  <div v-if="task.last_activity_at" class="meta-row">
                    <dt>最近活动</dt>
                    <dd>{{ formatTime(task.last_activity_at) }}</dd>
                  </div>
                  <div v-if="task.finished_at" class="meta-row">
                    <dt>完成时间</dt>
                    <dd>{{ formatTime(task.finished_at) }}</dd>
                  </div>
                  <div class="meta-row">
                    <dt>更新时间</dt>
                    <dd>{{ formatTime(task.updated_at) }}</dd>
                  </div>
                </dl>
              </section>
            </aside>
          </div>
        </el-tab-pane>

        <!-- Tab 3: 事件 -->
        <el-tab-pane label="事件" name="events" class="task-pane task-pane--events">
          <el-alert v-if="contentError" class="pane-alert" :title="contentError" type="error" :closable="false">
            <el-button @click="loadTab()">重试</el-button>
          </el-alert>

          <div class="events-bar">
            <div class="events-bar__info">
              <span class="events-bar__count">共 {{ events.length }} 条</span>
              <el-tag v-if="browsingEvents" size="small" type="info" class="astra-tag--neutral">历史分页</el-tag>
            </div>
            <div class="events-bar__actions">
              <el-button :disabled="!hasOlderEvents" @click="loadTab('older')">更早事件</el-button>
              <el-button v-if="browsingEvents" @click="loadTab('latest')">返回最新</el-button>
            </div>
          </div>

          <div class="events-table-wrap">
            <el-table :data="events" height="100%" row-key="id" aria-label="任务事件列表">
              <el-table-column label="时间" width="152">
                <template #default="{ row }">
                  <span class="mono-time">{{ formatTime(row.created_at) }}</span>
                </template>
              </el-table-column>
              <el-table-column label="阶段" width="156" show-overflow-tooltip>
                <template #default="{ row }">
                  <el-tag size="small" type="info" class="astra-tag--neutral">
                    {{ row.phase || row.event_type || '事件' }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="事件内容" min-width="320">
                <template #default="{ row }">
                  <div class="event-msg">
                    <p class="event-msg__text">{{ row.message }}</p>
                    <details v-if="hasEventDetail(row)" class="event-detail">
                      <summary class="event-detail__toggle">查看结构化明细</summary>
                      <pre class="event-detail__code">{{ formatEventDetail(row.detail) }}</pre>
                    </details>
                  </div>
                </template>
              </el-table-column>
              <template #empty>
                <el-empty :image-size="68" description="暂无任务事件" />
              </template>
            </el-table>
          </div>
        </el-tab-pane>

        <!-- Tab 4: 经验复盘 -->
        <el-tab-pane label="经验复盘" name="review" class="task-pane task-pane--scroll">
          <el-alert v-if="contentError" class="pane-alert" :title="contentError" type="error" :closable="false">
            <el-button @click="loadTab()">重试</el-button>
          </el-alert>

          <el-empty v-else-if="!reviews.length" :image-size="72" description="任务确认完成后将按平台开关自动触发复盘" />

          <div v-else class="review-list">
            <article v-for="job in reviews" :key="job.id" class="review-card">
              <header class="review-card__head">
                <div class="review-card__status">
                  <SemanticTag :tone="reviewStatusMeta(job.status).tone" :label="reviewStatusMeta(job.status).label" />
                  <el-tag v-if="job.phase" size="small" type="info" class="astra-tag--neutral">{{ job.phase }}</el-tag>
                  <IdDisplay :value="job.id" :max-width="16" />
                </div>
                <div class="review-card__head-right">
                  <span class="review-card__time">
                    {{ job.finished_at ? `完成于 ${formatTime(job.finished_at)}` : `创建于 ${formatTime(job.created_at)}` }}
                  </span>
                  <el-button
                    v-if="job.status === 'FAILED' || job.status === 'RETRY_WAIT'"
                    size="small"
                    type="primary"
                    plain
                    @click="retryReview(job.id)"
                  >
                    重试复盘
                  </el-button>
                </div>
              </header>

              <div class="review-metrics">
                <div class="review-metric">
                  <span class="review-metric__label">轨迹消息</span>
                  <strong class="review-metric__val">{{ job.source_message_count }}</strong>
                </div>
                <div class="review-metric">
                  <span class="review-metric__label">切片进度</span>
                  <strong class="review-metric__val">{{ job.completed_segment_count }} / {{ job.segment_count }}</strong>
                </div>
                <div class="review-metric">
                  <span class="review-metric__label">尝试次数</span>
                  <strong class="review-metric__val">{{ job.attempts }}</strong>
                </div>
                <div class="review-metric">
                  <span class="review-metric__label">候选知识</span>
                  <strong class="review-metric__val">{{ job.candidate_ids.length }}</strong>
                </div>
              </div>

              <div v-if="job.error_message" class="review-note review-note--error">
                <div class="review-note__head">
                  <span>失败说明</span>
                  <span v-if="job.error_code" class="code-chip">{{ job.error_code }}</span>
                </div>
                <MarkdownView :content="job.error_message" compact allow-copy />
              </div>

              <div v-else-if="job.zero_reason" class="review-note">
                <div class="review-note__head">
                  <span>复盘说明</span>
                </div>
                <MarkdownView :content="job.zero_reason" compact />
              </div>

              <div v-if="job.candidate_ids.length" class="review-candidates">
                <div class="review-candidates__label">产出候选经验</div>
                <div class="review-candidates__grid">
                  <router-link
                    v-for="candidateId in job.candidate_ids"
                    :key="candidateId"
                    class="candidate-link"
                    :to="`/library?knowledge=${candidateId}`"
                  >
                    <BookOpen :size="14" aria-hidden="true" />
                    <span class="candidate-link__id">{{ candidateId }}</span>
                    <ExternalLink :size="12" class="candidate-link__ext" aria-hidden="true" />
                  </router-link>
                </div>
              </div>
            </article>
          </div>
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
import {
  AlertCircle,
  AlertTriangle,
  ArrowLeft,
  BookOpen,
  Clock,
  ExternalLink,
  GitBranch,
  Paperclip,
  RefreshCw,
  Terminal,
} from 'lucide-vue-next'
import { api } from '@/api/client'
import type { AstraEvent, ExperienceReviewJob, Task, TaskWorkspaceBinding } from '@/api/types'
import IdDisplay from '@/components/astra/IdDisplay.vue'
import MarkdownView from '@/components/astra/MarkdownView.vue'
import RowMenu from '@/components/astra/RowMenu.vue'
import SemanticTag from '@/components/astra/SemanticTag.vue'
import TaskCreateDrawer from '@/components/task/TaskCreateDrawer.vue'
import { runTaskAction, taskActions } from '@/features/tasks/actions'
import { semanticTone, type SemanticTone } from '@/features/workers/semantic'
import { useAstraStore } from '@/stores/astra'
import {
  formatDuration,
  formatTime,
  priorityLabel,
  sourceLabel,
  statusLabel,
  statusTone,
  taskTypeLabel,
} from '@/utils/format'

const route = useRoute()
const router = useRouter()
const store = useAstraStore()

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
const reloading = ref(false)
const checkingWorkspace = ref(false)
const acting = ref(false)
const error = ref('')
const editing = ref(false)

const tabs = ['workspace', 'details', 'events', 'review']
const tab = computed({
  get: () => (tabs.includes(String(route.query.tab)) ? String(route.query.tab) : 'workspace'),
  set: (value) => {
    if (value !== tab.value) void router.replace({ query: { ...route.query, tab: value } })
  },
})

const repo = ref<Awaited<ReturnType<typeof api.taskRepo>>>()
const repoError = ref(false)
const workspaceError = ref('')
const contentError = ref('')

const categoryName = computed(() => {
  const categoryId = task.value?.category_id
  if (!categoryId) return ''
  return store.categories.find((item) => item.id === categoryId)?.name ?? ''
})

const actions = computed(() => (task.value ? taskActions(task.value) : []))
const primaryAction = computed(() =>
  actions.value.find((item) => ['start', 'pause', 'resume', 'confirm', 'restart'].includes(item.command)),
)
const secondaryActions = computed(() =>
  actions.value.filter(
    (item) => item !== primaryAction.value && (item.command !== 'workspace' || tab.value !== 'workspace'),
  ),
)

function hasAction(command: string) {
  return actions.value.some((item) => item.command === command && !item.disabled)
}

const canOpenWorkspace = computed(() =>
  Boolean(task.value?.current_session_id && task.value.waiting_reason !== 'session_missing'),
)

const waitingNeedsWorkspaceReply = computed(() =>
  ['question', 'permission', 'intervention', 'paused'].includes(task.value?.waiting_reason ?? ''),
)

const WAITING_INFO: Record<string, { title: string; hint: string }> = {
  confirmation: { title: '待确认完成', hint: '请核对工作区产出与验收标准后确认完成。' },
  question: { title: '等待人工回复', hint: 'Worker 发起了提问，请在原生工作区答复后继续。' },
  permission: { title: '等待操作授权', hint: 'Worker 请求操作权限，请在原生工作区确认或拒绝。' },
  intervention: { title: '需要人工介入', hint: '请在原生工作区查看上下文并给出处理指示。' },
  paused: { title: '执行已暂停', hint: '可在原生工作区发送新指令继续执行。' },
  session_missing: { title: '原生会话已不存在', hint: '远端 Session 已丢失，请重新开始以创建新会话。' },
  start_error: { title: '启动异常', hint: '任务启动失败，可检查 Worker 连接后重试启动。' },
  input_rejected: { title: '输入未被接受', hint: '远端拒绝了当前指令，请核对后重试。' },
  result_pending: { title: '等待结果同步', hint: '正在等待执行结果回传，可稍后刷新状态。' },
}

const waitingInfo = computed(() =>
  WAITING_INFO[task.value?.waiting_reason ?? ''] ?? {
    title: '等待人工处理',
    hint: '请查看原生工作区或任务进展后继续操作。',
  },
)

const REVIEW_STATUS_META: Record<string, { label: string; tone: SemanticTone }> = {
  PENDING: { label: '排队中', tone: 'neutral' },
  COLLECTING: { label: '采集轨迹', tone: 'progress' },
  ANALYZING: { label: '分析经验', tone: 'progress' },
  VALIDATING: { label: '校验依据', tone: 'progress' },
  WRITING: { label: '写入候选', tone: 'progress' },
  SUCCEEDED: { label: '复盘完成', tone: 'success' },
  NO_FINDINGS: { label: '无新增经验', tone: 'neutral' },
  RETRY_WAIT: { label: '等待重试', tone: 'attention' },
  FAILED: { label: '复盘失败', tone: 'danger' },
  SUPERSEDED: { label: '已替代', tone: 'neutral' },
  SKIPPED_SOURCE_MISSING: { label: '轨迹缺失跳过', tone: 'neutral' },
  SKIPPED_TOO_LARGE: { label: '轨迹超限跳过', tone: 'neutral' },
}

function reviewStatusMeta(status: string): { label: string; tone: SemanticTone } {
  return REVIEW_STATUS_META[status] ?? { label: status, tone: 'neutral' }
}

const frameUrl = computed(() => {
  const session = task.value?.current_session_id
  const runtime = binding.value?.runtime
  if (!session || task.value?.waiting_reason === 'session_missing' || runtime?.session_id !== session || !runtime.workspace_ui) {
    return ''
  }
  return `${runtime.workspace_ui}?task=${encodeURIComponent(id.value)}&session=${encodeURIComponent(session)}`
})

const workspaceMessage = computed(() => {
  if (task.value?.waiting_reason === 'session_missing') return '原生 Session 已不存在，请重新开始'
  if (task.value?.worker_online === false) return 'Worker 离线，等待连接恢复'
  if (!task.value?.current_session_id) return '任务尚未创建原生会话'
  return workspaceError.value || binding.value?.runtime.workspace_ui_error || '工作区暂不可用，请重试'
})

function attachmentName(item: Record<string, unknown>) {
  return String(item.name || item.path || '附件')
}

function attachmentSize(item: Record<string, unknown>) {
  const bytes = Number(item.size)
  if (!Number.isFinite(bytes) || bytes <= 0) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
}

function hasEventDetail(event: AstraEvent) {
  return Boolean(event.detail && Object.keys(event.detail).length > 0)
}

function formatEventDetail(detail: Record<string, unknown>) {
  try {
    return JSON.stringify(detail, null, 2)
  } catch {
    return String(detail)
  }
}

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

async function reload() {
  reloading.value = true
  try {
    await Promise.allSettled([refreshTask(), loadWorkspace(), loadRepo(), loadTab()])
  } finally {
    reloading.value = false
  }
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

watch(tab, (name) => {
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
.task-detail-page {
  gap: 10px;
}

/* ── 顶部身份头（Linear 极简紧凑两行） ────────────────────────────────── */
.task-header {
  display: grid;
  gap: 8px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--astra-border-subtle);
}

.task-header__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
}

.task-breadcrumb {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  min-width: 0;
}

.task-back {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  height: 26px;
  padding: 0 8px 0 4px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--astra-text-secondary);
  font-family: inherit;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: background-color 0.14s ease, color 0.14s ease;
}

.task-back:hover {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
}

.task-breadcrumb__sep {
  color: var(--astra-border-hover);
  font-size: 13px;
}

.task-chip {
  display: inline-flex;
  align-items: center;
  height: 21px;
  padding: 0 7px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

.task-priority-chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

.task-priority-chip__dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: #a1a1aa;
}

.task-priority-chip[data-priority='VERY_HIGH'] { color: var(--astra-danger); }
.task-priority-chip[data-priority='VERY_HIGH'] .task-priority-chip__dot { background: var(--astra-danger); }
.task-priority-chip[data-priority='HIGH'] { color: var(--astra-warning); }
.task-priority-chip[data-priority='HIGH'] .task-priority-chip__dot { background: var(--astra-warning); }

.task-header__actions {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
}

.task-header__actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.task-header__main {
  display: grid;
  gap: 6px;
}

.task-title-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  min-width: 0;
}

.task-title {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 19px;
  font-weight: 600;
  line-height: 1.35;
  letter-spacing: -0.01em;
  word-break: break-word;
}

.task-context-bar {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px 16px;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.ctx-item {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  min-width: 0;
}

.ctx-item__label {
  color: var(--astra-text-muted);
}

.ctx-item__val {
  max-width: 180px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-secondary);
  font-weight: 500;
}

.ctx-item__warn {
  padding: 0 5px;
  border-radius: 4px;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  font-size: 11px;
  font-weight: 500;
}

.ctx-item__icon {
  color: var(--astra-text-muted);
  flex: none;
}

.ctx-item__branch {
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 1px 6px;
  border-radius: 4px;
  background: var(--astra-bg-subtle);
  border: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11.5px;
}

.ctx-item__dirty {
  padding: 1px 5px;
  border-radius: 4px;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.ctx-item--time {
  font-variant-numeric: tabular-nums;
}

/* ── 待人工介入 / 异常焦点条 ─────────────────────────────────────────── */
.attention-strip {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 10px 16px;
  padding: 10px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}

.attention-strip--warning {
  border-color: color-mix(in srgb, var(--astra-warning) 28%, var(--astra-border));
  background: var(--astra-warning-soft);
}

.attention-strip--warning .attention-strip__icon,
.attention-strip--warning .attention-strip__title {
  color: var(--astra-warning);
}

.attention-strip--danger {
  border-color: color-mix(in srgb, var(--astra-danger) 28%, var(--astra-border));
  background: var(--astra-danger-soft);
}

.attention-strip--danger .attention-strip__icon,
.attention-strip--danger .attention-strip__title {
  color: var(--astra-danger);
}

.attention-strip__body {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 0;
  flex: 1 1 260px;
}

.attention-strip__icon {
  margin-top: 2px;
  flex: none;
}

.attention-strip__text {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 4px 10px;
  min-width: 0;
}

.attention-strip__title {
  font-size: 13.5px;
  font-weight: 600;
  white-space: nowrap;
}

.attention-strip__desc {
  color: var(--astra-text-secondary);
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.attention-strip__actions {
  display: flex;
  align-items: center;
  flex: none;
  gap: 8px;
}

.attention-strip__actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.attention-strip__chip {
  padding: 3px 9px;
  border: 1px solid color-mix(in srgb, var(--astra-warning) 30%, transparent);
  border-radius: 6px;
  background: rgba(255, 255, 255, 0.72);
  color: var(--astra-warning);
  font-size: 12px;
  font-weight: 500;
}

.status-notice {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 7px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12.5px;
}

.status-notice--warn {
  border-color: color-mix(in srgb, var(--astra-warning) 26%, var(--astra-border));
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

/* ── Tabs 主体容器 ──────────────────────────────────────────────────── */
.astra-page > .task-tabs {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  min-height: 0;
}

.task-tabs :deep(.el-tabs__header) {
  margin-bottom: 10px;
}

.task-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  border: 1px solid var(--astra-border);
  border-radius: 12px;
  background: var(--astra-bg-panel);
}

.task-pane {
  height: 100%;
  min-height: 0;
}

.task-pane--scroll {
  overflow-y: auto;
  padding: 20px 22px;
  scrollbar-gutter: stable;
}

iframe {
  display: block;
  width: 100%;
  height: 100%;
  border: 0;
}

/* ── 工作区加载 / 空态 ──────────────────────────────────────────────── */
.workspace-state {
  display: grid;
  place-items: center;
  height: 100%;
  padding: 24px;
  background: var(--astra-bg-subtle);
}

.workspace-state__spinner {
  width: 22px;
  height: 22px;
  border: 2px solid var(--astra-border);
  border-top-color: var(--astra-primary);
  border-radius: 50%;
  animation: task-spin 0.8s linear infinite;
}

.workspace-state__text {
  margin: 10px 0 0;
  color: var(--astra-text-muted);
  font-size: 13px;
}

.workspace-state__actions {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
}

@keyframes task-spin {
  to { transform: rotate(360deg); }
}

/* ── 任务详情（Linear 双栏阅读流） ───────────────────────────────────── */
.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 292px;
  gap: 24px;
  align-items: start;
  max-width: 1220px;
}

.detail-main {
  display: grid;
  gap: 22px;
  min-width: 0;
}

.doc-section {
  display: grid;
  gap: 10px;
  min-width: 0;
}

.doc-section__head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.doc-section__head h2 {
  margin: 0;
  color: var(--astra-text-primary);
  font-size: 14px;
  font-weight: 600;
  letter-spacing: -0.01em;
}

.count-pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 20px;
  height: 19px;
  padding: 0 6px;
  border-radius: 999px;
  background: var(--astra-bg-elevated);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.doc-card {
  padding: 16px 18px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
  min-width: 0;
}

.doc-card--subtle {
  background: var(--astra-bg-subtle);
}

.criteria-list {
  display: grid;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.criteria-item {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 10px;
  align-items: start;
  padding: 10px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  background: var(--astra-bg-subtle);
}

.criteria-item__index {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  height: 20px;
  padding: 0 6px;
  margin-top: 2px;
  border: 1px solid var(--astra-border);
  border-radius: 5px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.criteria-item__content {
  min-width: 0;
}

.attachment-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.attachment-chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  max-width: 280px;
  padding: 6px 11px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-size: 12.5px;
}

.attachment-chip__name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-primary);
}

.attachment-chip__size {
  flex: none;
  color: var(--astra-text-muted);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

/* ── 右侧属性栏 ─────────────────────────────────────────────────────── */
.detail-rail {
  display: grid;
  gap: 14px;
  min-width: 0;
}

.meta-card {
  padding: 14px 16px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-subtle);
}

.meta-card__title {
  margin: 0 0 10px;
  padding-bottom: 8px;
  border-bottom: 1px solid var(--astra-border-subtle);
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.02em;
}

.meta-list {
  display: grid;
  gap: 9px;
  margin: 0;
}

.meta-list--tabular dd {
  font-variant-numeric: tabular-nums;
}

.meta-row {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  align-items: center;
  gap: 8px;
  font-size: 13px;
}

.meta-row dt {
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.meta-row dd {
  margin: 0;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-primary);
}

.meta-val--worker {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.meta-val--mono {
  font-family: var(--astra-font-mono);
  font-size: 12px;
  color: var(--astra-text-secondary);
}

.is-muted {
  color: var(--astra-text-muted);
}

/* ── 事件 Tab ───────────────────────────────────────────────────────── */
.task-pane--events {
  display: flex;
  flex-direction: column;
}

.pane-alert {
  margin: 12px 16px 0;
}

.events-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--astra-border-subtle);
  background: var(--astra-bg-subtle);
}

.events-bar__info {
  display: flex;
  align-items: center;
  gap: 8px;
}

.events-bar__count {
  color: var(--astra-text-muted);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

.events-bar__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.events-bar__actions :deep(.el-button + .el-button) {
  margin-left: 0;
}

.events-table-wrap {
  flex: 1 1 auto;
  min-height: 0;
}

.mono-time {
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

.event-msg {
  display: grid;
  gap: 6px;
  padding: 2px 0;
}

.event-msg__text {
  margin: 0;
  color: var(--astra-text-primary);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.event-detail {
  min-width: 0;
}

.event-detail__toggle {
  display: inline-block;
  color: var(--astra-text-muted);
  font-size: 12px;
  cursor: pointer;
  user-select: none;
}

.event-detail__toggle:hover {
  color: var(--astra-text-primary);
}

.event-detail__code {
  margin: 6px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 12px;
  line-height: 1.55;
  overflow-x: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

/* ── 经验复盘 Tab ───────────────────────────────────────────────────── */
.review-list {
  display: grid;
  gap: 14px;
  max-width: 960px;
}

.review-card {
  display: grid;
  gap: 14px;
  padding: 16px 18px;
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
}

.review-card__head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 16px;
}

.review-card__status {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.review-card__head-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.review-card__time {
  color: var(--astra-text-muted);
  font-size: 12.5px;
  font-variant-numeric: tabular-nums;
}

.review-metrics {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}

.review-metric {
  display: grid;
  gap: 2px;
  padding: 9px 12px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}

.review-metric__label {
  color: var(--astra-text-muted);
  font-size: 11.5px;
}

.review-metric__val {
  color: var(--astra-text-primary);
  font-family: var(--astra-font-mono);
  font-size: 14px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}

.review-note {
  display: grid;
  gap: 8px;
  padding: 12px 14px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
}

.review-note--error {
  border-color: color-mix(in srgb, var(--astra-danger) 28%, var(--astra-border));
  background: var(--astra-danger-soft);
}

.review-note__head {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--astra-text-secondary);
  font-size: 12px;
  font-weight: 600;
}

.review-note--error .review-note__head {
  color: var(--astra-danger);
}

.code-chip {
  padding: 1px 6px;
  border: 1px solid var(--astra-border);
  border-radius: 4px;
  background: var(--astra-bg-panel);
  color: var(--astra-text-secondary);
  font-family: var(--astra-font-mono);
  font-size: 11px;
  font-weight: 500;
}

.review-candidates {
  display: grid;
  gap: 8px;
}

.review-candidates__label {
  color: var(--astra-text-muted);
  font-size: 12px;
  font-weight: 600;
}

.review-candidates__grid {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.candidate-link {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  border: 1px solid var(--astra-border);
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  color: var(--astra-accent-text);
  font-size: 12.5px;
  text-decoration: none;
  transition: border-color 0.14s ease, background-color 0.14s ease;
}

.candidate-link:hover {
  border-color: var(--astra-accent-border);
  background: var(--astra-accent-soft);
}

.candidate-link__id {
  font-family: var(--astra-font-mono);
  font-size: 12px;
}

.candidate-link__ext {
  color: var(--astra-text-muted);
}

@media (max-width: 980px) {
  .detail-grid {
    grid-template-columns: minmax(0, 1fr);
  }
  .review-metrics {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
</style>
