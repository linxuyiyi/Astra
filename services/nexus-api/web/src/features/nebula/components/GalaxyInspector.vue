<template>
  <aside class="galaxy-inspector" aria-label="星系详情">
    <header class="inspector-head">
      <div class="inspector-eyebrow">{{ eyebrow }}</div>
      <h2 class="inspector-name">{{ title }}</h2>
      <p class="inspector-desc">{{ description }}</p>
      <div class="inspector-status">
        <span class="status-chip" :class="statusTone">{{ statusText }}</span>
        <span class="status-meta">{{ metaText }}</span>
      </div>
    </header>

    <nav class="inspector-tabs" role="tablist">
      <button
        v-for="item in tabs"
        :key="item.value"
        type="button"
        role="tab"
        :aria-selected="tab === item.value"
        :class="{ active: tab === item.value }"
        @click="emit('update:tab', item.value)"
      >{{ item.label }}</button>
    </nav>

    <div class="inspector-body">
      <template v-if="tab === 'chat'">
        <EmployeeConversation :employee="employee" />
      </template>

      <template v-else-if="tab === 'tasks'">
        <div class="task-toolbar">
          <span class="section-title">
            {{ worker ? "当前 Worker 关联任务" : "本星系全部任务" }}
            <small>{{ taskTotal }} 个</small>
          </span>
          <button type="button" class="icon-button" title="刷新任务" @click="loadTasks()">↻</button>
        </div>
        <section v-if="task" class="selected-task">
          <div class="selected-task__head">
            <span class="status-pill" :class="pillClass(task.status)">{{ statusTextOf(task.status) }}</span>
            <span v-if="runtimeWarning(task)" class="runtime-tag">{{ runtimeWarning(task) }}</span>
            <span class="selected-task__id">{{ task.id }}</span>
          </div>
          <h3>{{ task.title }}</h3>
          <dl class="fact-grid">
            <div><dt>执行 Worker</dt><dd>{{ task.workerName || "尚未绑定" }}</dd></div>
            <div><dt>责任员工</dt><dd>{{ task.ownerName || "—" }}</dd></div>
            <div><dt>更新时间</dt><dd>{{ formatTime(task.updatedAt) }}</dd></div>
            <div><dt>Runtime</dt><dd>{{ runtimeText(task) }}</dd></div>
          </dl>
          <p v-if="task.currentProgress" class="progress-text">{{ task.currentProgress }}</p>
          <p v-if="task.waitingReason" class="reason-text wait">等待原因：{{ task.waitingReason }}</p>
          <p v-if="task.failureReason" class="reason-text fail">失败原因：{{ task.failureReason }}</p>
          <p v-if="task.sourceConversationId" class="reason-text">
            来源私聊：{{ task.sourceConversationId }}
            <RouterLink
              v-if="task.ownerId"
              class="reference-link"
              :to="`/chat?employee=${encodeURIComponent(task.ownerId)}`"
            >查看员工会话</RouterLink>
          </p>
          <p v-if="task.sourceChannelId" class="reason-text">来源群组：{{ task.sourceChannelId }}</p>
          <p v-if="task.conflict" class="reason-text fail">
            任务责任员工与执行 Worker 归属冲突，画布不绘制执行光路，请到任务中心核对。
          </p>
          <div class="selected-task__actions">
            <RouterLink class="primary-link" :to="`/tasks/${task.id}`">打开任务中心</RouterLink>
            <button
              v-if="canConfirm"
              type="button"
              class="action-button"
              :disabled="confirming"
              @click="confirmSelected"
            >{{ confirming ? "确认中…" : "按服务端结果确认完成" }}</button>
            <button type="button" class="text-button" @click="emit('refresh')">刷新星系</button>
          </div>
        </section>

        <div class="task-filters">
          <el-select v-model="statusFilter" placeholder="状态：全部" clearable size="small" style="width: 122px">
            <el-option label="排队中" value="PENDING" />
            <el-option label="执行中" value="RUNNING" />
            <el-option label="等待人工" value="WAITING" />
            <el-option label="执行异常" value="FAILED" />
            <el-option label="已完成" value="SUCCEEDED" />
            <el-option label="已取消" value="CANCELED" />
          </el-select>
          <el-select
            v-if="!worker"
            v-model="priorityFilter"
            placeholder="优先级：全部"
            clearable
            size="small"
            style="width: 118px"
          >
            <el-option label="紧急" value="VERY_HIGH" />
            <el-option label="高" value="HIGH" />
            <el-option label="中" value="MEDIUM" />
            <el-option label="低" value="LOW" />
          </el-select>
          <el-input
            v-if="!worker"
            v-model="keywordDraft"
            size="small"
            clearable
            placeholder="搜索任务编号 / 标题"
            @keydown.enter.prevent="applyKeyword"
            @clear="applyKeyword"
          />
          <el-button v-if="!worker" size="small" @click="applyKeyword">搜索</el-button>
        </div>

        <div v-if="workerCounts" class="worker-counts">
          <span v-for="(value, key) in workerCounts" :key="key">{{ statusTextOf(String(key)) }} {{ value }}</span>
        </div>

        <div v-if="tasksError" class="panel-note error">
          {{ tasksError }}
          <button type="button" class="text-button" @click="loadTasks()">重试</button>
        </div>
        <div v-else-if="tasksLoading && !tasks.length" class="panel-note">正在读取任务…</div>
        <div v-else-if="!tasks.length" class="empty-card">暂无关联任务</div>
        <div v-else class="task-list" :class="{ compact: Boolean(worker) }">
          <button
            v-for="item in tasks"
            :key="item.id"
            type="button"
            class="task-card"
            :class="{ selected: item.id === task?.id }"
            @click="selectTask(item)"
          >
            <span class="task-card__top">
              <span class="status-pill" :class="pillClass(item.status)">{{ statusTextOf(item.status) }}</span>
              <span v-if="runtimeWarning(item)" class="runtime-tag">{{ runtimeWarning(item) }}</span>
              <small>{{ priorityText(item.priority) }}</small>
            </span>
            <b>{{ item.title }}</b>
            <small class="task-card__meta">
              {{ item.id }} · {{ item.workerName || "未绑定 Worker" }} · {{ formatTime(item.updatedAt) }}
            </small>
            <span v-if="item.conflict" class="conflict-tag">数据冲突</span>
          </button>
        </div>

        <el-pagination
          v-if="!worker && taskTotal > pageSize"
          v-model:current-page="page"
          v-model:page-size="pageSize"
          :page-sizes="[10, 20, 50]"
          :total="taskTotal"
          layout="prev, pager, next"
          small
          class="task-pager"
        />

      </template>

      <template v-else-if="tab === 'overview'">
        <section v-if="collaboration" class="collab-card">
          <div class="collab-card__head">
            <span>跨星系职责协作</span>
            <b>{{ collaboration.requestCount }} 次</b>
          </div>
          <p class="collab-card__meta">
            最近请求：{{ formatTime(collaboration.lastRequestedAt) }}
            <span :class="{ live: collaboration.recent }">{{ collaboration.recent ? "24 小时内" : "历史关系" }}</span>
          </p>
          <div v-if="collabDetailLoading" class="panel-note">正在读取协作记录…</div>
          <div v-else-if="collabDetail?.items?.length" class="collab-directions">
            <div v-for="(direction, index) in collabDetail.items[0].directions" :key="index" class="collab-direction">
              <b>{{ employeeName(direction.fromEmployeeId) }} → {{ employeeName(direction.toEmployeeId) }}</b>
              <span>{{ direction.count }} 次 · 最近 {{ formatTime(direction.lastRequestedAt) }}</span>
              <div class="reference-links">
                <template v-for="(reference, refIndex) in direction.references" :key="refIndex">
                  <RouterLink
                    v-if="referenceLink(direction, reference)"
                    class="reference-link"
                    :to="referenceLink(direction, reference) || '/chat'"
                  >打开来源会话</RouterLink>
                  <span v-else class="reference-muted">{{ referenceLabel(reference) }}</span>
                </template>
                <span v-if="!direction.references.length" class="reference-muted">无可见来源</span>
              </div>
            </div>
          </div>
          <div v-else class="panel-note">暂无可展示的方向明细。</div>
          <p class="collab-disclaimer">职责请求只表示已发起联系，不代表对方已执行或完成。</p>
        </section>

        <section v-if="!employee && task" class="selected-task">
          <div class="selected-task__head">
            <span class="status-pill" :class="pillClass(task.status)">{{ statusTextOf(task.status) }}</span>
            <span v-if="runtimeWarning(task)" class="runtime-tag">{{ runtimeWarning(task) }}</span>
            <span class="selected-task__id">{{ task.id }}</span>
          </div>
          <h3>{{ task.title }}</h3>
          <dl class="fact-grid">
            <div><dt>执行 Worker</dt><dd>{{ task.workerName || "尚未绑定" }}</dd></div>
            <div><dt>责任员工</dt><dd>{{ task.ownerName || "未关联" }}</dd></div>
            <div><dt>更新时间</dt><dd>{{ formatTime(task.updatedAt) }}</dd></div>
            <div><dt>Runtime</dt><dd>{{ runtimeText(task) }}</dd></div>
          </dl>
          <p v-if="task.currentProgress" class="progress-text">{{ task.currentProgress }}</p>
          <p v-if="task.waitingReason" class="reason-text wait">等待原因：{{ task.waitingReason }}</p>
          <p v-if="task.failureReason" class="reason-text fail">失败原因：{{ task.failureReason }}</p>
          <p v-if="task.sourceConversationId" class="reason-text">来源私聊：{{ task.sourceConversationId }}</p>
          <p v-if="task.sourceChannelId" class="reason-text">来源群组：{{ task.sourceChannelId }}</p>
          <p class="reason-text fail">该任务没有可归属的数字员工，请到任务中心维护责任归属。</p>
          <div class="selected-task__actions">
            <RouterLink class="primary-link" :to="`/tasks/${task.id}`">打开任务中心</RouterLink>
            <button type="button" class="text-button" @click="emit('refresh')">刷新星系</button>
          </div>
        </section>

        <div v-if="detailError" class="panel-note error">
          {{ detailError }}
          <button type="button" class="text-button" @click="loadDetail()">重试</button>
        </div>

        <template v-if="employee">
          <div class="summary-line">
            <div v-for="item in employeeMetrics" :key="item.label" class="mini">
              <strong>{{ item.value }}</strong>
              <small>{{ item.label }}</small>
            </div>
          </div>

          <div class="section-title">
            专属 Agent Worker <small>{{ detail?.workers.length ?? employee.workerIds.length }} 台</small>
          </div>
          <div v-if="!employee.workerIds.length" class="empty-card">该员工尚未配备执行资源。</div>
          <div
            v-for="item in detail?.workers ?? workerList"
            :key="item.id"
            class="worker-line"
            :class="{ selected: item.id === worker?.id }"
            role="button"
            tabindex="0"
            @click="emit('select', { kind: 'worker', id: item.id })"
            @keydown.enter.prevent="emit('select', { kind: 'worker', id: item.id })"
          >
            <span class="worker-icon" :data-health="item.health">◈</span>
            <span class="worker-main">
              <b>{{ item.name }}</b>
              <small>{{ item.role }} · {{ item.environmentMode || "环境未知" }}</small>
            </span>
            <span class="worker-state" :data-tone="workerTone(item)">{{ healthText(item.health) }}</span>
          </div>

          <div class="section-title">
            待处理任务 <small>{{ detail?.attentionTasks.length ?? 0 }} 个</small>
          </div>
          <div v-if="detail?.attentionTasks.length" class="attention-list">
            <button
              v-for="item in detail.attentionTasks"
              :key="item.id"
              type="button"
              class="attention-line"
              @click="selectTask(item)"
            >
              <span class="status-pill" :class="pillClass(item.status)">{{ statusTextOf(item.status) }}</span>
              <b>{{ item.title }}</b>
              <small>{{ item.waitingReason || item.failureReason || formatTime(item.updatedAt) }}</small>
            </button>
          </div>
          <div v-else class="empty-card">没有需要人工处理的任务。</div>

          <div class="section-title">跨星系协作 <small>{{ (detail?.collaborations ?? []).length }} 条关系</small></div>
          <div
            v-for="edge in detail?.collaborations ?? []"
            :key="`${edge.fromEmployeeId}:${edge.toEmployeeId}`"
            class="worker-line"
            role="button"
            tabindex="0"
            @click="emit('select', { kind: 'collaboration', id: collaborationKey(edge.fromEmployeeId, edge.toEmployeeId) })"
            @keydown.enter.prevent="emit('select', { kind: 'collaboration', id: collaborationKey(edge.fromEmployeeId, edge.toEmployeeId) })"
          >
            <span class="worker-icon">⇄</span>
            <span class="worker-main">
              <b>{{ employeeName(otherEmployeeId(edge)) }}</b>
              <small>历史协作 {{ edge.requestCount }} 次 · 最近 {{ formatTime(edge.lastRequestedAt) }}</small>
            </span>
            <span class="worker-state">查看</span>
          </div>
          <div v-if="!(detail?.collaborations ?? []).length" class="empty-card">暂无跨员工职责请求记录。</div>
        </template>

        <template v-else-if="worker">
          <div class="summary-line">
            <div class="mini"><strong>{{ worker.runningTaskCount }}</strong><small>执行中</small></div>
            <div class="mini"><strong>{{ worker.waitingTaskCount }}</strong><small>待人工</small></div>
            <div class="mini"><strong>{{ worker.taskCount }}</strong><small>关联任务</small></div>
          </div>
          <dl class="fact-grid">
            <div><dt>健康状态</dt><dd>{{ healthText(worker.health) }}</dd></div>
            <div><dt>占用状态</dt><dd>{{ occupancyText(worker.occupancy) }}</dd></div>
            <div><dt>执行环境</dt><dd>{{ worker.environmentName || "—" }}</dd></div>
            <div><dt>环境模式</dt><dd>{{ worker.environmentMode || "—" }}</dd></div>
            <div><dt>远程实例</dt><dd>{{ worker.workerInstanceId || "无远程实例" }}</dd></div>
            <div><dt>最近心跳</dt><dd>{{ formatTime(worker.lastHeartbeatAt) }}</dd></div>
          </dl>
          <div class="section-title">能力标签</div>
          <div class="tag-row">
            <span v-for="label in worker.capabilityLabels" :key="label" class="mini-tag">{{ label }}</span>
            <span v-if="!worker.capabilityLabels.length" class="reference-muted">暂无标签</span>
          </div>
          <div class="section-title">Worker 详情</div>
          <RouterLink class="primary-link" :to="`/settings/workers/${worker.id}`">打开 Worker 配置与执行环境</RouterLink>
          <div class="section-title">说明</div>
          <p class="panel-copy">
            健康状态来自 Worker Gateway／执行环境事实，占用状态来自真实 Task 绑定；
            离线或等待人工都不会被显示为空闲。
          </p>
        </template>

        <template v-else>
          <div class="summary-line">
            <div v-for="item in globalMetrics" :key="item.label" class="mini">
              <strong>{{ item.value }}</strong>
              <small>{{ item.label }}</small>
            </div>
          </div>

          <div class="section-title">需要关注 <small>{{ snapshot?.attentionTasks.length ?? 0 }} 个任务</small></div>
          <div v-if="snapshot?.attentionTasks.length" class="attention-list">
            <button
              v-for="item in snapshot.attentionTasks"
              :key="item.id"
              type="button"
              class="attention-line"
              @click="selectTask(item)"
            >
              <span class="status-pill" :class="pillClass(item.status)">{{ statusTextOf(item.status) }}</span>
              <b>{{ item.title }}</b>
              <small>{{ item.ownerName || "未关联员工" }} · {{ runtimeWarning(item) || item.waitingReason || item.failureReason || formatTime(item.updatedAt) }}</small>
            </button>
          </div>
          <div v-else class="empty-card">当前没有等待人工、失败或连接异常的任务。</div>

          <div class="section-title">待绑定 Agent Worker <small>{{ unassignedWorkers.length }} 台</small></div>
          <RouterLink
            v-for="item in unassignedWorkers"
            :key="item.id"
            class="worker-line"
            :to="`/settings/workers/${item.id}`"
          >
            <span class="worker-icon" :data-health="item.health">◈</span>
            <span class="worker-main">
              <b>{{ item.name }}</b>
              <small>未关联数字员工 · {{ healthText(item.health) }} · {{ item.taskCount }} 项关联任务</small>
            </span>
            <span class="worker-state">去维护</span>
          </RouterLink>
          <div v-if="!unassignedWorkers.length" class="empty-card">所有 Worker 都已有数字员工归属。</div>

          <div class="section-title">待分配 / 未关联任务 <small>{{ snapshot?.unassigned.taskCount ?? 0 }} 个</small></div>
          <div v-if="snapshot?.unassignedTasks.length" class="attention-list">
            <button
              v-for="item in snapshot.unassignedTasks"
              :key="item.id"
              type="button"
              class="attention-line"
              @click="selectTask(item)"
            >
              <span class="status-pill" :class="pillClass(item.status)">{{ statusTextOf(item.status) }}</span>
              <b>{{ item.title }}</b>
              <small>{{ item.workerName || "无执行 Worker" }} · {{ runtimeWarning(item) || formatTime(item.updatedAt) }}</small>
            </button>
          </div>
          <div v-else class="empty-card">没有缺失责任员工的任务。</div>

          <div class="section-title">数据核对 <small>{{ snapshot?.dataConflicts.length ?? 0 }} 条</small></div>
          <div v-if="snapshot?.dataConflicts.length" class="conflict-list">
            <p v-for="item in snapshot.dataConflicts" :key="`${item.code}:${item.entityId}`">
              <b>{{ item.code }}</b> · {{ item.message }}
              <span>{{ item.entityId }}</span>
            </p>
          </div>
          <div v-else class="empty-card">员工、Worker、任务与职责请求映射一致。</div>

          <div v-if="snapshot?.dataWarnings.length" class="panel-note warning">
            <p v-for="item in snapshot.dataWarnings" :key="item.code">{{ item.message }}（{{ item.count }}）</p>
          </div>
          <div v-if="snapshot?.degradedSources.length" class="panel-note warning">
            部分数据暂不可用：{{ snapshot.degradedSources.join("、") }}。页面保留最近一次已确认状态。
          </div>
        </template>
      </template>

      <template v-else>
        <div class="section-title">需要关注 <small>{{ attentionCount }} 项</small></div>
        <div v-if="snapshot?.attentionTasks.length" class="attention-list">
          <button
            v-for="item in snapshot.attentionTasks"
            :key="item.id"
            type="button"
            class="attention-line"
            @click="selectTask(item)"
          >
            <span class="status-pill" :class="pillClass(item.status)">{{ statusTextOf(item.status) }}</span>
            <b>{{ item.title }}</b>
            <small>{{ item.ownerName || "未关联员工" }} · {{ item.waitingReason || item.failureReason || formatTime(item.updatedAt) }}</small>
          </button>
        </div>
        <div v-else class="empty-card">当前没有待人工、失败或连接异常的任务。</div>

        <div class="section-title">异常 Worker <small>{{ unhealthyWorkers.length }} 台</small></div>
        <div
          v-for="item in unhealthyWorkers"
          :key="item.id"
          class="worker-line"
          role="button"
          tabindex="0"
          @click="emit('select', { kind: 'worker', id: item.id })"
          @keydown.enter.prevent="emit('select', { kind: 'worker', id: item.id })"
        >
          <span class="worker-icon" :data-health="item.health">◈</span>
          <span class="worker-main">
            <b>{{ item.name }}</b>
            <small>{{ employeeName(item.digitalEmployeeId) }} · {{ healthText(item.health) }}</small>
          </span>
          <span class="worker-state" :data-tone="workerTone(item)">{{ healthText(item.health) }}</span>
        </div>
        <div v-if="!unhealthyWorkers.length" class="empty-card">所有 Agent Worker 健康。</div>

        <div class="section-title">人工处理入口</div>
        <RouterLink class="primary-link" :to="{ path: '/tasks', query: { view: 'interventions' } }">
          打开任务中心的待处理视图
        </RouterLink>
      </template>
    </div>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { RouterLink } from "vue-router";
import { ElMessage } from "element-plus";
import EmployeeConversation from "./EmployeeConversation.vue";
import {
  confirmTaskCompletion,
  getNebulaCollaborations,
  getNebulaEmployee,
  getNebulaEmployeeTasks,
  getNebulaLinkTasks,
  getNebulaTask,
} from "../api";
import {
  HEALTH_TEXT,
  OCCUPANCY_TEXT,
  RUNTIME_TEXT,
  SESSION_TEXT,
  STATUS_TEXT,
  collaborationKey,
  formatTime,
  priorityText,
} from "../layout";
import type {
  NebulaCollaborationDirection,
  NebulaCollaborationEdge,
  NebulaCollaborationResponse,
  NebulaEmployee,
  NebulaEmployeeDetail,
  NebulaSnapshot,
  NebulaTask,
  NebulaWorker,
} from "../types";

export type InspectorTab = "chat" | "tasks" | "overview" | "attention";

const props = defineProps<{
  snapshot: NebulaSnapshot | null;
  employee: NebulaEmployee | null;
  worker: NebulaWorker | null;
  task: NebulaTask | null;
  collaboration: NebulaCollaborationEdge | null;
  tab: InspectorTab;
}>();

const emit = defineEmits<{
  "update:tab": [tab: InspectorTab];
  select: [
    payload: {
      kind: "employee" | "worker" | "task" | "collaboration";
      id: string;
      task?: NebulaTask;
      employeeId?: string;
    },
  ];
  refresh: [];
}>();

const tasks = ref<NebulaTask[]>([]);
const taskTotal = ref(0);
const tasksLoading = ref(false);
const tasksError = ref("");
const statusFilter = ref("");
const priorityFilter = ref("");
const keywordDraft = ref("");
const keyword = ref("");
const page = ref(1);
const pageSize = ref(10);
const workerCounts = ref<Record<string, number> | null>(null);
const confirming = ref(false);
const detail = ref<NebulaEmployeeDetail | null>(null);
const detailLoading = ref(false);
const detailError = ref("");
const collabDetail = ref<NebulaCollaborationResponse | null>(null);
const collabDetailLoading = ref(false);
let taskGeneration = 0;
let detailGeneration = 0;

const tabs = computed<Array<{ value: InspectorTab; label: string }>>(() => {
  if (!props.employee) {
    return [
      { value: "overview", label: "星系总览" },
      { value: "attention", label: "待处理" },
    ];
  }
  if (props.worker) {
    return [
      { value: "tasks", label: "关联任务" },
      { value: "overview", label: "执行概览" },
    ];
  }
  return [
    { value: "chat", label: "员工对话" },
    { value: "tasks", label: "关联任务" },
    { value: "overview", label: "星系概览" },
  ];
});

watch(
  () => [props.employee?.id, props.worker?.id, tabs.value.length],
  () => {
    if (!tabs.value.some((item) => item.value === props.tab)) {
      emit("update:tab", tabs.value[0]?.value ?? "overview");
    }
  },
  { immediate: true },
);

const eyebrow = computed(() => {
  if (props.worker) return "AGENT WORKER / 专属执行节点";
  if (props.employee) return "DIGITAL EMPLOYEE / 星系核心";
  return "NEXUS / DIGITAL UNIVERSE";
});

const title = computed(() => {
  if (props.worker) return props.worker.name;
  if (props.employee) return props.employee.name;
  return "数字员工星系";
});

const description = computed(() => {
  if (props.worker) {
    return `固定归属：${props.employee?.name ?? "未知员工"} · ${props.worker.role || "执行节点"}`;
  }
  if (props.employee) return props.employee.description || props.employee.domain;
  return "每位数字员工是一座独立星系；Worker、任务与职责请求都来自 Nexus 正式事实源。";
});

const statusText = computed(() => {
  if (props.worker) {
    return `${HEALTH_TEXT[props.worker.health] ?? props.worker.health} · ${OCCUPANCY_TEXT[props.worker.occupancy] ?? props.worker.occupancy}`;
  }
  if (props.employee) {
    const counts = props.employee.counts;
    if (counts.waiting) return `有 ${counts.waiting} 项等待人工`;
    if (counts.failed) return `有 ${counts.failed} 项执行异常`;
    if (counts.running) return `正在执行 ${counts.running} 项任务`;
    if (counts.queued) return `排队 ${counts.queued} 项`;
    return props.employee.interactionStatus === "available" ? "在线 · 可交互" : "当前不可交互";
  }
  return "只读投影 · 每 15 秒核对快照";
});

const statusTone = computed(() => {
  if (props.worker) {
    if (props.worker.health === "error") return "warn";
    if (props.worker.health === "offline") return "muted";
    return "ok";
  }
  if (props.employee) {
    if (props.employee.counts.waiting || props.employee.counts.failed) return "warn";
    if (!props.employee.enabled) return "muted";
  }
  return "ok";
});

const metaText = computed(() => {
  if (props.worker) {
    return `${props.worker.environmentName || "执行环境未知"} · ${props.worker.taskCount} 项关联任务`;
  }
  if (props.employee) {
    return `${props.employee.workerIds.length} Worker · ${props.employee.counts.queued} 个排队 · 今日完成 ${props.employee.counts.completedToday}`;
  }
  return "员工目录 · Agent Worker · Task · 职责请求";
});

const employeeMetrics = computed(() => {
  const counts = props.employee?.counts;
  return [
    { label: "执行中", value: counts?.running ?? 0 },
    { label: "排队", value: counts?.queued ?? 0 },
    { label: "待人工", value: counts?.waiting ?? 0 },
    { label: "异常", value: counts?.failed ?? 0 },
    { label: "今日完成", value: counts?.completedToday ?? 0 },
  ];
});

const globalMetrics = computed(() => {
  const totals = props.snapshot?.totals;
  return [
    { label: "员工星系", value: totals?.employees ?? 0 },
    { label: "Worker 在线", value: `${totals?.workersOnline ?? 0}/${totals?.workers ?? 0}` },
    { label: "执行中", value: totals?.tasksRunning ?? 0 },
    { label: "排队", value: totals?.tasksQueued ?? 0 },
    { label: "待人工", value: totals?.tasksWaiting ?? 0 },
    { label: "今日完成", value: totals?.completedToday ?? 0 },
  ];
});

const workerList = computed<NebulaWorker[]>(() => {
  if (!props.employee) return [];
  return (props.snapshot?.workers ?? []).filter(
    (item) => item.digitalEmployeeId === props.employee?.id,
  );
});

const unassignedWorkers = computed(() => {
  const known = new Set((props.snapshot?.employees ?? []).map((item) => item.id));
  return (props.snapshot?.workers ?? []).filter(
    (item) => !item.digitalEmployeeId || !known.has(item.digitalEmployeeId),
  );
});

const unhealthyWorkers = computed(() =>
  (props.snapshot?.workers ?? []).filter((item) =>
    ["offline", "error", "unknown"].includes(item.health),
  ),
);

const attentionCount = computed(() => props.snapshot?.attentionTasks.length ?? 0);

const canConfirm = computed(
  () =>
    props.task?.status === "WAITING" &&
    props.task.sessionStatus === "SUCCEEDED" &&
    !props.task.pendingMessageId,
);

watch(
  () => [
    props.tab,
    props.employee?.id,
    props.worker?.id,
    statusFilter.value,
    priorityFilter.value,
    page.value,
    pageSize.value,
  ],
  () => void loadTasks(),
  { immediate: true },
);

watch(
  () => [props.tab, props.employee?.id],
  () => {
    if (props.tab === "overview" || props.tab === "attention") void loadDetail();
  },
  { immediate: true },
);

watch(
  () => props.collaboration,
  () => void loadCollaborationDetail(),
  { immediate: true },
);

async function loadTasks(): Promise<void> {
  if (props.tab !== "tasks" || !props.employee) return;
  const generation = ++taskGeneration;
  tasksLoading.value = true;
  tasksError.value = "";
  try {
    if (props.worker) {
      const response = await getNebulaLinkTasks(props.worker.id, {
        status: statusFilter.value || undefined,
        limit: 200,
      });
      if (generation !== taskGeneration) return;
      tasks.value = response.items;
      taskTotal.value = response.items.length;
      workerCounts.value = response.counts;
    } else {
      const response = await getNebulaEmployeeTasks(props.employee.id, {
        status: statusFilter.value || undefined,
        priority: priorityFilter.value || undefined,
        q: keyword.value || undefined,
        page: page.value,
        pageSize: pageSize.value,
      });
      if (generation !== taskGeneration) return;
      tasks.value = response.items;
      taskTotal.value = response.total;
      workerCounts.value = null;
    }
  } catch (reason) {
    if (generation !== taskGeneration) return;
    tasks.value = [];
    tasksError.value = reason instanceof Error ? reason.message : "关联任务读取失败";
  } finally {
    if (generation === taskGeneration) tasksLoading.value = false;
  }
}

async function loadDetail(): Promise<void> {
  if (!props.employee) {
    detail.value = null;
    return;
  }
  const generation = ++detailGeneration;
  detailLoading.value = true;
  detailError.value = "";
  detail.value = null;
  try {
    const response = await getNebulaEmployee(props.employee.id);
    if (generation !== detailGeneration) return;
    detail.value = response;
  } catch (reason) {
    if (generation !== detailGeneration) return;
    detail.value = null;
    detailError.value = reason instanceof Error ? reason.message : "星系详情读取失败";
  } finally {
    if (generation === detailGeneration) detailLoading.value = false;
  }
}

async function loadCollaborationDetail(): Promise<void> {
  const edge = props.collaboration;
  if (!edge) {
    collabDetail.value = null;
    return;
  }
  collabDetailLoading.value = true;
  try {
    collabDetail.value = await getNebulaCollaborations({
      fromEmployeeId: edge.fromEmployeeId,
      toEmployeeId: edge.toEmployeeId,
    });
  } catch {
    collabDetail.value = null;
  } finally {
    collabDetailLoading.value = false;
  }
}

function applyKeyword(): void {
  keyword.value = keywordDraft.value.trim();
  page.value = 1;
  void loadTasks();
}

watch([statusFilter, priorityFilter], () => {
  page.value = 1;
});

function selectTask(task: NebulaTask): void {
  emit("select", {
    kind: "task",
    id: task.id,
    task,
    employeeId: task.ownerId ?? props.employee?.id,
  });
}

async function confirmSelected(): Promise<void> {
  const task = props.task;
  if (!task) return;
  confirming.value = true;
  try {
    await confirmTaskCompletion(task.id);
    const fresh = await getNebulaTask(task.id).catch(() => null);
    if (fresh) {
      emit("select", {
        kind: "task",
        id: fresh.task.id,
        task: fresh.task,
        employeeId: fresh.task.ownerId ?? props.employee?.id,
      });
    }
    ElMessage.success("已按服务端结果确认任务完成");
    emit("refresh");
    await loadTasks();
    await loadDetail();
  } catch (reason) {
    ElMessage.error(reason instanceof Error ? reason.message : "任务状态冲突，请刷新后重试");
    emit("refresh");
    await loadTasks();
    await loadDetail();
  } finally {
    confirming.value = false;
  }
}

function statusTextOf(status: string): string {
  return STATUS_TEXT[status] ?? status;
}

function healthText(health: string): string {
  return HEALTH_TEXT[health] ?? health;
}

function runtimeWarning(task: NebulaTask): string {
  if (task.runtimeConnection === "UNAVAILABLE") return "连接异常";
  if (task.sessionStatus === "UNKNOWN") return "状态待确认";
  return "";
}

function runtimeText(task: NebulaTask): string {
  if (task.sessionStatus) return SESSION_TEXT[task.sessionStatus] ?? task.sessionStatus;
  return RUNTIME_TEXT[task.runtimeConnection] ?? task.runtimeConnection ?? "待确认";
}

function occupancyText(occupancy: string): string {
  return OCCUPANCY_TEXT[occupancy] ?? occupancy;
}

function workerTone(worker: NebulaWorker): string {
  if (worker.health === "error") return "warn";
  if (worker.health === "offline") return "muted";
  if (worker.occupancy === "waiting") return "wait";
  return "ok";
}

function pillClass(status: string): string {
  if (status === "PENDING") return "queue";
  if (status === "WAITING") return "wait";
  if (status === "FAILED") return "fail";
  if (status === "SUCCEEDED") return "done";
  if (status === "CANCELED") return "muted";
  return "run";
}

function employeeName(id: string | null | undefined): string {
  if (!id) return "未关联员工";
  return props.snapshot?.employees.find((item) => item.id === id)?.name ?? id;
}

function otherEmployeeId(edge: NebulaCollaborationEdge): string {
  const current = props.employee?.id ?? "";
  return edge.fromEmployeeId === current ? edge.toEmployeeId : edge.fromEmployeeId;
}

function referenceLink(
  direction: NebulaCollaborationDirection,
  reference: { kind: string; id: string | null },
): string | null {
  if (reference.kind === "conversation" && reference.id) {
    return `/chat?employee=${encodeURIComponent(direction.fromEmployeeId)}`;
  }
  if (reference.kind === "channel" && reference.id) {
    return `/chat?group=${encodeURIComponent(reference.id)}`;
  }
  return null;
}

function referenceLabel(reference: { kind: string; id: string | null }): string {
  if (reference.kind === "private") return "私聊来源（无权限查看）";
  if (reference.kind === "mailbox") return "员工邮箱";
  if (reference.kind === "unknown") return "来源不可识别";
  return "来源不可跳转";
}
</script>

<style scoped>
.galaxy-inspector {
  width: 366px;
  min-width: 366px;
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: #fbfefc;
  border-left: 1px solid #d9e9e0;
  box-shadow: -16px 0 34px #04180f22;
  color: #1b3b35;
  font-family: Inter, "PingFang SC", "Microsoft YaHei", sans-serif;
}
.inspector-head { padding: 20px 22px 15px; border-bottom: 1px solid #e2efe8; flex: none; }
.inspector-eyebrow { color: #0da782; font-weight: 800; font-size: 9px; letter-spacing: 1.6px; }
.inspector-name { margin: 9px 0 5px; font-size: 20px; line-height: 1.25; font-weight: 800; }
.inspector-desc { margin: 0; color: #80978c; font-size: 11px; line-height: 1.7; max-height: 54px; overflow: hidden; }
.inspector-status { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 11px; }
.status-chip { border-radius: 24px; padding: 4px 9px; background: #e9f9f2; color: #0b9971; font-size: 10px; }
.status-chip.warn { background: #fff4e4; color: #b7832d; }
.status-chip.muted { background: #eef2f1; color: #7d918a; }
.status-meta { color: #91a9a0; font-size: 10px; }
.inspector-tabs { display: flex; border-bottom: 1px solid #e4efe8; padding: 0 8px; flex: none; }
.inspector-tabs button {
  border: 0; background: transparent; color: #84978e; flex: 1; padding: 12px 4px 11px;
  font-size: 12px; cursor: pointer; border-bottom: 2px solid transparent;
}
.inspector-tabs button.active { color: #0c9774; border-bottom-color: #0ba77d; font-weight: 750; }
.inspector-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 15px 16px 22px; scrollbar-color: #cfe6da transparent; }
.section-title { display: flex; justify-content: space-between; align-items: baseline; font-size: 11px; color: #627f73; font-weight: 800; letter-spacing: .3px; margin: 16px 0 9px; }
.section-title small { color: #91a89c; font-weight: 500; }
.task-toolbar { display: flex; justify-content: space-between; align-items: center; }
.icon-button { border: 1px solid #d9e9e0; background: #fff; border-radius: 7px; width: 25px; height: 25px; cursor: pointer; color: #4c7a68; }
.task-filters { display: flex; gap: 6px; align-items: center; margin: 0 0 10px; flex-wrap: wrap; }
.worker-counts { display: flex; flex-wrap: wrap; gap: 5px 10px; font-size: 10px; color: #7d9589; margin-bottom: 9px; }
.task-list { display: grid; gap: 7px; }
.task-card {
  width: 100%; text-align: left; border: 1px solid #e1eee6; border-radius: 10px; background: #fbfefc;
  padding: 9px 10px; cursor: pointer; display: grid; gap: 3px; color: #1b3b35;
}
.task-card:hover { border-color: #9ccdb7; }
.task-card.selected { border-color: #11a77f; background: #e8faf1; }
.task-card__top { display: flex; justify-content: space-between; align-items: center; gap: 6px; }
.task-card__top small { color: #91a399; font-size: 9px; }
.task-card b { font-size: 12px; line-height: 1.45; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-card__meta { color: #839c90; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.task-list.compact .task-card b { white-space: normal; }
.runtime-tag { font-size: 9px; color: #9a6d2c; background: #fff4e0; border-radius: 5px; padding: 2px 5px; white-space: nowrap; }
.conflict-tag { justify-self: start; font-size: 9px; color: #b05b66; background: #fdeef0; border-radius: 5px; padding: 2px 5px; }
.status-pill { border-radius: 20px; background: #e4f7ed; color: #07946c; padding: 3px 7px; font-size: 9px; white-space: nowrap; }
.status-pill.queue { background: #fff5e5; color: #b47f27; }
.status-pill.wait { background: #ffecdf; color: #c76d39; }
.status-pill.fail { background: #fde9ec; color: #bd4e60; }
.status-pill.done { background: #edf3fa; color: #6f93ad; }
.status-pill.muted { background: #eef2f1; color: #7d918a; }
.status-pill.run { background: #e2f9ef; color: #07946c; }
.task-pager { margin-top: 11px; justify-content: center; }
.empty-card { padding: 16px 12px; border: 1px dashed #d0e4db; border-radius: 9px; color: #9bafa5; font-size: 11px; text-align: center; line-height: 1.6; }
.panel-note { padding: 10px 11px; border: 1px dashed #cfe5da; border-radius: 9px; color: #7c9589; font-size: 11px; line-height: 1.7; margin: 8px 0; }
.panel-note.error { border-color: #f0cbd0; color: #b25a66; background: #fff7f8; }
.panel-note.warning { border-color: #efd8ba; color: #9a6d2c; background: #fffaf0; }
.panel-note p { margin: 2px 0; }
.text-button { border: 0; background: transparent; color: #0b9e7b; font-size: 11px; cursor: pointer; padding: 0 2px; }
.selected-task { margin-top: 14px; border: 1px solid #a8dbbf; background: #eefaf4; border-radius: 11px; padding: 12px; }
.selected-task__head { display: flex; align-items: center; gap: 8px; }
.selected-task__id { color: #7ba195; font-size: 10px; }
.selected-task h3 { margin: 8px 0 9px; font-size: 14px; line-height: 1.45; }
.fact-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 7px 12px; margin: 0 0 10px; }
.fact-grid div { min-width: 0; }
.fact-grid dt { color: #8ba196; font-size: 9px; }
.fact-grid dd { margin: 2px 0 0; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.progress-text, .reason-text { margin: 6px 0; font-size: 11px; line-height: 1.7; color: #547264; }
.reason-text.wait { color: #b7732f; }
.reason-text.fail { color: #b25a66; }
.selected-task__actions { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 10px; align-items: center; }
.primary-link { display: inline-flex; align-items: center; min-height: 30px; padding: 0 10px; border-radius: 7px; background: #0f9c75; color: #fff; text-decoration: none; font-size: 11px; }
.primary-link:hover { background: #0b8764; }
.action-button { border: 1px solid #addbc5; background: #fff; color: #087757; border-radius: 7px; padding: 7px 9px; font-size: 11px; cursor: pointer; }
.action-button:disabled { opacity: .55; cursor: not-allowed; }
.summary-line { display: flex; gap: 6px; margin: 10px 0 15px; flex-wrap: wrap; }
.mini { flex: 1 1 54px; min-width: 54px; border: 1px solid #e0efe6; border-radius: 9px; padding: 8px 4px; text-align: center; background: #fbfffd; }
.mini strong { font-size: 17px; color: #166953; display: block; line-height: 1.35; }
.mini small { font-size: 9px; color: #92a89c; }
.worker-line {
  display: flex; align-items: center; gap: 8px; border: 1px solid #e1eee6; border-radius: 9px;
  padding: 9px 10px; margin-bottom: 6px; background: #fbfefc; cursor: pointer; text-decoration: none; color: inherit;
}
.worker-line:hover, .worker-line.selected { border-color: #9ccdb7; background: #f3fbf7; }
.worker-icon { width: 28px; height: 28px; border-radius: 8px; background: #e2f5ee; display: grid; place-items: center; font-size: 13px; color: #0d9171; flex: none; }
.worker-icon[data-health="error"] { background: #fde9ec; color: #bd4e60; }
.worker-icon[data-health="offline"] { background: #eef2f1; color: #7d918a; }
.worker-icon[data-health="unknown"] { background: #f4f1e8; color: #a0884d; }
.worker-main { flex: 1; min-width: 0; display: grid; gap: 2px; }
.worker-main b { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.worker-main small { color: #91a399; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.worker-state { font-size: 9px; border-radius: 20px; background: #e4f7ed; color: #07946c; padding: 3px 6px; white-space: nowrap; }
.worker-state[data-tone="warn"] { background: #fde9ec; color: #bd4e60; }
.worker-state[data-tone="muted"] { background: #eef2f1; color: #7d918a; }
.worker-state[data-tone="wait"] { background: #ffecdf; color: #c76d39; }
.attention-list { display: grid; gap: 6px; }
.attention-line {
  display: grid; grid-template-columns: auto 1fr; grid-template-rows: auto auto; gap: 2px 7px;
  border: 1px solid #e1eee6; border-radius: 9px; background: #fbfefc; padding: 9px 10px;
  text-align: left; cursor: pointer; color: inherit;
}
.attention-line:hover { border-color: #9ccdb7; }
.attention-line .status-pill { grid-row: span 2; align-self: center; }
.attention-line b { font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.attention-line small { color: #91a399; font-size: 9px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tag-row { display: flex; flex-wrap: wrap; gap: 5px; }
.mini-tag { border: 1px solid #dfede4; background: #f8fcf9; border-radius: 6px; font-size: 9px; padding: 3px 6px; color: #527d65; }
.panel-copy { color: #789287; font-size: 10px; line-height: 1.8; margin: 4px 0; }
.collab-card { border: 1px solid #bcd9e2; background: #f2fbfd; border-radius: 11px; padding: 12px; margin-bottom: 10px; }
.collab-card__head { display: flex; justify-content: space-between; font-size: 11px; color: #4c7a86; font-weight: 750; }
.collab-card__head b { color: #1f6e82; }
.collab-card__meta { margin: 6px 0 8px; font-size: 10px; color: #71909a; display: flex; gap: 7px; }
.collab-card__meta .live { color: #0b9e7b; }
.collab-directions { display: grid; gap: 7px; }
.collab-direction { display: grid; gap: 2px; border-top: 1px dashed #cfe3e8; padding-top: 6px; }
.collab-direction b { font-size: 10px; color: #33616b; }
.collab-direction span { font-size: 9px; color: #7b98a0; }
.reference-links { display: flex; flex-wrap: wrap; gap: 7px; margin-top: 2px; }
.reference-link { color: #0b8fa8; font-size: 9px; text-decoration: none; }
.reference-link:hover { text-decoration: underline; }
.reference-muted { color: #8ba1a8; font-size: 9px; }
.collab-disclaimer { margin: 8px 0 0; font-size: 9px; color: #8298a0; line-height: 1.6; }
.conflict-list { display: grid; gap: 5px; }
.conflict-list p { margin: 0; border-left: 2px solid #e5a6ae; padding-left: 7px; font-size: 10px; color: #9a6972; line-height: 1.6; }
.conflict-list p b { color: #b05b66; }
.conflict-list p span { color: #b19aa0; }
@media (max-width: 1180px) {
  .galaxy-inspector { width: 328px; min-width: 328px; }
}
@media (max-width: 900px) {
  .galaxy-inspector { width: 100%; min-width: 0; height: 46vh; border-left: 0; border-top: 1px solid #d9e9e0; }
}
</style>
