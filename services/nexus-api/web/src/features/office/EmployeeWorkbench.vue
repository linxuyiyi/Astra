<template>
  <aside
    class="employee-workbench"
    aria-labelledby="office-employee-name"
    aria-label="员工工作台"
  >
    <header class="panel-header">
      <button
        class="close-button"
        type="button"
        aria-label="关闭员工工作台"
        @click="$emit('close')"
      >
        <X :size="16" :stroke-width="1.8" />
      </button>

      <div class="employee-heading">
        <EmployeeAvatar :avatar="employee.avatar" :size="52" :disabled="!employee.enabled" />
        <div class="employee-title">
          <div class="employee-title__row">
            <h1 id="office-employee-name">{{ employee.name }}</h1>
            <span class="status-pill" :data-state="state">
              <i aria-hidden="true" />{{ STATE_TEXT[state] }}
            </span>
          </div>
          <div class="employee-meta">
            <span>{{ employee.domain || "数字员工" }}</span>
            <template v-if="data && data.employee.counts.completedToday > 0">
              <span class="employee-meta__sep" aria-hidden="true">·</span>
              <span>今日完成 {{ data.employee.counts.completedToday }}</span>
            </template>
          </div>
        </div>
      </div>

      <div v-if="employee.description?.trim()" class="employee-bio">
        <button
          type="button"
          class="employee-bio__toggle"
          :aria-expanded="bioExpanded"
          @click="bioExpanded = !bioExpanded"
        >
          <span class="employee-bio__preview">{{ bioSummary }}</span>
          <ChevronDown class="employee-bio__chevron" :class="{ 'is-open': bioExpanded }" :size="13" />
        </button>
        <div v-if="bioExpanded" class="employee-bio__body">
          <MarkdownView :content="employee.description" compact />
        </div>
      </div>

      <div class="employee-actions">
        <RouterLink
          class="employee-action employee-action--primary"
          :to="{ path: '/chat', query: { employee: employee.id } }"
        >
          <MessageSquare :size="14" :stroke-width="1.8" />
          <span>发起对话</span>
        </RouterLink>
        <RouterLink
          class="employee-action"
          :to="{ path: '/tasks', query: { owner: employee.id } }"
        >
          <ListTodo :size="14" :stroke-width="1.8" />
          <span>全部任务</span>
        </RouterLink>
      </div>
    </header>

    <div class="panel-content" :aria-busy="loading">
      <div v-if="error" class="inline-alert inline-alert--danger" role="alert">
        <AlertCircle :size="15" />
        <span>
          {{ error }}
          <small v-if="data">当前显示上次确认状态</small>
        </span>
        <button type="button" @click="$emit('refresh')">重试</button>
      </div>

      <div
        v-if="data?.degradedSources.length"
        class="inline-alert inline-alert--warning"
        role="status"
      >
        <AlertCircle :size="15" />
        <span>部分状态待确认：{{ data.degradedSources.join("、") }}</span>
      </div>

      <div v-if="loading && !data" class="panel-loading" role="status">
        <Loader2 class="wb-spinner" :size="16" />
        <span>读取员工状态…</span>
      </div>

      <template v-if="data">
        <!-- 核心指标 -->
        <div class="overview">
          <div class="metric-card">
            <span class="metric-card__label">执行中</span>
            <strong
              class="metric-card__value"
              :class="{ 'is-info': data.employee.counts.running > 0 }"
            >{{ data.employee.counts.running }}</strong>
          </div>

          <button type="button" class="metric-card metric-card--interactive" @click="jumpInbox">
            <span class="metric-card__label">
              待分发
              <ArrowUpRight :size="11" aria-hidden="true" />
            </span>
            <strong class="metric-card__value">{{ data.employee.counts.queued }}</strong>
          </button>

          <button
            type="button"
            class="metric-card metric-card--interactive"
            :class="{ 'is-warning': data.employee.counts.waiting > 0 }"
            @click="jumpInbox"
          >
            <span class="metric-card__label">
              待关注
              <ArrowUpRight :size="11" aria-hidden="true" />
            </span>
            <strong class="metric-card__value">{{ data.employee.counts.waiting }}</strong>
          </button>
        </div>

        <!-- 执行资源 -->
        <section class="wb-section" aria-labelledby="office-workers-heading">
          <div class="section-heading">
            <h2 id="office-workers-heading">执行资源</h2>
            <span class="section-count">{{ data.workerTotal }}</span>
          </div>

          <ul v-if="data.workers.length" class="worker-list">
            <li
              v-for="worker in data.workers"
              :key="worker.id"
              class="worker-row"
              :data-state="workerState(worker)"
            >
              <details>
                <summary>
                  <span class="worker-icon"><Monitor :size="14" :stroke-width="1.7" /></span>
                  <b class="worker-name">{{ worker.name }}</b>
                  <span class="worker-state" :data-state="workerState(worker)">
                    <i aria-hidden="true" />{{ workerLabel(worker) }}
                  </span>
                  <ChevronDown class="chevron" :size="14" />
                </summary>
                <div class="worker-details">
                  <dl>
                    <div>
                      <dt>关联方式</dt>
                      <dd>{{ worker.association === "task" ? "任务执行" : "调度可用" }}</dd>
                    </div>
                    <div>
                      <dt>运行环境</dt>
                      <dd>{{ worker.environmentName || "未配置" }}</dd>
                    </div>
                    <div>
                      <dt>健康 / 占用</dt>
                      <dd>{{ HEALTH_TEXT[worker.health] }} · {{ OCCUPANCY_TEXT[worker.occupancy] }}</dd>
                    </div>
                  </dl>
                  <RouterLink class="worker-detail-link" :to="`/settings/workers/${worker.id}`">
                    查看 Worker 详情 <ArrowUpRight :size="12" />
                  </RouterLink>
                </div>
              </details>

              <div v-if="worker.tasks.length" class="worker-current">
                <RouterLink
                  v-for="task in worker.tasks"
                  :key="task.id"
                  :to="`/tasks/${task.id}`"
                  class="work-task"
                >
                  <span class="work-task__title">{{ task.title }}</span>
                  <ArrowUpRight class="work-task__arrow" :size="12" />
                  <small :class="{ issue: needsAttention(task) }">{{ taskReason(task) }}</small>
                </RouterLink>
                <RouterLink
                  v-if="
                    worker.employeeTaskCounts.RUNNING +
                      worker.employeeTaskCounts.WAITING >
                    worker.tasks.length
                  "
                  class="more-tasks"
                  :to="{
                    path: '/tasks',
                    query: { owner: employee.id, employee: worker.id },
                  }"
                >查看其余关联任务 →</RouterLink>
              </div>

              <p v-else class="worker-idle">
                {{ worker.health === "online" ? "空闲，等待任务调度" : healthReason(worker) }}
              </p>

              <div
                v-if="worker.health !== 'online' && worker.tasks.length"
                class="worker-warning"
              >
                <AlertCircle :size="13" />
                <span>{{ healthReason(worker) }}</span>
              </div>

              <div
                v-if="worker.occupancy === 'waiting' && !worker.employeeTaskCounts.WAITING"
                class="worker-warning"
              >
                <AlertCircle :size="13" />
                <span>共享 Worker 有其他待人工事项</span>
              </div>
            </li>
          </ul>

          <div v-else class="panel-empty">
            <Monitor :size="18" :stroke-width="1.6" />
            <p>暂无关联的执行资源</p>
            <RouterLink to="/settings/workers">查看共享 Worker</RouterLink>
          </div>

          <el-pagination
            v-if="data.workerTotal > data.pageSize"
            small
            layout="prev, pager, next"
            :pager-count="5"
            :page-size="data.pageSize"
            :current-page="data.workerPage"
            :total="data.workerTotal"
            :disabled="loading"
            @current-change="$emit('worker-page', $event)"
          />
        </section>

        <!-- 待处理工作 -->
        <section
          ref="inbox"
          class="wb-section inbox-section"
          aria-labelledby="office-inbox-heading"
        >
          <div class="section-heading">
            <h2 id="office-inbox-heading">待处理</h2>
            <span class="section-count">{{ data.issueTotal }}</span>
          </div>

          <ul v-if="data.issues.length" class="issue-list">
            <li
              v-for="task in data.issues"
              :key="task.id"
              :class="{ urgent: needsAttention(task) }"
            >
              <RouterLink :to="`/tasks/${task.id}`">
                <div class="issue-card__top">
                  <span
                    class="issue-status"
                    :data-tone="needsAttention(task) ? 'warning' : 'neutral'"
                  >
                    <i aria-hidden="true" />{{ STATUS_TEXT[task.status] }}
                  </span>
                  <ArrowUpRight class="issue-card__arrow" :size="13" aria-hidden="true" />
                </div>
                <strong>{{ task.title }}</strong>
                <p>{{ taskReason(task) }}</p>
              </RouterLink>
            </li>
          </ul>

          <div v-else class="inbox-clear">
            <CircleCheck :size="15" :stroke-width="1.8" />
            <span>当前无待处理事项</span>
          </div>

          <el-pagination
            v-if="data.issueTotal > data.pageSize"
            small
            layout="prev, pager, next"
            :pager-count="5"
            :page-size="data.pageSize"
            :current-page="data.issuePage"
            :total="data.issueTotal"
            :disabled="loading"
            @current-change="$emit('issue-page', $event)"
          />
        </section>
      </template>
    </div>

    <footer class="panel-footer">
      <span class="panel-footer__time">
        <i :class="{ stale: !!error }" aria-hidden="true" />
        {{ data ? `更新于 ${formatTime(data.generatedAt)}` : "同步中…" }}
      </span>
      <button
        type="button"
        :disabled="loading"
        aria-label="刷新员工工作台"
        @click="$emit('refresh')"
      >
        <RefreshCw :size="13" :class="{ 'wb-spinner': loading }" />
      </button>
    </footer>
  </aside>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import EmployeeAvatar from "../../components/EmployeeAvatar.vue";
import MarkdownView from "@/components/astra/MarkdownView.vue";
import {
  AlertCircle,
  ArrowUpRight,
  ChevronDown,
  CircleCheck,
  ListTodo,
  Loader2,
  MessageSquare,
  Monitor,
  RefreshCw,
  X,
} from "lucide-vue-next";
import type { NebulaEmployee } from "../nebula/types";
import { HEALTH_TEXT, OCCUPANCY_TEXT, STATUS_TEXT } from "../nebula/layout";
import {
  employeeState,
  needsAttention,
  STATE_TEXT,
  taskReason,
  workerState,
  type EmployeeOffice,
  type OfficeWorker,
} from "./model";

const props = defineProps<{
  employee: NebulaEmployee;
  data: EmployeeOffice | null;
  loading: boolean;
  error: string;
}>();

defineEmits<{
  close: [];
  refresh: [];
  "worker-page": [page: number];
  "issue-page": [page: number];
}>();

const inbox = ref<HTMLElement>();
const bioExpanded = ref(false);

const bioSummary = computed(() => {
  const raw = (props.employee.description || "").replace(/[#>*`_\-\n]+/g, " ").replace(/\s+/g, " ").trim();
  return raw || "查看职责说明";
});

function jumpInbox() {
  inbox.value?.scrollIntoView({ behavior: "smooth", block: "start" });
}

const state = computed(() =>
  employeeState(
    props.data?.employee ?? props.employee,
    !props.data || !!props.error,
  ),
);

function workerLabel(worker: OfficeWorker) {
  if (worker.health !== "online") return HEALTH_TEXT[worker.health];
  if (worker.employeeTaskCounts.WAITING) return "等待人工";
  if (worker.tasks.some(needsAttention)) return "需关注";
  return OCCUPANCY_TEXT[worker.occupancy];
}

function healthReason(worker: OfficeWorker) {
  return worker.health === "offline"
    ? "Worker 已离线，请检查连接"
    : worker.health === "error"
      ? "运行环境异常，请检查 Worker 详情"
      : "健康状态待确认";
}

function formatTime(value: string) {
  return new Date(value).toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
</script>

<style scoped>
.employee-workbench {
  position: absolute;
  top: 14px;
  right: 14px;
  bottom: 66px;
  width: clamp(365px, 31.5vw, 440px);
  display: flex;
  flex-direction: column;
  z-index: 8;
  background: var(--astra-bg-panel);
  border: 1px solid var(--astra-border);
  box-shadow: var(--astra-shadow-float);
  border-radius: 14px;
  overflow: hidden;
  color: var(--astra-text-primary);
}

@media (min-width: 2240px) {
  .employee-workbench {
    top: 18px;
    right: 18px;
    bottom: 76px;
    width: clamp(460px, 20.5vw, 540px);
    border-radius: 16px;
  }
  .panel-header {
    padding: 20px 22px 16px;
  }
  .panel-content {
    padding: 18px 22px 22px;
  }
  .employee-title h1 {
    font-size: 19px;
  }
  .metric-card {
    padding: 12px 14px;
  }
  .metric-card__value {
    font-size: 22px;
  }
  .panel-footer {
    padding: 11px 22px;
  }
}

/* ── 工卡头部 ─────────────────────────────────────────────────── */
.panel-header {
  padding: 16px 18px 14px;
  background: var(--astra-bg-subtle);
  border-bottom: 1px solid var(--astra-border);
  position: relative;
}

.close-button {
  position: absolute;
  right: 12px;
  top: 12px;
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid transparent;
  background: transparent;
  color: var(--astra-text-muted);
  border-radius: 7px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  transition: background-color 0.14s ease, color 0.14s ease, border-color 0.14s ease;
}

.close-button:hover {
  background: var(--astra-bg-elevated);
  border-color: var(--astra-border);
  color: var(--astra-text-primary);
}

.employee-heading {
  display: flex;
  align-items: center;
  gap: 12px;
  padding-right: 28px;
}

.employee-title {
  min-width: 0;
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.employee-title__row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 7px;
}

.employee-title h1 {
  font-size: 17px;
  font-weight: 650;
  line-height: 1.25;
  margin: 0;
  color: var(--astra-text-primary);
  overflow-wrap: anywhere;
}

.employee-meta {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
  font-size: 12px;
  color: var(--astra-text-muted);
}

.employee-meta__sep {
  opacity: 0.6;
}

.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  line-height: 1.45;
  white-space: nowrap;
  color: var(--astra-neutral-text);
  background: var(--astra-neutral-soft);
}

.status-pill i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.status-pill[data-state="working"] {
  color: var(--astra-info);
  background: var(--astra-info-soft);
}

.status-pill[data-state="attention"] {
  color: var(--astra-warning);
  background: var(--astra-warning-soft);
}

.status-pill[data-state="idle"],
.status-pill[data-state="unknown"],
.status-pill[data-state="offline"] {
  color: var(--astra-neutral-text);
  background: var(--astra-neutral-soft);
}

/* 可展开的员工职责简介 */
.employee-bio {
  margin-top: 10px;
  border: 1px solid var(--astra-border-subtle);
  border-radius: 8px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.employee-bio__toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  width: 100%;
  padding: 6px 10px;
  border: 0;
  background: transparent;
  color: var(--astra-text-secondary);
  font-size: 12px;
  text-align: left;
  cursor: pointer;
}

.employee-bio__toggle:hover {
  background: var(--astra-bg-subtle);
}

.employee-bio__preview {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.employee-bio__chevron {
  flex: none;
  color: var(--astra-text-muted);
  transition: transform 0.16s ease;
}

.employee-bio__chevron.is-open {
  transform: rotate(180deg);
}

.employee-bio__body {
  max-height: 150px;
  overflow-y: auto;
  padding: 8px 10px 10px;
  border-top: 1px solid var(--astra-border-subtle);
}

.employee-actions {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}

.employee-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px;
  border-radius: 8px;
  border: 1px solid var(--astra-border);
  background: var(--astra-bg-panel);
  color: var(--astra-text-primary);
  text-decoration: none;
  font-size: 12.5px;
  font-weight: 500;
  transition: background-color 0.14s ease, border-color 0.14s ease;
}

.employee-action:hover {
  background: var(--astra-bg-base);
  border-color: var(--astra-border-hover);
}

.employee-action--primary {
  background: var(--astra-primary);
  border-color: var(--astra-primary);
  color: #ffffff;
}

.employee-action--primary:hover {
  background: var(--astra-primary-bright);
  border-color: var(--astra-primary-bright);
}

/* ── 主体内容区 ───────────────────────────────────────────────── */
.panel-content {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  padding: 14px 18px 18px;
}

.inline-alert {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 9px 11px;
  margin-bottom: 12px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.5;
}

.inline-alert svg {
  flex: none;
  margin-top: 1px;
}

.inline-alert > span {
  flex: 1;
  min-width: 0;
}

.inline-alert small {
  display: block;
  opacity: 0.82;
}

.inline-alert--danger {
  background: var(--astra-danger-soft);
  border: 1px solid color-mix(in srgb, var(--astra-danger) 24%, transparent);
  color: var(--astra-danger);
}

.inline-alert--danger button {
  border: 0;
  background: rgba(220, 38, 38, 0.14);
  color: inherit;
  border-radius: 6px;
  padding: 3px 8px;
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
}

.inline-alert--warning {
  background: var(--astra-warning-soft);
  border: 1px solid color-mix(in srgb, var(--astra-warning) 24%, transparent);
  color: var(--astra-warning);
}

.panel-loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 36px 12px;
  color: var(--astra-text-muted);
  font-size: 12.5px;
}

.wb-spinner {
  animation: wb-spin 0.9s linear infinite;
}

@keyframes wb-spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* 指标卡片 */
.overview {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 18px;
}

.metric-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 11px;
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  background: var(--astra-bg-subtle);
  text-align: left;
  font-family: inherit;
}

.metric-card--interactive {
  cursor: pointer;
  transition: border-color 0.14s ease, background-color 0.14s ease;
}

.metric-card--interactive:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-panel);
}

.metric-card__label {
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 4px;
  font-size: 11.5px;
  color: var(--astra-text-muted);
}

.metric-card__value {
  font-size: 20px;
  font-weight: 650;
  line-height: 1.2;
  font-variant-numeric: tabular-nums;
  color: var(--astra-text-primary);
}

.metric-card__value.is-info {
  color: var(--astra-info);
}

.metric-card.is-warning {
  background: var(--astra-warning-soft);
  border-color: color-mix(in srgb, var(--astra-warning) 30%, transparent);
}

.metric-card.is-warning .metric-card__label,
.metric-card.is-warning .metric-card__value {
  color: var(--astra-warning);
}

/* 分区标题 */
.wb-section {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.inbox-section {
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--astra-border-subtle);
}

.section-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.section-heading h2 {
  margin: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--astra-text-secondary);
}

.section-count {
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
  font-weight: 500;
}

/* Worker 列表 */
.worker-list,
.issue-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.worker-row {
  border: 1px solid var(--astra-border);
  border-radius: 10px;
  background: var(--astra-bg-panel);
  overflow: hidden;
}

.worker-row[data-state="attention"] {
  border-color: color-mix(in srgb, var(--astra-warning) 32%, var(--astra-border));
}

.worker-row summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 11px;
  transition: background-color 0.14s ease;
}

.worker-row summary:hover {
  background: var(--astra-bg-subtle);
}

.worker-row summary::-webkit-details-marker {
  display: none;
}

.worker-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  background: var(--astra-bg-subtle);
  border: 1px solid var(--astra-border);
  border-radius: 7px;
  color: var(--astra-text-secondary);
  flex: none;
}

.worker-name {
  font-size: 13px;
  font-weight: 600;
  min-width: 0;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--astra-text-primary);
}

.worker-state {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 500;
  white-space: nowrap;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
}

.worker-state i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.worker-state[data-state="working"] {
  background: var(--astra-info-soft);
  color: var(--astra-info);
}

.worker-state[data-state="attention"] {
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.chevron {
  color: var(--astra-text-muted);
  flex: none;
  transition: transform 0.16s ease;
}

.worker-row details[open] .chevron {
  transform: rotate(180deg);
}

.worker-details {
  margin: 0 11px 10px;
  padding: 8px 10px;
  border-radius: 8px;
  background: var(--astra-bg-subtle);
  border: 1px solid var(--astra-border-subtle);
}

.worker-details dl {
  margin: 0;
  display: grid;
  gap: 4px;
}

.worker-details dl > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 11.5px;
  line-height: 1.55;
}

.worker-details dt {
  color: var(--astra-text-muted);
  flex: none;
}

.worker-details dd {
  margin: 0;
  text-align: right;
  color: var(--astra-text-primary);
  overflow-wrap: anywhere;
}

.worker-detail-link {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  margin-top: 8px;
  font-size: 11.5px;
  font-weight: 500;
  color: var(--astra-accent-text);
  text-decoration: none;
}

.worker-detail-link:hover {
  text-decoration: underline;
}

.worker-current {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 0 11px 10px 45px;
}

.work-task {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 3px 6px;
  text-decoration: none;
  color: var(--astra-text-primary);
  font-size: 12.5px;
  padding: 5px 7px;
  margin: 0 -7px;
  border-radius: 7px;
  transition: background-color 0.14s ease, color 0.14s ease;
}

.work-task:hover {
  background: var(--astra-bg-subtle);
  color: var(--astra-accent-text);
}

.work-task__title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 500;
}

.work-task__arrow {
  color: var(--astra-text-muted);
  opacity: 0;
  transition: opacity 0.14s ease;
}

.work-task:hover .work-task__arrow {
  opacity: 1;
}

.work-task small {
  grid-column: 1 / -1;
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--astra-text-muted);
  overflow-wrap: anywhere;
}

.work-task small.issue {
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  padding: 4px 7px;
  border-radius: 6px;
}

.more-tasks {
  font-size: 11.5px;
  font-weight: 500;
  color: var(--astra-accent-text);
  text-decoration: none;
  margin-top: 2px;
}

.more-tasks:hover {
  text-decoration: underline;
}

.worker-idle {
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--astra-text-muted);
  margin: 0;
  padding: 0 11px 10px 45px;
}

.worker-warning {
  display: flex;
  gap: 6px;
  align-items: flex-start;
  margin: 0 11px 10px;
  padding: 7px 9px;
  font-size: 11.5px;
  border-radius: 7px;
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
  line-height: 1.5;
}

.worker-warning svg {
  flex: none;
  margin-top: 2px;
}

/* 待处理列表 */
.issue-list li {
  background: var(--astra-bg-panel);
  border: 1px solid var(--astra-border);
  border-radius: 9px;
  transition: border-color 0.14s ease, background-color 0.14s ease;
}

.issue-list li:hover {
  border-color: var(--astra-border-hover);
  background: var(--astra-bg-subtle);
}

.issue-list li.urgent {
  border-color: color-mix(in srgb, var(--astra-warning) 30%, var(--astra-border));
}

.issue-list a {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 10px 11px;
  text-decoration: none;
  color: var(--astra-text-primary);
}

.issue-card__top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.issue-status {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 7px;
  border-radius: 6px;
  font-size: 11px;
  font-weight: 500;
  background: var(--astra-neutral-soft);
  color: var(--astra-neutral-text);
}

.issue-status i {
  width: 5px;
  height: 5px;
  border-radius: 50%;
  background: currentColor;
}

.issue-status[data-tone="warning"] {
  background: var(--astra-warning-soft);
  color: var(--astra-warning);
}

.issue-card__arrow {
  color: var(--astra-text-muted);
  opacity: 0;
  transition: opacity 0.14s ease, transform 0.14s ease;
}

.issue-list li:hover .issue-card__arrow {
  opacity: 1;
  color: var(--astra-text-primary);
  transform: translate(1px, -1px);
}

.issue-list strong {
  font-size: 13px;
  font-weight: 600;
  line-height: 1.4;
  overflow-wrap: anywhere;
}

.issue-list p {
  font-size: 12px;
  color: var(--astra-text-secondary);
  line-height: 1.5;
  margin: 0;
}

.inbox-clear {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12.5px;
  color: var(--astra-text-muted);
  background: var(--astra-bg-subtle);
  border: 1px solid var(--astra-border-subtle);
  padding: 12px 13px;
  border-radius: 9px;
}

.inbox-clear svg {
  color: var(--astra-success);
  flex: none;
}

.panel-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  text-align: center;
  font-size: 12px;
  color: var(--astra-text-muted);
  padding: 20px 12px;
  background: var(--astra-bg-subtle);
  border: 1px dashed var(--astra-border);
  border-radius: 10px;
}

.panel-empty p {
  margin: 0;
}

.panel-empty a {
  color: var(--astra-accent-text);
  font-size: 12px;
  font-weight: 500;
  text-decoration: none;
}

.panel-empty a:hover {
  text-decoration: underline;
}

/* 底栏 */
.panel-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 9px 16px;
  border-top: 1px solid var(--astra-border);
  background: var(--astra-bg-subtle);
  color: var(--astra-text-muted);
  font-size: 11.5px;
}

.panel-footer__time {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--astra-font-mono);
}

.panel-footer i {
  display: inline-block;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--astra-success);
}

.panel-footer i.stale {
  background: var(--astra-warning);
}

.panel-footer button {
  border: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  background: transparent;
  color: var(--astra-text-muted);
  cursor: pointer;
  border-radius: 6px;
  transition: background-color 0.14s ease, color 0.14s ease;
}

.panel-footer button:hover {
  background: var(--astra-bg-elevated);
  color: var(--astra-text-primary);
}

.panel-footer button:disabled {
  opacity: 0.45;
  cursor: default;
}

.el-pagination {
  margin-top: 4px;
  justify-content: center;
}

button:focus-visible,
a:focus-visible,
summary:focus-visible {
  outline: 2px solid var(--astra-primary);
  outline-offset: 2px;
}

@media (max-width: 759px) {
  .employee-workbench {
    top: 44%;
    left: 10px;
    right: 10px;
    bottom: 65px;
    width: auto;
    border-radius: 12px 12px 8px 8px;
  }
  .panel-header {
    padding: 13px 15px 12px;
  }
  .panel-content {
    padding: 12px 15px;
  }
  .panel-footer {
    padding: 8px 15px;
  }
}
</style>
