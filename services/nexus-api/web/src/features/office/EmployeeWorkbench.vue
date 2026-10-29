<template>
  <aside
    class="employee-workbench"
    aria-labelledby="office-employee-name"
    aria-label="员工工作台"
  >
    <header class="panel-header">
      <div class="eyebrow">DIGITAL EMPLOYEE / 数字员工</div>
      <button
        class="close-button"
        type="button"
        aria-label="返回办公室全景"
        @click="$emit('close')"
      >
        <X :size="19" />
      </button>
      <div class="employee-heading">
        <div class="portrait">
          <img
            :src="`/office/person${robotVariant(employee)}_down_0.png`"
            alt=""
          />
        </div>
        <div class="employee-title">
          <h1 id="office-employee-name">{{ employee.name }}</h1>
          <p>{{ employee.domain || "数字员工" }}</p>
          <span class="status-pill" :data-state="state"
            ><i />{{ STATE_TEXT[state] }}</span
          >
        </div>
      </div>
      <div class="employee-actions">
        <RouterLink :to="{ path: '/chat', query: { employee: employee.id } }"
          ><MessageSquare :size="14" /> 员工对话</RouterLink
        >
        <RouterLink :to="{ path: '/tasks', query: { owner: employee.id } }"
          ><ListTodo :size="14" /> 全部任务</RouterLink
        >
      </div>
    </header>
    <div class="panel-content" :aria-busy="loading">
      <div v-if="error" class="inline-error" role="alert">
        <AlertCircle :size="17" /><span
          >{{ error }}<small v-if="data">当前保留上次确认数据</small></span
        ><button type="button" @click="$emit('refresh')">重试</button>
      </div>
      <div
        v-if="data?.degradedSources.length"
        class="inline-error"
        role="status"
      >
        部分来源暂不可用：{{ data.degradedSources.join("、") }}
      </div>
      <p v-if="loading && !data" class="panel-empty" role="status">
        正在读取员工工作状态…
      </p>
      <template v-if="data">
        <div class="overview">
          <div>
            <strong>{{ data.employee.counts.running }}</strong
            ><span>执行中</span>
          </div>
          <button type="button" @click="jumpInbox">
            <strong>{{ data.employee.counts.queued }}</strong
            ><span>待分发 <ArrowUpRight :size="10" /></span></button
          ><button
            type="button"
            :class="{
              warning:
                data.employee.counts.waiting + data.employee.counts.failed > 0,
            }"
            @click="jumpInbox"
          >
            <strong>{{
              data.employee.counts.waiting + data.employee.counts.failed
            }}</strong
            ><span>待关注 <ArrowUpRight :size="10" /></span>
          </button>
        </div>
        <section aria-labelledby="office-workers-heading">
          <div class="section-eyebrow">EXECUTION / 执行资源</div>
          <div class="section-heading">
            <h2 id="office-workers-heading">Agent Worker</h2>
            <span>{{ data.workerTotal }} 个关联资源</span>
          </div>
          <p class="section-caption">按需调度的共享资源 · 人工与异常优先</p>
          <ul v-if="data.workers.length" class="worker-list">
            <li
              v-for="worker in data.workers"
              :key="worker.id"
              class="worker-row"
              :data-state="workerState(worker)"
            >
              <details>
                <summary>
                  <span class="worker-icon"><Monitor :size="17" /></span
                  ><b>{{ worker.name }}</b
                  ><span class="worker-state">{{ workerLabel(worker) }}</span
                  ><ChevronDown class="chevron" :size="14" />
                </summary>
                <div class="worker-details">
                  <dl>
                    <div>
                      <dt>关联方式</dt>
                      <dd>
                        {{
                          worker.association === "task"
                            ? "当前任务执行关联"
                            : "当前调度配置可用资源"
                        }}
                      </dd>
                    </div>
                    <div>
                      <dt>运行环境</dt>
                      <dd>{{ worker.environmentName || "尚未配置" }}</dd>
                    </div>
                    <div>
                      <dt>健康 / 占用</dt>
                      <dd>
                        {{ HEALTH_TEXT[worker.health] }} /
                        {{ OCCUPANCY_TEXT[worker.occupancy] }}
                      </dd>
                    </div>
                  </dl>
                  <RouterLink :to="`/settings/workers/${worker.id}`"
                    >查看 Worker 详情 <ArrowUpRight :size="13"
                  /></RouterLink>
                </div>
              </details>
              <div v-if="worker.tasks.length" class="worker-current">
                <RouterLink
                  v-for="task in worker.tasks"
                  :key="task.id"
                  :to="`/tasks/${task.id}`"
                  class="work-task"
                  ><span>{{ task.title }}</span
                  ><ArrowUpRight :size="13" /><small
                    :class="{ issue: needsAttention(task) }"
                    >{{ taskReason(task) }}</small
                  ></RouterLink
                >
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
                  >查看其余关联任务</RouterLink
                >
              </div>
              <p v-else class="worker-idle">
                {{
                  worker.health === "online"
                    ? "等待新的任务调度"
                    : healthReason(worker)
                }}
              </p>
              <div
                v-if="worker.health !== 'online' && worker.tasks.length"
                class="worker-warning"
              >
                <AlertCircle :size="13" />{{ healthReason(worker) }}
              </div>
              <div
                v-if="
                  worker.occupancy === 'waiting' &&
                  !worker.employeeTaskCounts.WAITING
                "
                class="worker-warning"
              >
                <AlertCircle :size="13" />共享 Worker
                有其他待人工事项，可展开查看资源详情。
              </div>
            </li>
          </ul>
          <div v-else class="panel-empty">
            <Monitor :size="24" />
            <p>暂无关联的执行资源</p>
            <small>任务分发后，实际执行的共享 Worker 会出现在这里。</small
            ><RouterLink to="/settings/workers">查看共享 Worker</RouterLink>
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
        <section
          ref="inbox"
          class="inbox-section"
          aria-labelledby="office-inbox-heading"
        >
          <div class="section-eyebrow">INBOX / 未处理工作</div>
          <div class="section-heading">
            <h2 id="office-inbox-heading">等待处理</h2>
            <span>{{ data.issueTotal }} 项</span>
          </div>
          <ul v-if="data.issues.length" class="issue-list">
            <li
              v-for="task in data.issues"
              :key="task.id"
              :class="{ urgent: needsAttention(task) }"
            >
              <RouterLink :to="`/tasks/${task.id}`"
                ><div>
                  <strong>{{ task.title }}</strong
                  ><span>{{ STATUS_TEXT[task.status] }}</span>
                </div>
                <p>{{ taskReason(task) }}</p>
                <small>打开任务处理 <ArrowUpRight :size="12" /></small
              ></RouterLink>
            </li>
          </ul>
          <div v-else class="inbox-clear">
            <CircleCheck :size="18" /> 当前没有待处理事项
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
      <span
        ><i :class="{ stale: !!error }" />{{
          data ? `最近确认 ${formatTime(data.generatedAt)}` : "等待工作状态"
        }}</span
      ><button
        type="button"
        :disabled="loading"
        aria-label="刷新员工工作台"
        @click="$emit('refresh')"
      >
        <RefreshCw :size="13" />
      </button>
    </footer>
  </aside>
</template>
<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";
import {
  AlertCircle,
  ArrowUpRight,
  ChevronDown,
  CircleCheck,
  ListTodo,
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
  robotVariant,
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
function jumpInbox() {
  inbox.value?.scrollIntoView({ block: "start" });
}
const state = computed(() =>
  employeeState(
    props.data?.employee ?? props.employee,
    !props.data || !!props.error,
  ),
);
function workerLabel(worker: OfficeWorker) {
  if (worker.health !== "online") return HEALTH_TEXT[worker.health];
  if (worker.employeeTaskCounts.WAITING) return "等待人工处理";
  if (worker.tasks.some(needsAttention)) return "执行需关注";
  return OCCUPANCY_TEXT[worker.occupancy];
}
function healthReason(worker: OfficeWorker) {
  return worker.health === "offline"
    ? "Worker 已离线，请检查执行环境与连接"
    : worker.health === "error"
      ? "执行环境异常，请打开 Worker 详情检查"
      : "Worker 健康状态未确认，请检查运行环境";
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
  background: #fcfcf3;
  border: 1px solid #9db9a3;
  box-shadow: 0 12px 45px #345e4533;
  border-radius: 12px;
  overflow: hidden;
  color: #355c46;
}
.panel-header {
  padding: 23px 24px 17px;
  background: #f2f5e9;
  border-bottom: 1px solid #dce4d3;
  position: relative;
}
.eyebrow,
.section-eyebrow {
  color: #7b937d;
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 1.5px;
}
.eyebrow {
  padding-right: 28px;
}
.close-button {
  position: absolute;
  right: 15px;
  top: 15px;
  border: 1px solid #d5e0cc;
  background: #fbfcf4;
  color: #60816a;
  padding: 6px;
  border-radius: 6px;
  display: flex;
  cursor: pointer;
}
.employee-heading {
  display: flex;
  align-items: center;
  gap: 15px;
  margin-top: 20px;
}
.portrait {
  width: 65px;
  height: 73px;
  flex-shrink: 0;
  background: #dce8d5;
  border: 1px solid #c7d7c1;
  overflow: hidden;
  border-radius: 6px;
}
.portrait img {
  width: 65px;
  height: 98px;
  image-rendering: pixelated;
  margin-top: -11px;
}
.employee-title {
  min-width: 0;
}
.employee-title h1 {
  font-size: 23px;
  line-height: 1.3;
  margin: 0;
  overflow-wrap: anywhere;
}
.employee-title p {
  font-size: 12px;
  color: #73816b;
  margin: 5px 0 9px;
}
.status-pill {
  font-size: 10px;
  font-weight: 700;
  background: #e2eddb;
  padding: 4px 8px;
  color: #4e7b57;
  border-radius: 4px;
  display: inline-flex;
  align-items: center;
  gap: 5px;
}
.status-pill i {
  width: 5px;
  height: 5px;
  background: currentColor;
}
.status-pill[data-state="attention"] {
  color: #ad6246;
  background: #f7e9d9;
}
.status-pill[data-state="unknown"],
.status-pill[data-state="offline"] {
  color: #757d73;
  background: #e8eae3;
}
.employee-actions {
  display: flex;
  gap: 8px;
  margin-top: 18px;
}
.employee-actions a {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px 10px;
  background: #fdfef6;
  border: 1px solid #cad9c4;
  color: #52745a;
  text-decoration: none;
  border-radius: 6px;
  font-size: 11px;
}
.employee-actions a:first-child {
  background: #49765b;
  border-color: #49765b;
  color: #fffef3;
}
.panel-content {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
  scrollbar-width: thin;
  scrollbar-color: #c5d2bd transparent;
  padding: 18px 22px 24px;
}
.overview {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 8px;
  margin-bottom: 25px;
}
.overview > div,
.overview > button {
  display: flex;
  flex-direction: column;
  gap: 5px;
  padding: 12px 10px;
  background: #eff3e7;
  border: 1px solid #e0e7d8;
  border-radius: 6px;
}
.overview > button {
  text-align: left;
  font-family: inherit;
  color: inherit;
  cursor: pointer;
}
.overview > button:hover {
  border-color: #a9be9c;
}
.overview span svg {
  vertical-align: middle;
}
.overview strong {
  font-size: 23px;
  line-height: 1.2;
}
.overview span {
  font-size: 10px;
  color: #778773;
}
.overview .warning {
  background: #fbefdf;
  border-color: #eadcc6;
  color: #b06b44;
}
.section-heading {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  margin-top: 7px;
}
.section-heading h2 {
  font-size: 19px;
  margin: 0;
}
.section-heading > span {
  font-size: 10px;
  color: #83917c;
}
.section-caption {
  font-size: 10px;
  color: #82907b;
  margin: 8px 0 15px;
}
.worker-list,
.issue-list {
  list-style: none;
  padding: 0;
  margin: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.worker-row {
  border: 1px solid #d9e2ce;
  border-radius: 7px;
  background: #fffef7;
  overflow: hidden;
  border-left: 3px solid #9db89b;
}
.worker-row[data-state="attention"] {
  border-color: #e6d2b9;
  border-left-color: #ba7954;
  background: #fffaf0;
}
.worker-row[data-state="working"] {
  border-left-color: #6d9c77;
}
.worker-row summary {
  list-style: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 12px 9px;
}
.worker-row summary::-webkit-details-marker {
  display: none;
}
.worker-icon {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 29px;
  height: 28px;
  background: #edf1e3;
  border: 1px solid #d8e1cd;
  border-radius: 5px;
  color: #709176;
  flex-shrink: 0;
}
.worker-row summary b {
  font-size: 12px;
  min-width: 0;
  overflow-wrap: anywhere;
  flex: 1;
}
.worker-state {
  font-size: 9px;
  color: #64846a;
  white-space: nowrap;
  padding: 4px 6px;
  background: #edf2e4;
  border-radius: 4px;
}
.worker-row[data-state="attention"] .worker-state {
  background: #f7e8d4;
  color: #a66c46;
}
.chevron {
  color: #91a188;
  flex-shrink: 0;
}
.worker-row details[open] .chevron {
  transform: rotate(180deg);
}
.worker-details {
  margin: 0 12px 10px;
  padding: 9px 0;
  border-bottom: 1px dashed #dce3d2;
}
.worker-details dl {
  margin: 0;
}
.worker-details dl > div {
  display: flex;
  justify-content: space-between;
  gap: 12px;
  font-size: 10px;
  line-height: 1.7;
  margin: 4px 0;
}
.worker-details dt {
  color: #83927c;
  flex-shrink: 0;
}
.worker-details dd {
  margin: 0;
  text-align: right;
  overflow-wrap: anywhere;
}
.worker-details a {
  display: inline-flex;
  align-items: center;
  font-size: 10px;
  color: #3f7b58;
  margin-top: 8px;
}
.worker-current {
  padding: 0 12px 11px 49px;
}
.work-task {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 5px;
  text-decoration: none;
  color: #59725b;
  font-size: 11px;
  padding: 5px 0;
}
.work-task > span {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.work-task small {
  grid-column: 1/-1;
  font-size: 10px;
  line-height: 1.6;
  color: #89927f;
  overflow-wrap: anywhere;
}
.work-task small.issue {
  background: #f9ead7;
  color: #a76742;
  padding: 5px 8px;
  border-radius: 3px;
}
.more-tasks {
  font-size: 10px;
  color: #548267;
}
.worker-idle {
  font-size: 10px;
  line-height: 1.7;
  color: #89927e;
  margin: 0;
  padding: 0 13px 12px 49px;
}
.worker-warning {
  display: flex;
  gap: 5px;
  align-items: flex-start;
  margin: 0 12px 10px;
  padding: 7px;
  font-size: 10px;
  background: #f8e7d3;
  color: #a26944;
  line-height: 1.6;
}
.worker-warning svg {
  flex-shrink: 0;
  margin-top: 2px;
}
.inbox-section {
  margin-top: 28px;
  padding-top: 20px;
  border-top: 1px solid #e1e6d7;
}
.issue-list {
  margin-top: 15px;
}
.issue-list li {
  background: #f1f4e8;
  border: 1px solid #dfe6d3;
  border-radius: 6px;
}
.issue-list li.urgent {
  background: #faf0e2;
  border-color: #ead8c0;
}
.issue-list a {
  display: block;
  padding: 12px;
  text-decoration: none;
  color: #536d52;
}
.issue-list a > div {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 9px;
}
.issue-list strong {
  font-size: 12px;
  overflow-wrap: anywhere;
}
.issue-list a > div span {
  font-size: 9px;
  flex-shrink: 0;
  color: #9c805e;
}
.issue-list p {
  font-size: 10px;
  color: #8b816b;
  line-height: 1.7;
  margin: 7px 0;
}
.issue-list small {
  font-size: 10px;
  display: flex;
  align-items: center;
  gap: 3px;
  color: #738d68;
}
.inbox-clear {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 15px;
  font-size: 11px;
  color: #819176;
  background: #eef3e6;
  padding: 15px;
  border-radius: 6px;
}
.panel-empty {
  text-align: center;
  font-size: 12px;
  color: #819179;
  line-height: 1.8;
  padding: 25px 12px;
  background: #f1f4e9;
  border-radius: 7px;
  margin: 15px 0;
}
.panel-empty > svg {
  color: #94ab8f;
}
.panel-empty p {
  margin: 7px 0;
}
.panel-empty small {
  display: block;
  font-size: 10px;
}
.panel-empty a {
  display: inline-block;
  color: #4b7a58;
  margin-top: 10px;
  font-size: 11px;
}
.inline-error {
  display: flex;
  align-items: center;
  gap: 8px;
  background: #faebda;
  border: 1px solid #e9d2b7;
  border-radius: 6px;
  padding: 10px;
  margin-bottom: 14px;
  font-size: 11px;
  color: #aa6842;
  line-height: 1.6;
}
.inline-error > span {
  flex: 1;
}
.inline-error small {
  display: block;
}
.inline-error button {
  border: 0;
  background: #edd8bd;
  padding: 5px 8px;
  color: #845430;
  border-radius: 4px;
  cursor: pointer;
}
.inline-error > svg {
  flex-shrink: 0;
}
.panel-footer {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 22px;
  border-top: 1px solid #e0e7d6;
  background: #f4f6ec;
  color: #8a977f;
  font-size: 9px;
}
.panel-footer i {
  display: inline-block;
  width: 5px;
  height: 5px;
  background: #6d9973;
  margin-right: 6px;
}
.panel-footer i.stale {
  background: #c98c51;
}
.panel-footer button {
  border: 0;
  display: flex;
  background: transparent;
  color: #7f9374;
  cursor: pointer;
  padding: 3px;
}
.panel-footer button:disabled {
  opacity: 0.5;
}
.el-pagination {
  margin-top: 12px;
  justify-content: center;
}
a:hover {
  filter: brightness(0.96);
}
button:focus-visible,
a:focus-visible,
summary:focus-visible {
  outline: 2px solid #50805c;
  outline-offset: 3px;
}
@media (max-width: 759px) {
  .employee-workbench {
    top: 44%;
    left: 10px;
    right: 10px;
    bottom: 65px;
    width: auto;
    border-radius: 12px 12px 7px 7px;
  }
  .panel-header {
    padding: 14px 18px 12px;
  }
  .employee-heading {
    margin-top: 10px;
    gap: 10px;
  }
  .portrait {
    height: 55px;
    width: 50px;
  }
  .portrait img {
    width: 50px;
    height: 75px;
    margin-top: -8px;
  }
  .employee-title h1 {
    font-size: 19px;
  }
  .employee-title p {
    margin: 3px 0 5px;
  }
  .employee-actions {
    margin-top: 12px;
  }
  .panel-content {
    padding: 14px 18px;
  }
  .overview {
    margin-bottom: 18px;
  }
  .eyebrow {
    font-size: 8px;
  }
  .panel-footer {
    padding: 9px 18px;
  }
}
</style>
